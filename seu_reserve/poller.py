"""Background read-only activity-list and history polling in every phase.

The poller only issues its read-only query on its own authenticated
session. It never submits, updates task state or sends alerts: the scan loop still
owns every decision and every write. Snapshot freshness is judged by the caller.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .client import AuthenticationExpired, AuthenticationRejected, RemoteError


@dataclass(frozen=True)
class Snapshot:
    rows: list[dict[str, Any]]
    at: float
    error: str | None = None
    # The fixed, log-safe reason code when the client exposed one (``network_timeout``, ...);
    # ``None`` on success and for errors that only have a class name.
    reason: str | None = None
    retry_after: float = 0.0
    # When the error carried a usable server-directed wait (429/Retry-After or the
    # server_backoff code), the scan loop must observe the same pause before new requests.
    failed_at: float = 0.0
    server_directed: bool = False
    # When the query that produced this snapshot started, and how long it took. Callers can then
    # tell data age (from request start) apart from snapshot age (from response completion).
    started: float = 0.0
    seconds: float = 0.0


def server_directed_error(exc: BaseException) -> bool:
    """Whether a request error asked the client to wait before another request."""
    retry_after = float(getattr(exc, 'retry_after', 0) or 0)
    return (retry_after > 0 or getattr(exc, 'http_status', None) == 429
            or getattr(exc, 'args', ())[:1] == ('server_backoff',))


def error_reason(exc: BaseException) -> str:
    """Fixed, log-safe error code for diagnostics; never the exception message text.

    The client only lets fixed reason codes cross its boundary, so a plain ``[a-z_]+`` code is
    reported as-is; anything else (a localized message, a wrapped failure) is reduced to the
    exception class name so no server or response text can leak into the local log.
    """
    reason = str(exc)
    if isinstance(exc, RemoteError) and re.fullmatch('[a-z_]+', reason):
        return reason
    return type(exc).__name__


# A server-directed error without an explicit Retry-After still asks for a pause: the scan loop
# starts its bounded short backoff at one second, so reader threads use the same floor.
SERVER_WAIT_FLOOR = 1.0


class WaitGate:
    """Shared server-directed wait deadline for the read-only query threads.

    Only the deadline is protected by the lock; network requests never hold it. A thread that
    receives a 429/Retry-After updates the gate immediately, so every other thread checks the
    same deadline before its next request instead of waiting for the scan loop to notice.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._until = 0.0

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._until - self._clock())

    def deadline(self) -> float:
        with self._lock:
            return self._until

    def record(self, seconds: float, *, since: float | None = None) -> None:
        """Extend the shared wait by ``seconds`` counted from ``since`` (default: now)."""
        if seconds <= 0:
            return
        based_at = self._clock() if since is None else since
        self.record_until(based_at + seconds)

    def record_until(self, until: float) -> None:
        with self._lock:
            self._until = max(self._until, until)


class ActivityPoller:
    def __init__(self, query: Callable[[], list[dict[str, Any]]], *,
                 interval: float | Callable[[], float | None] | None,
                 clock: Callable[[], float] = time.monotonic,
                 gate: WaitGate | None = None,
                 on_exit: Callable[[], None] | None = None,
                 on_update: Callable[[], None] | None = None,
                 retry_delay: Callable[[int, Snapshot | None], float] | None = None,
                 retry_interval: Callable[[], float] | None = None,
                 max_consecutive_failures: int | None = None,
                 name: str = 'activity-poller'):
        self._query = query
        self._interval = interval
        self._clock = clock
        self._gate = gate
        self._on_exit = on_exit
        self._on_update = on_update
        self._retry_delay = retry_delay
        self._retry_interval = retry_interval
        # Event-driven owners stop retrying an outage after this many attempts; the next
        # explicit refresh request (or a phase change) wakes them for another try.
        self._max_failures = max_consecutive_failures
        self._name = name
        self._exit_called = False
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._last_success: tuple[Snapshot, int] | None = None
        self._stop = threading.Event()
        # Wakes the interval wait early for an on-demand refresh (and for stop). A refresh
        # requested while a query is already in flight is satisfied by that query: the target
        # version records which completed poll makes the request redundant.
        self._wake = threading.Event()
        self._policy_changed = threading.Event()
        self._refresh_target = 0
        self._polls = 0
        self._in_flight_since: float | None = None
        self._thread: threading.Thread | None = None

    def snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    def snapshot_with_version(self) -> tuple[Snapshot | None, int]:
        """The snapshot and its matching ``polls`` version, read under one lock.

        Reading them separately races with the poller thread: the caller could take an older
        snapshot and then a newer version, mark the newer version consumed and never be woken
        for the update it has not seen.
        """
        with self._lock:
            return self._snapshot, self._polls

    def snapshot_for_scan(self) -> tuple[Snapshot | None, int]:
        """Keep the original successful snapshot and version across ordinary failures."""
        with self._lock:
            latest = self._snapshot
            if (self._last_success is not None and latest is not None and latest.error is not None
                    and not latest.server_directed):
                return self._last_success
            return latest, self._polls

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def in_flight_since(self) -> float | None:
        with self._lock:
            return self._in_flight_since

    @property
    def polls(self) -> int:
        """Completed query attempts, successful and failed."""
        with self._lock:
            return self._polls

    def request_refresh(self) -> int:
        """Ask the running poller to run a query now; return the version that satisfies it.

        An in-flight query satisfies the request, so the target is ``max(pending target,
        completed polls + 1)`` computed under the same lock as the counter. Returning it lets the
        caller record the target atomically: a poll that completes around the request is then
        observed as satisfying it instead of forcing a duplicate query. Returns 0 when the poller
        is not running (no poll can satisfy the request).
        """
        if not self.running:
            return 0
        with self._lock:
            target = max(self._refresh_target, self._polls + 1)
            self._refresh_target = target
            self._wake.set()
            return target

    def reschedule(self) -> None:
        """Re-evaluate a changed phase without requesting an extra poll."""
        self._policy_changed.set()
        self._wake.set()

    def _refresh_pending(self) -> bool:
        """Whether a refresh request is not yet satisfied by a completed poll."""
        with self._lock:
            return self._refresh_target > self._polls

    def _consume_satisfied_refresh(self) -> bool:
        """Acknowledge a refresh request already satisfied by the poll that just completed.

        The wake event alone cannot tell a stale request from a new one: a request made while a
        query was in flight is satisfied when that query finishes, so its wake must be consumed
        here. Otherwise the next bounded-failure wait would return immediately and start another
        round of requests for an event that was already handled.
        """
        with self._lock:
            if self._refresh_target > 0 and self._polls >= self._refresh_target:
                self._refresh_target = 0
                if not self._stop.is_set():
                    self._wake.clear()
                return True
            return False

    def _wait_for_new_event(self, *, allow_policy: bool) -> None:
        """Wait for a genuinely new refresh request (or stop) and consume its wake.

        A policy change is not a query request: event-driven owners must apply the new budget at
        the start of the next real request instead of querying just because the phase changed.
        """
        while not self._stop.is_set():
            if self._refresh_pending():
                return
            if allow_policy and self._policy_changed.is_set():
                self._policy_changed.clear()
                return
            self._wake.wait(0.2)
            self._wake.clear()

    def _call_exit(self) -> None:
        if self._exit_called:
            return
        self._exit_called = True
        if self._on_exit is not None:
            self._on_exit()

    def _publish(self) -> None:
        if self._on_update is not None:
            try:
                self._on_update()
            except Exception:
                pass

    def poll_once(self) -> bool:
        """One query; store the result (or the error type, Retry-After and wait class)."""
        started = self._clock()
        with self._lock:
            if self._stop.is_set():
                return False
            self._in_flight_since = started
        try:
            rows = self._query()
        except Exception as exc:
            retry_after = float(getattr(exc, 'retry_after', 0) or 0)
            failed_at = self._clock()
            server_directed = server_directed_error(exc)
            if self._gate is not None and server_directed:
                # Tell every reader immediately, not only after the scan loop reads this snapshot.
                self._gate.record(retry_after if retry_after > 0 else SERVER_WAIT_FLOOR,
                                  since=failed_at)
            with self._lock:
                previous = self._snapshot
                if isinstance(exc, (AuthenticationExpired, AuthenticationRejected)):
                    self._last_success = None
                self._snapshot = Snapshot(
                    rows=previous.rows if previous is not None else [],
                    at=previous.at if previous is not None else started,
                    error=type(exc).__name__,
                    reason=error_reason(exc),
                    retry_after=retry_after,
                    failed_at=failed_at,
                    server_directed=server_directed, started=started,
                    seconds=max(0.0, failed_at - started))
                self._polls += 1
                self._in_flight_since = None
            self._publish()
            return False
        except BaseException:
            with self._lock:
                self._in_flight_since = None
            raise
        with self._lock:
            finished = self._clock()
            self._snapshot = Snapshot(rows=rows, at=finished, started=started,
                                      seconds=max(0.0, finished - started))
            self._polls += 1
            self._last_success = self._snapshot, self._polls
            self._in_flight_since = None
        self._publish()
        return True

    def start(self) -> None:
        if self.running:
            return
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._policy_changed.clear()
        self._refresh_target = 0
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, *, join: bool = True) -> None:
        """Signal the thread to stop; with ``join=False`` do not wait for an in-flight request.

        The worker owns the exit callback, so a session is closed only after its request has
        returned. The scan loop therefore never waits on network I/O when a read-only thread is
        being recycled.
        """
        thread = self._thread
        self._stop.set()
        self._wake.set()
        if join and thread is not None:
            thread.join(timeout=1.0)
        self._thread = None
        if thread is None:
            self._call_exit()

    def _run(self) -> None:
        try:
            failures = 0
            while not self._stop.is_set():
                # Check the shared deadline immediately before each request. The gate is updated
                # by whichever thread received the 429, so the other reader stops even while the
                # scan loop is blocked in a captcha or submission request.
                if self._gate is not None and self._gate.remaining() > 0:
                    if self._retry_delay is None:
                        return
                    self._wake.wait(self._gate.remaining())
                    self._wake.clear()
                    continue
                started = self._clock()
                interval = self._interval() if callable(self._interval) else self._interval
                if not self.poll_once():
                    if self._retry_delay is None or self._stop.is_set():
                        return
                    # A refresh that arrived during this failed query is satisfied by the attempt
                    # that just completed; consume it before deciding whether to keep retrying.
                    self._consume_satisfied_refresh()
                    snapshot = self.snapshot()
                    backoff = (snapshot is not None and (
                        snapshot.server_directed or snapshot.reason == 'service_unavailable'))
                    failures = failures + 1 if backoff else 0
                    if self._max_failures is not None and failures >= self._max_failures:
                        # Event-driven owner: one outage must not turn into a request storm.
                        # Genuinely new refresh events still get one more bounded round; a stale
                        # wake from the already-satisfied request must not.
                        if self._refresh_pending():
                            failures = 0
                            continue
                        self._wait_for_new_event(allow_policy=False)
                        failures = 0
                        continue
                    # Ordinary errors follow the same cadence as successful requests.
                    if backoff:
                        delay = self._retry_delay(failures, snapshot)
                    else:
                        target = interval if interval is not None else (
                            self._retry_interval() if self._retry_interval is not None else 0.0)
                        delay = max(0.0, target - (self._clock() - started))
                    self._stop.wait(delay)
                    continue
                failures = 0
                self._consume_satisfied_refresh()
                # A stop may have arrived while the query was in flight and the refresh clear
                # above could otherwise discard its wake: never enter the interval after a stop.
                if self._stop.is_set():
                    return
                if interval is None:
                    # Event-driven owner: only another explicit refresh event may query. Policy
                    # changes are applied by the next real request, never by an extra query.
                    self._policy_changed.clear()
                    if self._refresh_pending():
                        # A refresh event was requested after the query started/finished; it is
                        # still pending, so run the query it asked for.
                        self._wake.clear()
                        continue
                    self._wait_for_new_event(allow_policy=False)
                    continue
                wait = interval - (self._clock() - started)
                while wait > 0 and not self._stop.is_set():
                    if not self._wake.wait(wait):
                        break
                    self._wake.clear()
                    if not self._policy_changed.is_set():
                        break
                    self._policy_changed.clear()
                    with self._lock:
                        if self._refresh_target > self._polls:
                            break
                    interval = self._interval() if callable(self._interval) else self._interval
                    if interval is None:
                        break
                    wait = interval - (self._clock() - started)
        finally:
            self._call_exit()
