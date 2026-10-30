"""Terminal interface with one dotenv file for credentials and settings."""

import argparse
import json
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from getpass import getpass
from io import StringIO
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from .app import SELECTION_REASONS, Settings, resolve_pending, run
from .mail import Mailer, mail_failure_reason, mail_history
from .model import SHANGHAI
from .state import Task, read_json, task_running


def show_status(directory: Path) -> None:
    try:
        saved = read_json(directory / 'task.json')
    except FileNotFoundError:
        saved = {}
    if not isinstance(saved, dict):
        raise ValueError('invalid_state')
    status = saved.get('status', 'not_started')
    if status not in ('not_started', 'scanning', 'pending', 'done'):
        raise ValueError('invalid_state')
    if saved.get('version') == 1 and status == 'done':
        status = 'legacy_completed'
    print('status=' + status)
    running = task_running(directory)
    print('process_running=' + str(running).lower())
    snapshot = saved.get('health')
    # 'preflight' stays accepted for state files written before the pre-check was removed.
    phases = {'starting', 'authentication', 'my_activities', 'activities', 'preflight', 'captcha',
              'submitting', 'confirming', 'waiting', 'waiting_for_attendance', 'manual_required',
              'backoff', 'done', 'stopped', 'failed'}
    timestamps = ('updated_at', 'last_query_at', 'last_error_at', 'next_check_at', 'last_mail_at')
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('phase'), str)
            or snapshot['phase'] not in phases
            or snapshot.get('mail_status') not in ('disabled', 'ready', 'sending', 'accepted', 'failed')
            or not isinstance(snapshot.get('last_error'), str)
            or not re.fullmatch('[a-z_]{1,80}', snapshot['last_error'])
            or any(type(snapshot.get(key)) not in (int, float) or not 0 <= snapshot[key] <= 253402214400 for key in timestamps)
            or type(snapshot.get('failure_count')) is not int or snapshot['failure_count'] < 0
            or any(snapshot.get(key) is not None and (type(snapshot[key]) is not int or snapshot[key] < 0)
                   for key in ('activity_count', 'candidate_count'))):
        print('phase=unknown')
        return
    try:
        history = mail_history(snapshot)
    except ValueError:
        print('phase=unknown')
        return
    print('phase=' + (snapshot['phase'] if running else 'stopped'))
    if not running:
        print('last_phase=' + snapshot['phase'])
    for key in timestamps:
        value = snapshot[key]
        print(key + '=' + (datetime.fromtimestamp(value, SHANGHAI).isoformat(timespec='seconds') if value else 'never'))
    for key, label in (('updated_at', 'snapshot_age_seconds'), ('last_query_at', 'last_query_age_seconds')):
        print(label + '=' + (str(max(0, round(time.time() - snapshot[key]))) if snapshot[key] else 'unknown'))
    for key in ('last_error', 'failure_count', 'mail_status', 'activity_count', 'candidate_count'):
        print(key + '=' + (str(snapshot[key]) if snapshot.get(key) is not None else 'unknown'))
    for key in ('last_mail_status', 'last_mail_error', 'last_mail_event', 'last_mail_duration_seconds'):
        print(key + '=' + (str(history[key]) if history[key] is not None else 'unknown'))
    selection = saved.get('selection')
    if selection is None:
        print('selection_updated_at=unknown')
        return
    if (not isinstance(selection, dict) or type(selection.get('updated_at')) not in (int, float)
            or not 0 <= selection['updated_at'] <= 253402214400
            or not isinstance(selection.get('activities'), list)
            or any(not isinstance(item, dict) or not isinstance(item.get('title'), str)
                   or not isinstance(item.get('reason'), str) or item['reason'] not in SELECTION_REASONS
                   or any(item.get(key) is not None and (not isinstance(item[key], str)
                          or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00', item[key]))
                          for key in ('reservation_start', 'reservation_end'))
                   for item in selection['activities'])):
        print('selection_updated_at=unknown')
        print('activity_details=unknown')
        return
    print('selection_updated_at=' + datetime.fromtimestamp(selection['updated_at'], SHANGHAI).isoformat(timespec='seconds'))
    details = [{key: item.get(key) for key in ('title', 'reason', 'reservation_start', 'reservation_end')}
               for item in selection['activities']]
    print('activity_details=' + json.dumps(details, ensure_ascii=False))


def configuration_from(path: Path | None, process: Mapping[str, str]) -> tuple[Settings, dict[str, str]]:
    source = path if path is not None else Path('.env')
    values: dict[str, str] = {}
    if path is not None or source.exists():
        text = source.read_text(encoding='utf-8')
        if any(binding.error for binding in parse_stream(StringIO(text))):
            raise ValueError('invalid_dotenv_syntax')
        values = {key: value if value is not None else ''
                  for key, value in dotenv_values(stream=StringIO(text), interpolate=False).items()}
    allowed = {'SEU_CARD_NUMBER', 'SEU_USERNAME', 'SEU_PASSWORD', 'SEU_PREFERRED_LECTURES',
               'SEU_REQUIRED_CATEGORIES', 'SEU_PREFERRED_CAMPUS', 'SEU_ENABLE_ONLINE',
               'SEU_ENABLE_OFFLINE', 'SEU_STATE_DIR', 'SEU_MAIL_SUCCESS', 'SMTP_HOST', 'SMTP_PORT',
               'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAIL_FROM', 'MAIL_TO', 'SEU_VPN_IP',
               'SEU_CAPTCHA_PREFETCH', 'SEU_PREWARM', 'SEU_POLLER_ALIGN',
               'SEU_HISTORY_BACKGROUND', 'SEU_TRUST_DEVICE', 'SEU_DATA_DIR',
               'SEU_WEB_HOST', 'SEU_WEB_PORT', 'SEU_WEB_ADMINS'}
    if set(values) - allowed:
        raise ValueError('unknown_configuration_key')
    environ = {**values, **process}
    if address := environ.get('SEU_VPN_IP', '').strip():
        ip_address(address)
    for layer in (values, process):
        if 'SEU_CARD_NUMBER' in layer:
            environ['SEU_USERNAME'] = layer['SEU_CARD_NUMBER']
        elif 'SEU_USERNAME' in layer:
            environ['SEU_USERNAME'] = layer['SEU_USERNAME']
    defaults = Settings()
    data: dict[str, Any] = {}
    for key, name in (('titles', 'SEU_PREFERRED_LECTURES'), ('categories', 'SEU_REQUIRED_CATEGORIES'),
                      ('campuses', 'SEU_PREFERRED_CAMPUS')):
        value = json.loads(environ[name]) if name in environ else list(getattr(defaults, key))
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError('invalid_filter_list')
        data[key] = tuple(value)
    if not data['categories'] or len(set(data['categories'])) != len(data['categories']):
        raise ValueError('invalid_categories')
    for key, name in (('online', 'SEU_ENABLE_ONLINE'), ('offline', 'SEU_ENABLE_OFFLINE'),
                      ('mail_success', 'SEU_MAIL_SUCCESS'),
                      ('captcha_prefetch', 'SEU_CAPTCHA_PREFETCH'), ('prewarm', 'SEU_PREWARM'),
                      ('poller_align', 'SEU_POLLER_ALIGN'),
                      ('history_background', 'SEU_HISTORY_BACKGROUND'),
                      ('trust_device', 'SEU_TRUST_DEVICE')):
        raw = environ.get(name, str(getattr(defaults, key))).strip().lower()
        if raw not in ('true', 'false'):
            raise ValueError('invalid_switch')
        data[key] = raw == 'true'
    state_dir = environ.get('SEU_STATE_DIR', 'state')
    if not state_dir.strip():
        raise ValueError('invalid_state_directory')
    data['state_dir'] = source.parent / state_dir
    return Settings(**data), environ


def data_root_from(source: Path, environ: Mapping[str, str]) -> Path:
    """Resolve the shared data root used by the web catalogue and `--account` CLI."""
    value = environ.get('SEU_DATA_DIR', environ.get('SEU_STATE_DIR', 'state')).strip()
    if not value:
        raise ValueError('invalid_data_directory')
    return source.parent / value


def load_recognizer() -> Any:
    """Load OCR before any opening window; a first-candidate import would spend opening seconds."""
    import ddddocr
    return ddddocr.DdddOcr(show_ad=False).classification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='SEU lecture goal: 8 attended, including law, art and mental health')
    parser.add_argument('--config', type=Path, help='dotenv file (default: .env in the current directory)')
    parser.add_argument('--account', help='managed school account (card number or internal ID)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='authenticate, explain lecture selection and fetch captcha without submitting or saving a task')
    mode.add_argument('--new-task', action='store_true', help='compatibility: recheck a completed goal (normal startup also rechecks)')
    mode.add_argument('--status', action='store_true', help='show local task status and private selection details without network access')
    mode.add_argument('--test-mail', action='store_true', help='send one test email without school login or reservations')
    mode.add_argument('--resolve-pending', action='store_true',
                      help='interactively verify an unconfirmed submission and clear it only after explicit confirmation')
    args = parser.parse_args(argv)
    if not args.status:
        print('SEU Lecture Reserve HTTPS | by MuZiJin701\n'
              'https://github.com/MuZiJin701/seu-lecture-reserve-http', flush=True)
    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        settings, environ = configuration_from(args.config, os.environ)
        from .accounts import Accounts, account_environ, account_settings
        source = args.config or Path('.env')
        data_root = data_root_from(source, environ)
        account_identifier = ''
        if args.account:
            catalogue = Accounts(data_root)
            entries = catalogue.list()
            # Prefer the internal ID so a card number that happens to look like another account's
            # ID can never select the wrong account.
            account = next((entry for entry in entries if entry['id'] == args.account), None)
            if account is None:
                account = next((entry for entry in entries if entry['username'] == args.account), None)
            if account is None or account['deleting']:
                raise ValueError('account_not_found')
            account_identifier = account['id']
            settings = account_settings(account, catalogue.state_dir(account['id']))
            if not args.status:
                # --check and --resolve-pending never send mail; do not let a broken shared SMTP
                # configuration block read-only checks or pending resolution.
                environ = account_environ(account, environ,
                                          mail=not (args.check or args.resolve_pending))
        if args.status:
            show_status(settings.state_dir)
            return 0
        if args.test_mail:
            if not Mailer.configured(environ):
                print('event=mail_disabled', flush=True)
                return 1
            try:
                mailer = Mailer(environ)
            except ValueError:
                print('event=mail_configuration_invalid', flush=True)
                return 1
            try:
                mailer('test_notification')
            except Exception as exc:
                print('event=notification_failed reason=' + mail_failure_reason(exc), flush=True)
                return 1
            print('event=test_mail_accepted', flush=True)
            return 0
        if args.resolve_pending and not sys.stdin.isatty():
            print('event=interactive_terminal_required', flush=True)
            return 1
        if not sys.stdin.isatty() and any(not environ.get(key) for key in ('SEU_USERNAME', 'SEU_PASSWORD')):
            print('event=credentials_require_terminal', flush=True)
            return 1
        for key, label in (('SEU_USERNAME', '一卡通号：'), ('SEU_PASSWORD', '密码：')):
            if not environ.get(key):
                environ[key] = getpass(label)
        if not args.account and (data_root / 'accounts.sqlite3').exists():
            if any(entry['username'] == environ['SEU_USERNAME'] for entry in Accounts(data_root).list()):
                print('event=account_managed_use_account', flush=True)
                return 1
        if not args.check and task_running(settings.state_dir):
            # Another reservation loop owns this checkpoint; never start a second writer.
            print('event=state_in_use', flush=True)
            return 1
        if args.resolve_pending:
            return resolve_pending(settings, environ=environ)
        account_task: Task | None = None
        had_directory = had_task = False
        if args.account and not args.check:
            # Hold the checkpoint lock from the lifecycle re-check through engine startup (OCR
            # loading included), so a delete cannot remove the account between the catalogue read
            # and the engine's own checkpoint open. The engine reuses this locked checkpoint.
            had_directory = settings.state_dir.exists()
            had_task = (settings.state_dir / 'task.json').exists()
            account_task = Task(settings.state_dir, environ['SEU_USERNAME'])
            try:
                current = Accounts(data_root).get(account_identifier)
            except ValueError:
                current = None
            if current is None or current['deleting']:
                account_task.close()
                account_task = None
                if not had_directory:
                    shutil.rmtree(settings.state_dir, ignore_errors=True)
                elif not had_task:
                    (settings.state_dir / 'task.json').unlink(missing_ok=True)
                print('event=account_not_found', flush=True)
                return 1
        recognize = None
        if not args.check:
            try:
                recognize = load_recognizer()
            except Exception:
                if account_task is not None:
                    # Do not leave a checkpoint behind for an account that never started.
                    account_task.close()
                    account_task = None
                    if not had_directory:
                        shutil.rmtree(settings.state_dir, ignore_errors=True)
                    elif not had_task:
                        (settings.state_dir / 'task.json').unlink(missing_ok=True)
                print('event=ocr_unavailable', flush=True)
                return 1
        try:
            return run(settings, environ=environ, check_only=args.check, new_task=args.new_task,
                       recognize=recognize, checkpoint=account_task)
        finally:
            # run() owns and closes the checkpoint from the moment it takes over.
            account_task = None
    except (KeyboardInterrupt, EOFError):
        return 130
    except (ValueError, OSError, TypeError, AttributeError, sqlite3.Error) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else ''
        events = {'account_not_found', 'state_in_use', 'mail_configuration_invalid',
                  'unknown_configuration_key', 'invalid_data_directory', 'invalid_state'}
        print('event=' + (reason if reason in events else 'configuration_or_state_invalid'), flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
