"""One supervisor, one spawned process and one private mailbox per school account."""

import contextlib
import errno
import multiprocessing
import os
import queue
import re
import secrets
import signal
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .accounts import MAIL_KEYS, AccountInput, Accounts, account_environ, account_settings, public_account
from .app import SELECTION_REASONS, run
from .mail import Mailer, mail_failure_reason
from .state import Task, hold_checkpoint, lock_file, open_nofollow, read_json, task_running

FATAL_ERRORS = {'authentication_rejected', 'credentials_missing', 'configuration_or_state_invalid',
                'state_invalid', 'account_already_running', 'mail_configuration_invalid', 'ocr_unavailable'}


def worker_main(account: dict[str, Any], root: str, shared: dict[str, str],
                commands: Any, updates: Any, stopped: Any) -> None:
    """Only primitive configuration crosses spawn; sessions and OCR are created here."""
    directory = Path(root) / 'accounts' / account['id']
    directory.mkdir(parents=True, exist_ok=True)

    def interrupted(*args: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)

    def prompt(label: str) -> str:
        challenge = secrets.token_hex(16)
        updates.put(dict(type='challenge', id=challenge, kind='sms' if '六位' in label else 'manual',
                         label=label[:600]))
        while not stopped.is_set():
            try:
                reply = commands.get(timeout=0.2)
            except queue.Empty:
                continue
            if reply.get('id') == challenge:
                updates.put(dict(type='challenge_clear', id=challenge))
                return str(reply['answer'])
        raise KeyboardInterrupt

    result, reason = 1, 'unexpected_failure'
    try:
        fd = open_nofollow(directory / 'runner.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, 'a') as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            environ = account_environ(account, shared)
            try:
                checkpoint = Task(directory, account['username'])
                checkpoint.close()
            except ValueError as exc:
                if str(exc) == 'state_in_use':
                    raise
                raise ValueError('configuration_or_state_invalid') from None
            except OSError:
                raise ValueError('configuration_or_state_invalid') from None
            if stopped.is_set():
                raise KeyboardInterrupt
            from .cli import load_recognizer
            try:
                recognize = load_recognizer()
            except Exception:
                raise ValueError('ocr_unavailable') from None
            result = run(account_settings(account, directory), environ=environ,
                         prompt=prompt, stop=stopped.is_set, recognize=recognize)
            if result == 1:
                try:
                    state = read_json(directory / 'task.json')
                    reason = state.get('health', {}).get('last_error', 'configuration_or_state_invalid')
                except (OSError, ValueError, AttributeError):
                    reason = 'configuration_or_state_invalid'
    except (KeyboardInterrupt, EOFError):
        result, reason = 130, 'stopped'
    except ValueError as exc:
        explicit = str(exc)
        result, reason = 1, explicit if explicit in FATAL_ERRORS or explicit == 'state_in_use' else 'configuration_or_state_invalid'
    except Exception:
        result, reason = 1, 'unexpected_failure'
    updates.put(dict(type='finished', result=result, reason=reason))


@dataclass
class Worker:
    process: Any
    commands: Any
    updates: Any
    stopped: Any
    started: float
    challenge: dict[str, Any] | None = None
    result: int | None = None
    reason: str = 'unexpected_failure'
    stopping: bool = False


class Manager:
    def __init__(self, accounts: Accounts, shared: Mapping[str, str], *,
                 target: Callable[..., None] = worker_main, clock: Callable[[], float] = time.monotonic):
        self.accounts = accounts
        self.shared = {key: shared[key] for key in (*MAIL_KEYS, 'SEU_VPN_IP') if key in shared}
        self.target = target
        self.clock = clock
        self.context = multiprocessing.get_context('spawn')
        self.lock = threading.RLock()
        self.workers: dict[str, Worker] = {}
        self.operations: dict[str, Any] = {}
        self.retries: dict[str, int] = {}
        self.retry_at: dict[str, float] = {}
        self.closed = threading.Event()
        self.thread: threading.Thread | None = None
        self.fd: int | None = None

    def open(self) -> None:
        self.fd = open_nofollow(self.accounts.root / 'manager.lock', os.O_CREAT | os.O_RDWR)
        try:
            try:
                lock_file(self.fd)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ValueError('manager_already_running') from None
                raise
            for account in self.accounts.list():
                if account['desired'] and not account['deleting']:
                    self._start_or_retry(account['id'], manual=False)
            self.thread = threading.Thread(target=self._monitor, name='account-supervisor', daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def _launch(self, account: dict[str, Any]) -> None:
        if account['deleting']:
            raise ValueError('account_deleting')
        environ = account_environ(account, self.shared)
        commands, updates, stopped = self.context.Queue(), self.context.Queue(), self.context.Event()
        process = self.context.Process(target=self.target,
            args=(account, str(self.accounts.root),
                  {key: environ[key] for key in (*MAIL_KEYS, 'SEU_VPN_IP') if key in environ},
                  commands, updates, stopped),
            name='reservation-' + account['id'][:8])
        try:
            process.start()
        except BaseException:
            commands.close()
            updates.close()
            raise
        self.workers[account['id']] = Worker(process, commands, updates, stopped, self.clock())

    @contextmanager
    def operation(self, identifier: str):
        with self.lock:
            gate = self.operations.setdefault(identifier, threading.RLock())
        with gate:
            yield

    def start(self, identifier: str, *, manual: bool = True) -> None:
        with self.operation(identifier), self.lock:
            if self.closed.is_set():
                raise ValueError('service_stopping')
            if identifier in self.workers:
                raise ValueError('account_already_running')
            account = self.accounts.get(identifier)
            if not manual and not account['desired']:
                return
            # A CLI `--account` run owns the same checkpoint and lock; never start a second writer.
            if task_running(self.accounts.state_dir(identifier)):
                raise ValueError('state_in_use')
            self._launch(account)
            self.accounts.runtime(identifier, desired=True)
            self.retry_at.pop(identifier, None)
            if manual:
                self.retries[identifier] = 0

    @staticmethod
    def _drain(worker: Worker) -> None:
        while True:
            try:
                message = worker.updates.get_nowait()
            except (queue.Empty, EOFError, OSError):
                return
            if message['type'] == 'challenge':
                worker.challenge = {key: message[key] for key in ('id', 'kind', 'label')}
            elif message['type'] == 'challenge_clear':
                if worker.challenge and worker.challenge['id'] == message['id']:
                    worker.challenge = None
            elif message['type'] == 'finished':
                worker.result, worker.reason = message['result'], message['reason']

    @staticmethod
    def _dispose(worker: Worker) -> None:
        worker.process.join(timeout=0)
        worker.commands.close()
        worker.updates.close()
        worker.process.close()

    def stop(self, identifier: str, *, preserve: bool = False, timeout: float = 60) -> None:
        with self.operation(identifier):
            with self.lock:
                worker = self.workers.get(identifier)
                if worker is None:
                    # Another process (CLI `--account`) may own the checkpoint; a 200 here would
                    # falsely promise a stop, so refuse instead of pretending success.
                    if task_running(self.accounts.state_dir(identifier)):
                        raise ValueError('state_in_use')
                    self.accounts.get(identifier)
                    self.retry_at.pop(identifier, None)
                    if not preserve:
                        self.accounts.runtime(identifier, desired=False)
                    return
                if not preserve:
                    self.accounts.get(identifier)
                    self.accounts.runtime(identifier, desired=False)
                self.retry_at.pop(identifier, None)
                if worker.stopping:
                    raise ValueError('account_stopping')
                worker.stopping = True
                worker.stopped.set()
            worker.process.join(timeout=timeout)
            if worker.process.is_alive():
                worker.process.terminate()
                worker.process.join(timeout=3)
            if worker.process.is_alive():
                worker.process.kill()
                worker.process.join(timeout=3)
            with self.lock:
                if worker.process.is_alive():
                    worker.stopping = False
                    raise ValueError('stop_timeout')
                self._drain(worker)
                self._dispose(worker)
                self.workers.pop(identifier, None)
                if worker.result == 0:
                    self.accounts.runtime(identifier, desired=False)

    def update(self, identifier: str, data: AccountInput) -> dict[str, Any]:
        with self.operation(identifier):
            with self.lock:
                current = self.accounts.get(identifier)
                if data.username != current['username']:
                    raise ValueError('account_identity_immutable')
                if current['deleting']:
                    raise ValueError('account_deleting')
                if data.revision != current['revision']:
                    raise ValueError('configuration_conflict')
                # Refuse before any write only when another process (CLI `--account`) owns the
                # checkpoint. Our own live worker holds the same lock, and stopping it below is
                # the normal safe-restart path, so it must never be mistaken for an external run.
                worker = self.workers.get(identifier)
                own_running = worker is not None and worker.process.is_alive()
                if not own_running and task_running(self.accounts.state_dir(identifier)):
                    raise ValueError('state_in_use')
                candidate = {**current, **data.model_dump(), 'password': data.password or current['password']}
                candidate['mail_enabled'] = bool(data.recipient)
                if current['desired']:
                    account_environ(candidate, self.shared)
                active = current['desired']
            self.stop(identifier, preserve=True)
            # Hold the checkpoint lock across the configuration write: a check followed by a
            # separate write would let an external CLI take the lock in between, so the console
            # would save a configuration that the running CLI never saw.
            with hold_checkpoint(self.accounts.state_dir(identifier)):
                with self.lock:
                    account = self.accounts.update(identifier, data)
            if active:
                try:
                    self.start(identifier)
                except (ValueError, OSError) as exc:
                    self._note_start_failure(identifier, exc)
                    if str(exc) == 'state_in_use':
                        # The configuration is saved and consistent (the CLI re-reads it under
                        # the lock); only the restart is skipped because the CLI owns the run.
                        return self.accounts.get(identifier)
                    raise
            return account

    def delete(self, identifier: str) -> None:
        with self.operation(identifier):
            self.stop(identifier)
            with self.lock:
                self.accounts.delete(identifier)

    def answer(self, identifier: str, challenge: str, answer: str) -> None:
        with self.lock:
            worker = self.workers.get(identifier)
            if worker is None or worker.stopping:
                raise ValueError('challenge_expired')
            self._drain(worker)
            if not worker.challenge or worker.challenge['id'] != challenge:
                raise ValueError('challenge_expired')
            kind = worker.challenge['kind']
            if (kind == 'sms' and answer != 'send' and not re.fullmatch('[0-9]{6}', answer)
                    or kind != 'sms' and answer != ''):
                raise ValueError('invalid_answer')
            worker.commands.put(dict(id=challenge, answer=answer))
            worker.challenge = None

    def _note_start_failure(self, identifier: str, exc: BaseException) -> None:
        """Keep the recovery intent unless the failure is explicitly not recoverable."""
        reason = str(exc)
        if reason in ('service_stopping', 'account_already_running', 'account_not_found', 'account_deleting'):
            return
        if reason == 'state_in_use':
            # The checkpoint belongs to a CLI run. Drop the web start intent without burning the
            # restart budget or sending a false "keeps exiting" alert.
            self.retry_at.pop(identifier, None)
            self.accounts.runtime(identifier, desired=False, error='state_in_use')
            return
        if reason in FATAL_ERRORS:
            self.retry_at.pop(identifier, None)
            self.accounts.runtime(identifier, desired=False, error=reason)
            if reason == 'authentication_rejected':
                self._alert_async(identifier)
            return
        count = self.retries.get(identifier, 0)
        if count >= 3:
            self.retry_at.pop(identifier, None)
            self.accounts.runtime(identifier, desired=False, error='restart_exhausted')
            self._alert_async(identifier)
            return
        self.retries[identifier] = count + 1
        self.retry_at[identifier] = self.clock() + 5
        self.accounts.runtime(identifier, desired=True, error='restarting')

    def _start_or_retry(self, identifier: str, *, manual: bool) -> None:
        try:
            self.start(identifier, manual=manual)
        except (ValueError, OSError) as exc:
            self._note_start_failure(identifier, exc)

    def _alert_async(self, identifier: str) -> None:
        try:
            account = self.accounts.get(identifier)
        except ValueError:
            return
        if account['mail_enabled']:
            threading.Thread(target=self._alert, args=(account,), daemon=True).start()

    def tick(self) -> None:
        due = []
        with self.lock:
            for identifier, worker in list(self.workers.items()):
                if worker.stopping:
                    continue
                self._drain(worker)
                if worker.process.is_alive():
                    if self.clock() - worker.started >= 600:
                        self.retries[identifier] = 0
                    continue
                worker.process.join(timeout=0)
                self._drain(worker)
                account = self.accounts.get(identifier)
                self._dispose(worker)
                self.workers.pop(identifier)
                if not account['desired']:
                    continue
                if worker.result == 0:
                    self.accounts.runtime(identifier, desired=False)
                elif worker.reason == 'state_in_use':
                    # A CLI run took the checkpoint between the pre-launch check and startup.
                    # Do not count it as an abnormal exit or alert on it.
                    self.retry_at.pop(identifier, None)
                    self.accounts.runtime(identifier, desired=False, error='state_in_use')
                elif worker.reason in FATAL_ERRORS:
                    self.accounts.runtime(identifier, desired=False, error=worker.reason)
                    if worker.reason == 'authentication_rejected':
                        self._alert_async(identifier)
                elif self.retries.get(identifier, 0) >= 3:
                    self.accounts.runtime(identifier, desired=False, error='restart_exhausted')
                    self._alert_async(identifier)
                else:
                    self.retries[identifier] = self.retries.get(identifier, 0) + 1
                    self.retry_at[identifier] = self.clock() + 5
                    self.accounts.runtime(identifier, desired=True, error='restarting')
            if not self.closed.is_set():
                for identifier, when in list(self.retry_at.items()):
                    if self.clock() < when:
                        continue
                    self.retry_at.pop(identifier)
                    due.append(identifier)
        for identifier in due:
            self._start_or_retry(identifier, manual=False)

    def _alert(self, account: dict[str, Any]) -> None:
        try:
            with self.operation(account['id']):
                if self.closed.is_set():
                    return
                try:
                    current = self.accounts.get(account['id'])
                except ValueError:
                    # Deleted while the alert was queued: never recreate its directory or send.
                    return
                mailer = Mailer(account_environ(current, self.shared))
                task = Task(self.accounts.state_dir(current['id']), current['username'])
                try:
                    alerts = task.data.setdefault('alerts', {})
                    if 'task_failed' in alerts:
                        return
                    alerts['task_failed'] = time.time()
                    health = task.data.setdefault('health', {})
                    health.update(mail_status='sending', last_mail_status='sending', last_mail_at=time.time(),
                                  last_mail_error='none', last_mail_event='task_failed', last_mail_duration_seconds=None)
                    task.save()
                    started = self.clock()
                    try:
                        mailer('task_failed')
                        health.update(mail_status='accepted', last_mail_status='accepted')
                    except Exception as exc:
                        health.update(mail_status='failed', last_mail_status='failed', last_mail_error=mail_failure_reason(exc))
                    health.update(last_mail_at=time.time(), last_mail_duration_seconds=max(0, self.clock() - started))
                    task.save()
                finally:
                    task.close()
        except Exception:
            # The persisted restart_exhausted state remains visible when SMTP is unavailable.
            pass

    def _monitor(self) -> None:
        while not self.closed.wait(0.2):
            try:
                self.tick()
            except BaseException:
                # A supervisor failure must not look healthy to the web page.
                self.closed.set()

    def snapshot(self, account: dict[str, Any]) -> dict[str, Any]:
        identifier = account['id']
        result = public_account(account)
        result.update(reservations=None, protections=[])
        with self.lock:
            worker = self.workers.get(identifier)
            if worker is not None:
                self._drain(worker)
            try:
                external = worker is None and task_running(self.accounts.state_dir(identifier))
            except OSError:
                external = False
            result.update(running=bool(worker and worker.process.is_alive()) or external,
                          external_running=external,
                          stopping=bool(worker and worker.stopping),
                          challenge=worker.challenge if worker and not worker.stopping else None,
                          restart_count=self.retries.get(identifier, 0))
        try:
            state = self.accounts.checkpoint(identifier)
            health = state.get('health', {})
            result['status'] = state.get('status', 'not_started')
            result['protected'] = bool(state.get('status') == 'pending' or state.get('holds') or state.get('hold'))
            keys = ('phase', 'updated_at', 'last_query_at', 'next_check_at', 'last_error', 'failure_count',
                    'activity_count', 'candidate_count', 'mail_status', 'last_mail_at', 'last_mail_status',
                    'last_mail_error', 'last_mail_event', 'last_mail_duration_seconds', 'progress')
            result['health'] = {key: health[key] for key in keys if isinstance(health, dict) and key in health}
            selection = state.get('selection', {}).get('activities', [])
            result['selection'] = [{key: row.get(key) for key in
                ('title', 'reason', 'reservation_start', 'reservation_end')} for row in selection
                if isinstance(row, dict) and row.get('reason') in SELECTION_REASONS]
            display = state.get('reservations')
            if (isinstance(display, dict) and type(display.get('at')) in (int, float)
                    and 0 < display['at'] <= 253402214400 and isinstance(display.get('items'), list)):
                fields = ('wid', 'title', 'category', 'location', 'online', 'start', 'end', 'status')
                result['reservations'] = dict(at=display['at'], stale=(not result['running']
                    or time.time() - display['at'] > 30 or bool(health.get('failure_count'))),
                    items=[{key: row.get(key) for key in fields} for row in display['items']
                           if isinstance(row, dict) and type(row.get('end')) in (int, float)
                           and row['end'] > time.time()])
            if state.get('status') == 'pending':
                result['protections'].append(dict(wid=state.get('activity'), kind='pending',
                    start=state.get('pending_start'), end=state.get('pending_end')))
            holds = state.get('holds')
            for wid, entry in holds.items() if isinstance(holds, dict) else []:
                if isinstance(entry, dict):
                    result['protections'].append(dict(wid=wid,
                        kind='confirmed_hold' if entry.get('confirmed') else 'hold',
                        start=entry.get('start'), end=entry.get('end')))
        except (OSError, ValueError, AttributeError, TypeError):
            result.update(status='invalid', protected=True, health={}, selection=[])
        events = []
        try:
            fd = open_nofollow(self.accounts.state_dir(identifier) / 'events.log', os.O_RDONLY)
            with os.fdopen(fd, 'rb') as source:
                source.seek(0, os.SEEK_END)
                source.seek(max(0, source.tell() - 32768))
                for line in deque(source.read().decode('utf-8', errors='replace').splitlines(), maxlen=30):
                    match = re.fullmatch(r'(\d+) event=([a-z_]{1,80})(?: [a-z_]+=[a-z_0-9.]+)*\n?', line)
                    if match:
                        events.append(dict(at=int(match[1]), event=match[2]))
        except OSError:
            pass
        result['events'] = events
        healthy = not self.closed.is_set() and self.thread is not None and self.thread.is_alive()
        result['supervisor_healthy'] = healthy
        return result

    def test_mail(self, identifier: str) -> None:
        account = self.accounts.get(identifier)
        if not account['mail_enabled']:
            raise ValueError('mail_disabled')
        mailer = Mailer(account_environ(account, self.shared))
        try:
            mailer('test_notification')
        except Exception as exc:
            raise ValueError('mail_' + mail_failure_reason(exc)) from None

    def close(self) -> None:
        self.closed.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        # Wake all accounts first, so ten SMS waits do not turn into ten serial shutdown delays.
        with self.lock:
            identifiers = list(self.workers)
            for worker in self.workers.values():
                worker.stopped.set()
        deadline = time.monotonic() + 60
        try:
            for identifier in identifiers:
                try:
                    self.stop(identifier, preserve=True, timeout=max(0, deadline - time.monotonic()))
                except (OSError, ValueError, sqlite3.Error):
                    # Storage failure must not prevent reaping the other account processes.
                    continue
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
