"""Terminal interface; configuration never loads school credentials from files."""

import argparse
import json
import os
import sys
import tomllib
from getpass import getpass
from pathlib import Path

from .app import Settings, run


def settings_from(path: Path | None) -> Settings:
    if path is None:
        return Settings()
    with path.open('rb') as source:
        data = tomllib.load(source)
    allowed = {'titles', 'categories', 'campuses', 'online', 'offline', 'state_dir'}
    if set(data) - allowed:
        raise ValueError('unknown_configuration_key')
    defaults = Settings()
    for key in ('titles', 'categories', 'campuses'):
        value = data.get(key, list(getattr(defaults, key)))
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError('invalid_filter_list')
        data[key] = tuple(value)
    if not data['categories'] or len(set(data['categories'])) != len(data['categories']):
        raise ValueError('invalid_categories')
    for key in ('online', 'offline'):
        if key in data and type(data[key]) is not bool:
            raise ValueError('invalid_switch')
    state_dir = data.get('state_dir', 'state')
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ValueError('invalid_state_directory')
    data['state_dir'] = path.parent / state_dir
    return Settings(**data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='SEU HTTPS lecture reservation; one success per task')
    parser.add_argument('--config', type=Path, help='non-secret TOML configuration')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='authenticate and fetch data/captcha without submitting')
    mode.add_argument('--new-task', action='store_true', help='explicitly replace a completed task')
    mode.add_argument('--status', action='store_true', help='show local task status without network access')
    args = parser.parse_args(argv)
    try:
        settings = settings_from(args.config)
        if args.status:
            path = settings.state_dir / 'task.json'
            if path.is_symlink():
                raise ValueError('unsafe_state')
            status = json.loads(path.read_text()).get('status') if path.exists() else 'not_started'
            if status not in ('not_started', 'scanning', 'pending', 'done'):
                raise ValueError('invalid_state')
            print('status=' + status)
            return 0
        environ = dict(os.environ)
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
