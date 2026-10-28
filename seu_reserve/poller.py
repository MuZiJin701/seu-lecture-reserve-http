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


def server_directed_error(exc: BaseException) -> bool:
    """Whether a request error asked the client to wait before another request."""
    retry_after = float(getattr(exc, 'retry_after', 0) or 0)
    return (retry_after > 0 or getattr(exc, 'http_status', None) == 429
            or getattr(exc, 'args', ())[:1] == ('server_backoff',))


class ActivityPoller:
    def __init__(self, query: Callable[[], list[dict[str, Any]]], *,
                 interval: float, clock: Callable[[], float] = time.monotonic):
        self._query = query
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._stop = threading.Event()
        # Set after every completed poll (success or failure) so the scan loop can sleep
        # until the next read instead of re-checking wall-clock freshness.
        self._updated = threading.Event()
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

    def poll_once(self) -> bool:
        """One query; store the result (or the error type, Retry-After and wait class)."""
        started = self._clock()
        try:
            rows = self._query()
        except Exception as exc:
            retry_after = float(getattr(exc, 'retry_after', 0) or 0)
            with self._lock:
                previous = self._snapshot
                self._snapshot = Snapshot(
                    rows=previous.rows if previous is not None else [],
                    at=previous.at if previous is not None else started,
                    error=type(exc).__name__,
                    retry_after=retry_after,
                    failed_at=self._clock(),
                    server_directed=server_directed_error(exc))
                self._polls += 1
            self._updated.set()
            return False
        with self._lock:
            self._snapshot = Snapshot(rows=rows, at=self._clock())
            self._polls += 1
        self._updated.set()
        return True

    def start(self) -> None:
        if self.running:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='activity-poller', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        self._stop.set()
        if thread is not None:
            # Do not stall the scan loop on an in-flight request; the daemon thread finishes
            # on its own and discards its result. The caller closes the session afterwards.
            thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            started = self._clock()
            if not self.poll_once():
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
                self._stop.wait(wait)

    def _take_align(self) -> float | None:
        """Consume the pending alignment target (one-shot) under the state lock."""
        with self._lock:
            target, self._align = self._align, None
            return target
