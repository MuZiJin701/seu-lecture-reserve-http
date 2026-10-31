"""Configured administrators and process-local, fixed-lifetime browser sessions."""

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import deque
from typing import Any, Callable, Mapping

SESSION_SECONDS = 12 * 60 * 60
COOKIE_NAME = 'seu_admin_session'
ITERATIONS = 600_000
HASH_PATTERN = re.compile(r'pbkdf2_sha256\$600000\$([a-f0-9]{32})\$([a-f0-9]{64})')


def hash_password(password: str) -> str:
    if not password or len(password) > 1024:
        raise ValueError('invalid_admin_password')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}'


class Administrators:
    def __init__(self, shared: Mapping[str, str], *, clock: Callable[[], float] = time.monotonic):
        def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError('admin_configuration_invalid')
                result[key] = value
            return result

        try:
            admins = json.loads(shared.get('SEU_WEB_ADMINS', '{}'), object_pairs_hook=unique)
        except (TypeError, ValueError):
            raise ValueError('admin_configuration_invalid') from None
        if (not isinstance(admins, dict) or not 1 <= len(admins) <= 128
                or any(not re.fullmatch(r'[\w.@-]{1,80}', name)
                       or not isinstance(value, str) or not 1 <= len(value) <= 1024
                       or (value.startswith('pbkdf2_sha256$') and not HASH_PATTERN.fullmatch(value))
                       for name, value in admins.items())):
            raise ValueError('admin_configuration_invalid')
        self.admins: dict[str, str] = {
            name: value if HASH_PATTERN.fullmatch(value) else hash_password(value)
            for name, value in admins.items()
        }
        self.clock = clock
        self.sessions: dict[str, dict[str, Any]] = {}
        self.attempts: deque[tuple[float, str]] = deque()
        # In-flight password validations. A slot is reserved before the (expensive) hash and
        # settled afterwards so concurrent requests cannot all pass the budget check first.
        self.pending: dict[str, int] = {}
        self.pending_total = 0
        self.lock = threading.Lock()

    def clear(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.attempts.clear()
            self.pending.clear()
            self.pending_total = 0

    def _prune(self) -> None:
        now = self.clock()
        self.sessions = {key: value for key, value in self.sessions.items() if value['expires'] > now}
        while self.attempts and self.attempts[0][0] <= now - 60:
            self.attempts.popleft()

    def _reserve(self, address: str) -> None:
        """Reserve one validation slot; call under the lock."""
        in_flight = self.pending.get(address, 0)
        failures = sum(ip == address for _, ip in self.attempts)
        if (len(self.attempts) + self.pending_total >= 100
                or failures + in_flight >= 5):
            raise ValueError('login_rate_limited')
        self.pending[address] = in_flight + 1
        self.pending_total += 1

    def _release(self, address: str) -> None:
        """Release one validation slot; call under the lock."""
        remaining = self.pending.get(address, 0) - 1
        if remaining > 0:
            self.pending[address] = remaining
        else:
            self.pending.pop(address, None)
        self.pending_total = max(0, self.pending_total - 1)

    def login(self, username: str, password: str, address: str) -> tuple[str, dict[str, Any]]:
        with self.lock:
            self._prune()
            self._reserve(address)
        try:
            # Unknown names pay the same hash cost and return the same error as wrong passwords.
            encoded = self.admins.get(username, next(iter(self.admins.values())))
            match = HASH_PATTERN.fullmatch(encoded)
            assert match is not None
            actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(match[1]), ITERATIONS)
            correct = hmac.compare_digest(actual, bytes.fromhex(match[2]))
        except BaseException:
            with self.lock:
                self._release(address)
            raise
        accepted = correct and username in self.admins
        with self.lock:
            self._release(address)
            self._prune()
            if accepted:
                # Only failures consume the budget; a success clears this source's record.
                self.attempts = deque(item for item in self.attempts if item[1] != address)
            else:
                self.attempts.append((self.clock(), address))
        if not accepted:
            raise ValueError('login_rejected')
        token = secrets.token_urlsafe(32)
        session = dict(username=username, csrf=secrets.token_urlsafe(32), expires=self.clock() + SESSION_SECONDS)
        with self.lock:
            if len(self.sessions) >= 1024:
                raise ValueError('login_rate_limited')
            self.sessions[token] = session
        return token, session

    def session(self, token: str) -> dict[str, Any] | None:
        with self.lock:
            self._prune()
            return self.sessions.get(token)

    def logout(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)
