"""Versioned wakeups, one CAPTCHA worker, and private append-only logs."""

import os
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import SimpleQueue
from typing import TextIO

from .state import open_nofollow


class WorkSignal:
    """Acknowledge before consuming results; publication during consumption stays visible."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._version = 0
        self._seen = 0
        self._sources: set[str] = set()

    def set(self, source: str = 'work') -> None:
        with self._condition:
            self._version += 1
            self._sources.add(source)
            self._condition.notify_all()

    def clear(self) -> None:
        with self._condition:
            self._seen = self._version
            self._sources.clear()

    def is_set(self) -> bool:
        with self._condition:
            return self._version != self._seen

    def wait(self, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._version != self._seen, timeout)

    @property
    def sources(self) -> frozenset[str]:
        with self._condition:
            return frozenset(self._sources)


@dataclass(frozen=True)
class PreparedCaptcha:
    code: str
    at: float
    seconds: float
    ocr_seconds: float
    connect_seconds: float | None
    expired: bool


class CaptchaWorker:
    """Owner-only dispatch/consume, one job including its unconsumed result, no retries."""

    def __init__(self, on_update: Callable[[], None]):
        self._jobs: SimpleQueue[tuple[Future[PreparedCaptcha], Callable[[], PreparedCaptcha]] | None] = SimpleQueue()
        self._current: Future[PreparedCaptcha] | None = None
        self._closed = False
        self.number = 0
        self.generation = 0
        def run() -> None:
            while (job := self._jobs.get()) is not None:
                future, prepare = job
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(prepare())
                except BaseException as exc:
                    future.set_exception(exc)
                finally:
                    on_update()
        self._thread = threading.Thread(target=run, name='captcha-worker', daemon=True)
        self._thread.start()

    def submit(self, prepare: Callable[[], PreparedCaptcha], *, generation: int) -> None:
        if self._closed:
            raise RuntimeError('captcha_worker_closed')
        if self._current is not None:
            raise RuntimeError('captcha_job_pending')
        self.number += 1
        self.generation = generation
        self._current = Future()
        self._jobs.put((self._current, prepare))

    @property
    def ready(self) -> bool:
        return self._current is not None and self._current.done()

    def result(self) -> PreparedCaptcha:
        if not self.ready:
            raise RuntimeError('captcha_result_unavailable')
        current, self._current = self._current, None
        assert current is not None
        return current.result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._current is not None:
            self._current.cancel()
        self._jobs.put(None)
        # HTTP owns a deadline; never close/reuse the lent session while it is still in flight.
        self._thread.join()


class LocalLogs:
    """Keep safely opened descriptors; flush each line and serialize writes and shutdown."""

    def __init__(self, directory: Path):
        self._directory = directory
        self._files: dict[str, TextIO] = {}
        self._lock = threading.Lock()
        self._closed = False

    def append(self, name: str, line: str) -> None:
        if name not in ('events.log', 'diagnostics.jsonl'):
            raise ValueError('invalid_log_name')
        with self._lock:
            if self._closed:
                return
            stream = self._files.get(name)
            try:
                if stream is None:
                    fd = open_nofollow(self._directory / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                    try:
                        stream = os.fdopen(fd, 'a', encoding='utf-8')
                    except BaseException:
                        os.close(fd)
                        raise
                    self._files[name] = stream
                stream.write(line + '\n')
                stream.flush()
            except OSError:
                failed = self._files.pop(name, None)
                if failed is not None:
                    try:
                        failed.close()
                    except OSError:
                        pass
                raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            error: OSError | None = None
            for stream in self._files.values():
                try:
                    stream.close()
                except OSError as exc:
                    error = exc
            self._files.clear()
            if error is not None:
                raise error
