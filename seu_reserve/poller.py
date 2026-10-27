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


class ActivityPoller:
    def __init__(self, query: Callable[[], list[dict[str, Any]]], *,
                 interval: float, clock: Callable[[], float] = time.monotonic):
        self._query = query
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def poll_once(self) -> bool:
        """One query; store the result (or the error type and Retry-After)."""
        started = self._clock()
        try:
            rows = self._query()
        except Exception as exc:
            with self._lock:
                previous = self._snapshot
                self._snapshot = Snapshot(
                    rows=previous.rows if previous is not None else [],
                    at=previous.at if previous is not None else started,
                    error=type(exc).__name__,
                    retry_after=float(getattr(exc, 'retry_after', 0) or 0))
            return False
        with self._lock:
            self._snapshot = Snapshot(rows=rows, at=self._clock())
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
            remaining = self._interval - (self._clock() - started)
            if remaining > 0:
                self._stop.wait(remaining)
