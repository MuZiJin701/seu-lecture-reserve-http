"""Terminal interface with one dotenv file for credentials and settings."""

import argparse
import json
import os
import re
import signal
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

from .app import SELECTION_REASONS, Settings, run
from .mail import Mailer, mail_failure_reason, mail_history
from .model import SHANGHAI
from .state import read_json, task_running


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
               'SEU_ENABLE_OFFLINE', 'SEU_STATE_DIR', 'SMTP_HOST', 'SMTP_PORT',
               'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAIL_FROM', 'MAIL_TO', 'SEU_VPN_IP'}
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
    for key, name in (('online', 'SEU_ENABLE_ONLINE'), ('offline', 'SEU_ENABLE_OFFLINE')):
        raw = environ.get(name, str(getattr(defaults, key))).strip().lower()
        if raw not in ('true', 'false'):
            raise ValueError('invalid_switch')
        data[key] = raw == 'true'
    state_dir = environ.get('SEU_STATE_DIR', 'state')
    if not state_dir.strip():
        raise ValueError('invalid_state_directory')
    data['state_dir'] = source.parent / state_dir
    return Settings(**data), environ


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='SEU lecture goal: 8 attended, including law, art and mental health')
    parser.add_argument('--config', type=Path, help='dotenv file (default: .env in the current directory)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='authenticate, explain lecture selection and fetch captcha without submitting or saving a task')
    mode.add_argument('--new-task', action='store_true', help='compatibility: recheck a completed goal (normal startup also rechecks)')
    mode.add_argument('--status', action='store_true', help='show local task status and private selection details without network access')
    mode.add_argument('--test-mail', action='store_true', help='send one test email without school login or reservations')
    args = parser.parse_args(argv)
    if not args.status:
        print('SEU Lecture Reserve HTTPS | by MuZiJin701\n'
              'https://github.com/MuZiJin701/seu-lecture-reserve-http', flush=True)
    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        settings, environ = configuration_from(args.config, os.environ)
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
        if not sys.stdin.isatty() and any(not environ.get(key) for key in ('SEU_USERNAME', 'SEU_PASSWORD')):
            print('event=credentials_require_terminal', flush=True)
            return 1
        for key, label in (('SEU_USERNAME', '一卡通号：'), ('SEU_PASSWORD', '密码：')):
            if not environ.get(key):
                environ[key] = getpass(label)
        return run(settings, environ=environ, check_only=args.check, new_task=args.new_task)
    except (KeyboardInterrupt, EOFError):
        return 130
    except (ValueError, OSError, TypeError, AttributeError):
        print('event=configuration_or_state_invalid', flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
