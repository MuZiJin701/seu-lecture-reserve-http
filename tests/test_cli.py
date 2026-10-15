import subprocess
import sys
import json


def cli(directory, *args):
    return subprocess.run([sys.executable, '-m', 'seu_reserve.cli', *args], cwd=directory,
                          env={}, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)


def test_status_and_help_do_not_require_credentials_or_network(tmp_path):
    assert cli(tmp_path, '--help').returncode == 0
    status = cli(tmp_path, '--status')
    assert status.returncode == 0
    assert status.stdout.strip() == 'status=not_started'
    assert not (tmp_path / 'state').exists()


def test_noninteractive_missing_credentials_fail_without_unsafe_prompt(tmp_path):
    result = cli(tmp_path)
    assert result.returncode == 1
    assert result.stdout.strip() == 'event=credentials_require_terminal'
    assert 'GetPassWarning' not in result.stderr


def test_invalid_configuration_is_rejected_without_echoing_its_value(tmp_path):
    config = tmp_path / 'filters.toml'
    config.write_text('online = "SECRET invalid switch"\n')
    result = cli(tmp_path, '--config', str(config))
    assert result.returncode == 1
    assert 'SECRET' not in result.stdout + result.stderr


def test_completed_task_restart_does_not_prompt_for_credentials(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'task.json').write_text(json.dumps({'version': 1, 'account': '0' * 64,
        'status': 'done', 'activity': 'one', 'cooldowns': {}}))
    assert cli(tmp_path).returncode == 0
