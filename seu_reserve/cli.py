"""Terminal interface with one dotenv file for credentials and settings."""

import argparse
import json
import os
import sys
from collections.abc import Mapping
from getpass import getpass
from io import StringIO
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from .app import Settings, run


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
               'SMTP_USERNAME', 'SMTP_PASSWORD', 'MAIL_FROM', 'MAIL_TO'}
    if set(values) - allowed:
        raise ValueError('unknown_configuration_key')
    environ = {**values, **process}
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
    parser = argparse.ArgumentParser(description='SEU HTTPS lecture reservation; one success per task')
    parser.add_argument('--config', type=Path, help='dotenv file (default: .env in the current directory)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='authenticate and fetch data/captcha without submitting')
    mode.add_argument('--new-task', action='store_true', help='explicitly replace a completed task')
    mode.add_argument('--status', action='store_true', help='show local task status without network access')
    args = parser.parse_args(argv)
    try:
        settings, environ = configuration_from(args.config, os.environ)
        if args.status:
            path = settings.state_dir / 'task.json'
            if path.is_symlink():
                raise ValueError('unsafe_state')
            status = json.loads(path.read_text()).get('status') if path.exists() else 'not_started'
            if status not in ('not_started', 'scanning', 'pending', 'done'):
                raise ValueError('invalid_state')
            print('status=' + status)
            return 0
        checkpoint = settings.state_dir / 'task.json'
        if not args.check and not args.new_task and checkpoint.exists():
            if checkpoint.is_symlink():
                raise ValueError('unsafe_state')
            saved = json.loads(checkpoint.read_text())
            if isinstance(saved, dict) and saved.get('status') == 'done':
                return run(settings, environ=environ)
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


if __name__ == '__main__':
    raise SystemExit(main())
