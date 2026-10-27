"""Private, atomic task checkpoint with a process lock."""

import hashlib
import errno
import json
import math
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from .mail import mail_history


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
            os.replace(name, self.path)
            if sys.platform != 'win32':
                directory_fd = os.open(self.directory, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def close(self) -> None:
        os.close(self.lock)
