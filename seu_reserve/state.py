"""Private, atomic task checkpoint with a process lock."""

import fcntl
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


class Task:
    def __init__(self, directory: Path, username: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.path = directory / 'task.json'
        self.lock = os.open(directory / 'task.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            account = hashlib.sha256(username.encode()).hexdigest()
            if self.path.exists() or self.path.is_symlink():
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd) as source:
                    data = json.load(source)
                if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
                        or data.get('status') not in ('scanning', 'pending', 'done')
                        or not isinstance(data.get('account'), str)
                        or not isinstance(data.get('activity'), str)
                        or not isinstance(data.get('cooldowns'), dict)
                        or (data['status'] in ('pending', 'done') and not data['activity'])
                        or type(data.get('retry_at', 0)) not in (int, float)
                        or not math.isfinite(data.get('retry_at', 0))
                        or any(not isinstance(k, str) or type(v) not in (int, float) or not math.isfinite(v)
                               for k, v in data['cooldowns'].items())):
                    raise ValueError('state_invalid')
                if username and account != data['account']:
                    raise ValueError('state_account_mismatch')
                if not username and data['status'] != 'done':
                    raise ValueError('credentials_missing')
                self.data = data
            else:
                if not username:
                    raise ValueError('credentials_missing')
                self.data = {'version': 1, 'account': account, 'status': 'scanning',
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
