"""Private, atomic task checkpoint with a process lock."""

import hashlib
import errno
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .mail import mail_history


# A backup tool, antivirus scanner or another local reader may briefly hold task.json open on
# Windows; os.replace then fails with a sharing violation. Retry briefly instead of failing the
# checkpoint (and flooding the terminal with health_write_failed).
REPLACE_ATTEMPTS = 4
REPLACE_RETRY_SECONDS = 0.005

# Opt-in trusted-device fingerprint used with the school's SMS second factor. The value is a
# client-generated stable ID: it identifies this state directory to the school, is never sent
# anywhere else and is never logged.
FINGERPRINT_FILE = 'device-fingerprint'
FINGERPRINT_PATTERN = re.compile(r'[A-Za-z0-9_-]{8,128}')
# Opt-in trusted-device CAS cookies (TGT/JSESSIONID, ...). A browser keeps these across sessions,
# which is what lets the school skip the SMS second factor on a later login; the file is private
# and only the fixed cookie fields are stored (never headers, passwords or page content).
TRUST_COOKIES_FILE = 'trust-cookies.json'
TRUST_COOKIE_FIELDS = ('name', 'value', 'domain', 'path', 'secure', 'expires')


def lock_file(fd: int) -> None:
    if sys.platform == 'win32':
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def task_running(directory: Path) -> bool:
    try:
        fd = open_nofollow(directory / 'task.lock', os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            lock_file(fd)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return True
            raise
        return False
    finally:
        os.close(fd)


def read_json(path: Path) -> Any:
    fd = open_nofollow(path, os.O_RDONLY)
    with os.fdopen(fd) as source:
        return json.load(source)


def open_nofollow(path: Path, flags: int) -> int:
    if not hasattr(os, 'O_NOFOLLOW') and (path.is_symlink() or path.is_junction()):
        raise OSError('unsafe_state_file')
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        # Windows has no O_NOFOLLOW; check the opened file before reading or writing.
        if not hasattr(os, 'O_NOFOLLOW'):
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or not os.path.samestat(info, os.fstat(fd))):
                raise OSError('unsafe_state_file')
        return fd
    except BaseException:
        os.close(fd)
        raise


def replace_with_retry(source: str, target: Path) -> None:
    """Publish a same-directory replacement atomically, tolerating brief Windows reader locks."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt + 1 == REPLACE_ATTEMPTS:
                raise
            time.sleep(REPLACE_RETRY_SECONDS * (attempt + 1))


def load_or_create_device_fingerprint(directory: Path) -> str:
    """Stable, private device fingerprint for the opt-in SMS trusted-device flow.

    An existing valid value is reused so the school sees the same device across logins and
    restarts; a missing, unreadable or malformed value is replaced with a freshly generated
    ``stable_<hex>`` ID. The file is private (0600 via ``open_nofollow``) and never removed.
    I/O problems (unsafe symlink, permissions, read-only state dir) must never fail login, so the
    generated value is returned even when it could not be persisted.
    """
    path = directory / FINGERPRINT_FILE
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return 'stable_' + secrets.token_hex(16)
    try:
        fd = open_nofollow(path, os.O_RDONLY)
        with os.fdopen(fd) as source:
            value = source.read().strip()
        if FINGERPRINT_PATTERN.fullmatch(value):
            return value
    except (OSError, UnicodeDecodeError):
        pass
    value = 'stable_' + secrets.token_hex(16)
    try:
        fd = open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(fd, 'w') as target:
            target.write(value)
    except OSError:
        pass
    return value


def _trust_cookie_record(item: Any) -> dict[str, Any] | None:
    """Normalize one stored cookie to the fixed schema; None when it is not valid.

    Only the fields the school login needs are kept. Values are private local state and are never
    logged; a malformed entry is dropped instead of failing the whole file.
    """
    if not isinstance(item, dict):
        return None
    name = item.get('name')
    value = item.get('value')
    domain = item.get('domain')
    path = item.get('path')
    secure = item.get('secure', False)
    expires = item.get('expires')
    if not (isinstance(name, str) and isinstance(value, str) and isinstance(domain, str)
            and isinstance(path, str) and name and domain):
        return None
    if type(secure) is not bool:
        return None
    if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires)):
        return None
    return dict(name=name, value=value, domain=domain, path=path, secure=secure,
                expires=float(expires) if expires is not None else None)


def save_trust_cookies(directory: Path, cookies: Iterable[dict]) -> int:
    """Persist the school's CAS cookies privately and atomically; never fails the login.

    A same-directory temp file is fsynced and published with ``os.replace`` (Windows reader-lock
    retry included), so a crash cannot leave a half-written file; the state directory is created
    private (0700) and the file is created private (mkstemp 0600). Only fixed cookie fields are
    stored. Any OSError is swallowed and reported as 0 saved cookies.
    """
    records = [record for item in cookies if (record := _trust_cookie_record(item)) is not None]
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return 0
    name: str | None = None
    try:
        fd, created = tempfile.mkstemp(prefix='.trust-cookies-', dir=directory)
        name = created
        with os.fdopen(fd, 'w') as target:
            json.dump(records, target, allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        replace_with_retry(name, directory / TRUST_COOKIES_FILE)
        name = None
    except OSError:
        return 0
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except OSError:
                pass
    return len(records)


def load_trust_cookies(directory: Path) -> list[dict[str, Any]]:
    """Read persisted CAS cookies; missing, unreadable, corrupt or malformed data returns [].

    Expired cookies are dropped so a stale TGT does not suppress the normal password/SMS flow
    with a request that can only fail.
    """
    try:
        fd = open_nofollow(directory / TRUST_COOKIES_FILE, os.O_RDONLY)
        with os.fdopen(fd) as source:
            data = json.load(source)
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(data, list):
        return []
    now = time.time()
    cookies: list[dict[str, Any]] = []
    for item in data:
        record = _trust_cookie_record(item)
        if record is None:
            continue
        expires = record['expires']
        if expires is not None and expires <= now:
            continue
        cookies.append(record)
    return cookies


class Task:
    def __init__(self, directory: Path, username: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.path = directory / 'task.json'
        self.lock = open_nofollow(directory / 'task.lock', os.O_CREAT | os.O_RDWR)
        try:
            lock_file(self.lock)
            account = hashlib.sha256(username.encode()).hexdigest()
            if self.path.exists() or self.path.is_symlink():
                data = read_json(self.path)
                if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] not in (1, 2)
                        or data.get('status') not in ('scanning', 'pending', 'done')
                        or not isinstance(data.get('account'), str)
                        or not isinstance(data.get('activity'), str)
                        or not isinstance(data.get('cooldowns'), dict)
                        or (data['status'] == 'pending' and not data['activity'])
                        or (data['version'] == 1 and data['status'] == 'done' and not data['activity'])
                        or type(data.get('retry_at', 0)) not in (int, float)
                        or not 0 <= data.get('retry_at', 0) <= 253402214400
                        or not isinstance(data.get('retry_server', False), bool)
                        or any(not isinstance(k, str) or type(v) not in (int, float)
                               or not 0 <= v <= 253402214400 for k, v in data['cooldowns'].items())):
                    raise ValueError('state_invalid')
                alerts = data.get('alerts', {})
                if (not isinstance(alerts, dict)
                        or any(key not in ('query_failure', 'authentication_required', 'task_failed',
                                           'reservation_unconfirmed', 'reservation_step_failure')
                               for key in alerts)
                        or any(type(value) not in (int, float) or not math.isfinite(value)
                               or not 0 <= value <= 253402214400 for value in alerts.values())):
                    raise ValueError('state_invalid')
                for field in ('query_failure_since', 'pending_since', 'step_failure_since',
                              'pending_start', 'pending_end'):
                    since = data.get(field)
                    if since is not None and (type(since) not in (int, float) or not math.isfinite(since)
                                              or not 0 <= since <= 253402214400):
                        raise ValueError('state_invalid')
                checks = data.get('pending_checks')
                if checks is not None and (type(checks) is not int or not 0 <= checks <= 1_000_000):
                    raise ValueError('state_invalid')
                pending_category = data.get('pending_category')
                if pending_category is not None and (
                        not isinstance(pending_category, str) or not pending_category):
                    raise ValueError('state_invalid')
                hold = data.get('hold')
                if hold is not None:
                    if (not isinstance(hold, dict)
                            or not isinstance(hold.get('activity'), str) or not hold['activity']
                            or any(type(hold.get(key)) not in (int, float)
                                   or not math.isfinite(hold[key]) or not 0 <= hold[key] <= 253402214400
                                   for key in ('start', 'end', 'since', 'until'))
                            or not hold['end'] > hold['start']
                            or type(hold.get('attempts')) is not int
                            or not 0 <= hold['attempts'] <= 100):
                        raise ValueError('state_invalid')
                holds = data.get('holds')
                if holds is not None:
                    if not isinstance(holds, dict):
                        raise ValueError('state_invalid')
                    for wid, entry in holds.items():
                        if (not isinstance(wid, str) or not wid or len(wid) > 512
                                or not isinstance(entry, dict)
                                or any(type(entry.get(key)) not in (int, float)
                                       or not math.isfinite(entry[key])
                                       or not 0 <= entry[key] <= 253402214400
                                       for key in ('start', 'end', 'since', 'until'))
                                or not entry['end'] > entry['start']
                                or type(entry.get('attempts')) is not int
                                or not 0 <= entry['attempts'] <= 100
                                or not isinstance(entry.get('confirmed'), bool)
                                or entry.get('category') is not None and (
                                    not isinstance(entry['category'], str) or not entry['category'])):
                            raise ValueError('state_invalid')
                if username and account != data['account']:
                    raise ValueError('state_account_mismatch')
                if not username and (data['status'] != 'done' or data['version'] == 1):
                    raise ValueError('credentials_missing')
                if isinstance(data.get('health'), dict):
                    mail_history(data['health'])
                self.data = data
                if data['version'] == 1:
                    data['version'] = 2
                    if data['status'] == 'done':
                        data.update(status='scanning', activity='')
                    self.save()
            else:
                if not username:
                    raise ValueError('credentials_missing')
                self.data = {'version': 2, 'account': account, 'status': 'scanning',
                             'activity': '', 'cooldowns': {}}
                self.save()
        except BaseException:
            os.close(self.lock)
            raise

    data: dict[str, Any]

    def save(self) -> None:
        fd, name = tempfile.mkstemp(prefix='.task-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as target:
                json.dump(self.data, target, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            self._replace(name)
            if sys.platform != 'win32':
                directory_fd = os.open(self.directory, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _replace(self, name: str) -> None:
        """Publish the checkpoint atomically, tolerating brief reader locks."""
        replace_with_retry(name, self.path)

    def close(self) -> None:
        os.close(self.lock)
