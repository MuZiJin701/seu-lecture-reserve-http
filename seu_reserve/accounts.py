"""Private account catalogue; reservation checkpoints remain owned by the engine."""

import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .app import DEFAULT_CATEGORIES, Settings
from .mail import Mailer
from .state import Task, open_nofollow, read_json

MAIL_KEYS = ('SMTP_HOST', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAIL_FROM', 'MAIL_TO')


class AccountOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    titles: list[str] = Field(default_factory=list, max_length=100)
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_CATEGORIES), min_length=1, max_length=20)
    campuses: list[str] = Field(default_factory=list, max_length=20)
    online: bool = True
    offline: bool = True
    trust_device: bool = True
    prewarm: bool = True
    captcha_prefetch: bool = True
    poller_align: bool = True
    history_background: bool = True
    mail_success: bool = True

    @model_validator(mode='after')
    def managed_policy(self) -> 'AccountOptions':
        self.titles = []
        self.trust_device = self.prewarm = self.captcha_prefetch = True
        self.poller_align = self.history_background = self.mail_success = True
        return self

    @field_validator('titles', 'categories', 'campuses')
    @classmethod
    def valid_list(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 512 for value in values) or len(set(values)) != len(values):
            raise ValueError('invalid_filter_list')
        return values


class AccountInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    username: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.@-]+$')
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(default='', max_length=1024)
    recipient: str = Field(default='', max_length=254)
    mail_enabled: bool = False
    options: AccountOptions = Field(default_factory=AccountOptions)
    revision: int = Field(default=0, ge=0)

    @field_validator('recipient')
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+', value):
            raise ValueError('invalid_recipient')
        return value


def account_environ(account: Mapping[str, Any], shared: Mapping[str, str], *,
                    mail: bool = True) -> dict[str, str]:
    result = {key: shared[key] for key in (*MAIL_KEYS, 'SEU_VPN_IP') if key in shared}
    result.update(SEU_USERNAME=account['username'], SEU_PASSWORD=account['password'])
    if not mail:
        # Read-only commands (--check, --resolve-pending) never send mail, so a broken shared
        # SMTP configuration must not block them.
        for key in MAIL_KEYS:
            result.pop(key, None)
        return result
    if account['mail_enabled']:
        if not account['recipient'] or not Mailer.configured(result):
            raise ValueError('mail_configuration_invalid')
        result['MAIL_TO'] = account['recipient']
        Mailer(result)
    else:
        for key in MAIL_KEYS:
            result.pop(key, None)
    return result


def account_settings(account: Mapping[str, Any], directory: Path) -> Settings:
    options = AccountOptions.model_validate(account['options']).model_dump()
    for key in ('titles', 'categories', 'campuses'):
        options[key] = tuple(options[key])
    return Settings(state_dir=directory, **options)


def public_account(account: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: account[key] for key in ('id', 'username', 'name', 'recipient', 'mail_enabled',
                                          'desired', 'error', 'deleting', 'revision')}
    result['options'] = {key: account['options'][key] for key in ('categories', 'campuses', 'online', 'offline')}
    return result


class Accounts:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.directory = self.root / 'accounts'
        self.directory.mkdir(exist_ok=True)
        self.path = self.root / 'accounts.sqlite3'
        fd = open_nofollow(self.path, os.O_CREAT | os.O_RDWR)
        os.close(fd)
        with closing(self.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                password TEXT NOT NULL, recipient TEXT NOT NULL, mail_enabled INTEGER NOT NULL,
                options TEXT NOT NULL, desired INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', deleting INTEGER NOT NULL DEFAULT 0)''')
            if 'revision' not in {row[1] for row in db.execute('PRAGMA table_info(accounts)')}:
                db.execute('ALTER TABLE accounts ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def state_dir(self, identifier: str) -> Path:
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('account_not_found')
        path = self.directory / identifier
        if path.is_symlink() or path.is_junction():
            raise ValueError('unsafe_state_directory')
        return path

    @staticmethod
    def decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result['options'] = AccountOptions.model_validate_json(result['options']).model_dump()
        for key in ('mail_enabled', 'desired', 'deleting'):
            result[key] = bool(result[key])
        result['mail_enabled'] = bool(result['recipient'])
        return result

    def list(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM accounts ORDER BY rowid')]

    def get(self, identifier: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM accounts WHERE id = ?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('account_not_found')
        return self.decode(row)

    def create(self, data: AccountInput) -> dict[str, Any]:
        if not data.password:
            raise ValueError('password_required')
        identifier = uuid.uuid4().hex
        try:
            with closing(self.connect()) as db, db:
                self.insert(db, identifier, data)
        except sqlite3.IntegrityError:
            raise ValueError('account_exists') from None
        return self.get(identifier)

    @staticmethod
    def insert(db: sqlite3.Connection, identifier: str, data: AccountInput) -> None:
        db.execute('''INSERT INTO accounts
            (id, username, name, password, recipient, mail_enabled, options) VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (identifier, data.username, data.name, data.password, data.recipient,
             bool(data.recipient), data.options.model_dump_json()))

    def update(self, identifier: str, data: AccountInput) -> dict[str, Any]:
        current = self.get(identifier)
        if data.username != current['username']:
            raise ValueError('account_identity_immutable')
        if current['deleting']:
            raise ValueError('account_deleting')
        if data.revision != current['revision']:
            raise ValueError('configuration_conflict')
        with closing(self.connect()) as db, db:
            cursor = db.execute('''UPDATE accounts SET name=?, password=?, recipient=?, mail_enabled=?, options=?,
                          revision=revision+1 WHERE id=? AND revision=?''',
                (data.name, data.password or current['password'], data.recipient,
                 bool(data.recipient), data.options.model_dump_json(), identifier, data.revision))
            if cursor.rowcount != 1:
                raise ValueError('configuration_conflict')
        return self.get(identifier)

    def runtime(self, identifier: str, *, desired: bool, error: str = '') -> None:
        with closing(self.connect()) as db, db:
            db.execute('UPDATE accounts SET desired=?, error=? WHERE id=?', (desired, error, identifier))

    def checkpoint(self, identifier: str) -> dict[str, Any]:
        path = self.state_dir(identifier) / 'task.json'
        try:
            result = read_json(path)
        except FileNotFoundError:
            return {}
        if not isinstance(result, dict):
            raise ValueError('state_invalid')
        return result

    def delete(self, identifier: str) -> None:
        account = self.get(identifier)
        directory = self.state_dir(identifier)
        validated: Task | None = None
        try:
            # Lock before inspecting files: a first CLI start may be about to create task.json,
            # so "no task.json yet" must never skip mutual exclusion.
            validated = Task(directory, account['username'])
            state = validated.data
            if state.get('status') == 'pending' or state.get('holds') or state.get('hold'):
                raise ValueError('reservation_protected')
            # Tombstone before releasing the lock or touching files; a CLI that re-reads the
            # catalogue now refuses to start this account.
            with closing(self.connect()) as db, db:
                db.execute('UPDATE accounts SET deleting=1, desired=0 WHERE id=?', (identifier,))
            if directory.exists():
                if directory.resolve().parent != self.directory.resolve():
                    raise ValueError('unsafe_state_directory')
                try:
                    self._remove_state_files(directory)
                except OSError:
                    raise ValueError('account_deleting') from None
                if validated is not None:
                    # The lock file must be closed before it can be removed on Windows.
                    validated.close()
                    validated = None
                try:
                    lock_path = directory / 'task.lock'
                    if lock_path.exists() or lock_path.is_symlink():
                        lock_path.unlink()
                    directory.rmdir()
                except OSError:
                    raise ValueError('account_deleting') from None
            with closing(self.connect()) as db, db:
                db.execute('DELETE FROM accounts WHERE id=?', (identifier,))
        finally:
            if validated is not None:
                validated.close()

    @staticmethod
    def _remove_state_files(directory: Path) -> None:
        """Remove everything but the checkpoint lock while the caller still holds it."""
        for child in list(directory.iterdir()):
            if child.name == 'task.lock':
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
