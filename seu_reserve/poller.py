"""Background read-only activity-list polling used during opening windows.

The poller only issues the read-only activity-list query on its own authenticated
session. It never submits, updates task state or sends alerts: the scan loop still
owns every decision and every write. Snapshot freshness is judged by the caller.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Snapshot:
    rows: list[dict[str, Any]]
    at: float
    error: str | None = None
    retry_after: float = 0.0
    # When the error carried a usable server-directed wait (429/Retry-After or the
    # server_backoff code), the scan loop must observe the same pause instead of falling
    # back to a synchronous query.
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
                 interval: float, clock: Callable[[], float] = time.monotonic,
                 gate: WaitGate | None = None,
                 on_exit: Callable[[], None] | None = None):
        self._query = query
        self._interval = interval
        self._clock = clock
        self._gate = gate
        self._on_exit = on_exit
        self._exit_called = False
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._stop = threading.Event()
        # Set after every completed poll (success or failure) so the scan loop can sleep
        # until the next read instead of re-checking wall-clock freshness.
        self._updated = threading.Event()
        # Wakes the interval wait early for an on-demand refresh (and for stop). A refresh
        # requested while a query is already in flight is satisfied by that query: the target
        # version records which completed poll makes the request redundant.
        self._wake = threading.Event()
        self._refresh_target = 0
        # One-shot start-of-poll target, consumed by the next wait computation. Targets
        # already in the past are dropped so alignment can never rewind or busy-wait.
        self._align: float | None = None
        self._polls = 0
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

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def polls(self) -> int:
        """Completed query attempts, successful and failed."""
        with self._lock:
            return self._polls

    def wait_update(self, timeout: float, since: int | None = None) -> bool:
        """Sleep until a poll completes after ``since``; False on timeout or while not running.

        ``since`` is the ``polls`` version the caller last consumed (0 when unsure). Checking the
        version *before* clearing the pulse and again *after* clearing closes the race where a
        poll completes between the version read and the clear: that update is reported instead of
        being silently dropped until the timeout expires.
        """
        with self._lock:
            version = self._polls
        if since is None or since > version:
            since = version
        if version != since:
            return True
        self._updated.clear()
        with self._lock:
            if self._polls != since:
                return True
        return self._updated.wait(timeout)

    def align_at(self, when: float | None) -> None:
        """One-shot: retarget the next wait computation so the following poll starts at
        ``when`` (same clock basis).

        ``None`` drops a pending target. The target is honored only when it is still in
        the future and at most two intervals away, so the regular cadence is never pushed
        out by more than an extra period and an already-missed target is ignored.
        """
        with self._lock:
            self._align = when

    def request_refresh(self) -> None:
        """Ask the running poller to run a query now; an in-flight query satisfies the request."""
        if not self.running:
            return
        with self._lock:
            self._refresh_target = max(self._refresh_target, self._polls + 1)
            self._wake.set()

    def _call_exit(self) -> None:
        if self._exit_called:
            return
        self._exit_called = True
        if self._on_exit is not None:
            self._on_exit()

    def poll_once(self) -> bool:
        """One query; store the result (or the error type, Retry-After and wait class)."""
        started = self._clock()
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
                self._snapshot = Snapshot(
                    rows=previous.rows if previous is not None else [],
                    at=previous.at if previous is not None else started,
                    error=type(exc).__name__,
                    retry_after=retry_after,
                    failed_at=failed_at,
                    server_directed=server_directed)
                self._polls += 1
            self._updated.set()
            return False
        with self._lock:
            finished = self._clock()
            self._snapshot = Snapshot(rows=rows, at=finished, started=started,
                                      seconds=max(0.0, finished - started))
            self._polls += 1
        self._updated.set()
        return True

    def start(self) -> None:
        if self.running:
            return
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._refresh_target = 0
        self._thread = threading.Thread(target=self._run, name='activity-poller', daemon=True)
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
            while not self._stop.is_set():
                # Check the shared deadline immediately before each request. The gate is updated
                # by whichever thread received the 429, so the other reader stops even while the
                # scan loop is blocked in a captcha or submission request.
                if self._gate is not None and self._gate.remaining() > 0:
                    return
                started = self._clock()
                if not self.poll_once():
                    return
                with self._lock:
                    if (self._refresh_target > 0
                            and self._polls >= self._refresh_target):
                        # The query we just completed satisfies the pending refresh request.
                        self._refresh_target = 0
                        if not self._stop.is_set():
                            self._wake.clear()
                # A stop may have arrived while the query was in flight and the refresh clear
                # above could otherwise discard its wake: never enter the interval after a stop.
                if self._stop.is_set():
                    return
                wait = self._interval - (self._clock() - started)
                target = self._take_align()
                if target is not None:
                    now = self._clock()
                    if now < target and target - now <= 2 * self._interval:
                        # Exactly when the next poll must start; the target is consumed either
                        # way, so a missed opening cannot shift later polls.
                        wait = target - now
                if wait > 0:
                    if self._wake.wait(wait):
                        self._wake.clear()
        finally:
            self._call_exit()

    def _take_align(self) -> float | None:
        """Consume the pending alignment target (one-shot) under the state lock."""
        with self._lock:
            target, self._align = self._align, None
            return target
