import subprocess
import sys
import json
from pathlib import Path

import pytest


def cli(directory, *args, environment=None):
    entrypoint = Path(__file__).resolve().parents[1] / 'main.py'
    return subprocess.run([sys.executable, str(entrypoint), *args], cwd=directory,
                          env=environment or {}, input='', capture_output=True, text=True, timeout=10)


def test_status_and_help_do_not_require_credentials_or_network(tmp_path):
    assert cli(tmp_path, '--help').returncode == 0
    status = cli(tmp_path, '--status')
    assert status.returncode == 0
    assert status.stdout.splitlines() == ['status=not_started', 'process_running=false', 'phase=unknown']
    assert not (tmp_path / 'state').exists()


def test_cli_starts_without_system_timezone_data(tmp_path):
    result = cli(tmp_path, '--help', environment={'PYTHONTZPATH': ''})
    assert result.returncode == 0, result.stderr
    assert '--check' in result.stdout


def test_cli_help_does_not_import_unix_only_locking(tmp_path):
    result = subprocess.run([sys.executable, '-c',
        "import runpy, sys; sys.modules['fcntl'] = None; "
        "sys.argv = ['seu-reserve', '--help']; runpy.run_module('seu_reserve.cli', run_name='__main__')"],
        cwd=tmp_path, env={}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert '--check' in result.stdout


def test_noninteractive_missing_credentials_fail_without_unsafe_prompt(tmp_path):
    result = cli(tmp_path)
    assert result.returncode == 1
    assert result.stdout.splitlines()[-1] == 'event=credentials_require_terminal'
    assert 'GetPassWarning' not in result.stderr


def test_invalid_configuration_is_rejected_without_echoing_its_value(tmp_path):
    config = tmp_path / 'custom.env'
    config.write_text('SEU_ENABLE_ONLINE="SECRET invalid switch"\n')
    result = cli(tmp_path, '--config', str(config))
    assert result.returncode == 1
    assert 'SECRET' not in result.stdout + result.stderr


def test_completed_task_restart_requires_credentials_to_recheck_school(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'task.json').write_text(json.dumps({'version': 2, 'account': '0' * 64,
        'status': 'done', 'activity': 'one', 'cooldowns': {}}))
    result = cli(tmp_path)
    assert result.returncode == 1
    assert 'credentials_require_terminal' in result.stdout


def test_legacy_single_success_requires_credentials_to_recheck_goal(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    saved = json.dumps({'version': 1, 'account': '0' * 64,
        'status': 'done', 'activity': 'one', 'cooldowns': {}})
    (state / 'task.json').write_text(saved)
    assert cli(tmp_path, '--status').stdout.splitlines()[0] == 'status=legacy_completed'
    assert cli(tmp_path).returncode == 1
    assert (state / 'task.json').read_text() == saved


def test_dotenv_is_loaded_automatically_and_controls_state_directory(tmp_path):
    (tmp_path / '.env').write_text('SEU_STATE_DIR=jobs\n')
    state = tmp_path / 'jobs'
    state.mkdir()
    (state / 'task.json').write_text(json.dumps({'version': 2, 'account': '0' * 64,
        'status': 'done', 'activity': 'one', 'cooldowns': {}}))
    result = cli(tmp_path, '--status')
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == 'status=done'


def test_environment_overrides_dotenv_and_explicit_path_does_not_search_other_files(tmp_path):
    (tmp_path / '.env').write_text('SEU_ENABLE_ONLINE=invalid\n')
    private = tmp_path / 'configuration'
    private.mkdir()
    selected = private / '.env'
    selected.write_text('SEU_ENABLE_ONLINE=false\nSEU_STATE_DIR=jobs\n')
    state = private / 'overridden'
    state.mkdir()
    (state / 'task.json').write_text('{"status":"done"}')
    result = cli(tmp_path, '--config', str(selected), '--status', environment={'SEU_STATE_DIR': 'overridden'})
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == 'status=done'


@pytest.mark.parametrize('environment, expected', [
    ({'SEU_USERNAME': 'legacy-user'}, 'legacy-user'),
    ({'SEU_CARD_NUMBER': 'canonical-user', 'SEU_USERNAME': 'legacy-user'}, 'canonical-user'),
])
def test_process_account_aliases_override_file_and_resume_the_correct_account(tmp_path, environment, expected):
    import hashlib
    (tmp_path / '.env').write_text('SEU_CARD_NUMBER=file-user\n')
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'task.json').write_text(json.dumps({'version': 2,
        'account': hashlib.sha256(expected.encode()).hexdigest(), 'status': 'done',
        'activity': 'one', 'cooldowns': {}}))
    from seu_reserve.cli import configuration_from
    assert configuration_from(tmp_path / '.env', environment)[1]['SEU_USERNAME'] == expected
    before = (state / 'task.json').read_text()
    assert cli(tmp_path, environment=environment).returncode == 1
    assert (state / 'task.json').read_text() == before


@pytest.mark.parametrize('text', [
    'SEU_PASSWORD="SIMULATED_SECRET\n',
    'SEU_PREFERRED_LECTURES=SIMULATED_SECRET\n',
    'SEU_REQUIRED_CATEGORIES=\'[]\'\n',
    'SEU_ENABLE_ONLNE=SIMULATED_SECRET\n',
    'SEU_CARD_NUMBER=test-user\nSEU_PASSWORD=test-password\nSEU_VPN_IP=SIMULATED_SECRET\n',
])
def test_invalid_dotenv_never_echoes_values_or_starts_authentication(tmp_path, text):
    (tmp_path / '.env').write_text(text)
    result = cli(tmp_path, '--check')
    assert result.returncode == 1
    assert result.stdout.splitlines()[-1] == 'event=configuration_or_state_invalid'
    assert 'SIMULATED_SECRET' not in result.stdout + result.stderr


@pytest.mark.parametrize('args, expected', [
    ((), 1), (('--check',), 1), (('--new-task',), 1), (('--status',), 0), (('--help',), 0),
])
def test_startup_credit_appears_once_except_status_and_help(tmp_path, args, expected):
    result = cli(tmp_path, *args)
    assert result.stdout.count('by MuZiJin701') == expected
    assert result.stdout.count('https://github.com/MuZiJin701/seu-lecture-reserve-http') == expected


def test_example_is_valid_dotenv_with_the_confirmed_default_categories(tmp_path):
    example = Path(__file__).resolve().parents[1] / '.env.example'
    (tmp_path / '.env').write_text(example.read_text())
    result = cli(tmp_path, '--status')
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == 'status=not_started'


def test_gateway_ip_is_loaded_from_dotenv_and_process_takes_precedence(tmp_path):
    from seu_reserve.cli import configuration_from

    config = tmp_path / '.env'
    config.write_text('SEU_VPN_IP=192.0.2.1\n')
    assert configuration_from(config, {})[1]['SEU_VPN_IP'] == '192.0.2.1'
    assert configuration_from(config, {'SEU_VPN_IP': '192.0.2.2'})[1]['SEU_VPN_IP'] == '192.0.2.2'


@pytest.mark.parametrize('configuration, outcome', [
    ('', 'mail_disabled'),
    ('SMTP_USERNAME=sender@gmail.com\n', 'mail_configuration_invalid'),
    ('SMTP_USERNAME=sender@gmail.com\nSMTP_PASSWORD=SECRET\n', 'test_mail_accepted'),
])
def test_test_mail_needs_no_school_credentials_or_task(tmp_path, monkeypatch, capsys, configuration, outcome):
    from seu_reserve.cli import main
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    (tmp_path / '.env').write_text(configuration)
    def send(self, event):
        calls.append(event)
    monkeypatch.setattr('seu_reserve.mail.Mailer.__call__', send)
    def school_forbidden():
        pytest.fail('test mail must not access the school')
    monkeypatch.setattr('requests.Session', school_forbidden)
    assert main(['--test-mail']) == (0 if outcome == 'test_mail_accepted' else 1)
    assert calls == (['test_notification'] if outcome == 'test_mail_accepted' else [])
    output = capsys.readouterr().out
    assert 'event=' + outcome in output
    assert 'SECRET' not in output
    assert not (tmp_path / 'state').exists()


def test_test_mail_failure_is_safe_and_does_not_create_task(tmp_path, monkeypatch, capsys):
    from seu_reserve.cli import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    (tmp_path / '.env').write_text('SMTP_USERNAME=sender@gmail.com\nSMTP_PASSWORD=SECRET\n')
    def fail(self, event):
        raise OSError('SECRET server reply')
    monkeypatch.setattr('seu_reserve.mail.Mailer.__call__', fail)
    assert main(['--test-mail']) == 1
    output = capsys.readouterr().out
    assert 'event=notification_failed' in output and 'SECRET' not in output
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('mailbox, host', [
    (None, 'smtp.example.org'), ('sender@gmail.com', 'smtp.gmail.com'),
    ('sender@QQ.COM', 'smtp.qq.com'), ('sender@163.com', 'smtp.163.com'),
    ('sender@126.com', 'smtp.126.com'),
])
def test_one_dotenv_supplies_credentials_filters_and_mail_without_shell_exports(
        tmp_path, monkeypatch, capsys, mailbox, host):
    import base64
    from types import SimpleNamespace
    from cryptography.hazmat.primitives.asymmetric import padding
    from seu_reserve.cli import main
    from test_app import School, lecture, interrupted
    from seu_reserve.app import run as run_application

    password = 'test ${literal} # = password'
    class ConfiguredSchool(School):
        def request(self, method, url, **kwargs):
            if method == 'POST' and url.endswith('casLogin'):
                assert kwargs['json']['username'] == 'test-user'
                decoded = self.key.decrypt(base64.b64decode(kwargs['json']['password']), padding.PKCS1v15()).decode()
                assert decoded == password
            return super().request(method, url, **kwargs)
    school = ConfiguredSchool()
    school.activities = [lecture('skip', HDMC='不匹配'),
                         lecture('chosen', HDMC='指定讲座', IS_ONLINE=False, JZDD='无锡校区会议室')]
    messages = []
    class SMTP:
        def __init__(self, server, port, **kwargs):
            assert (server, port) == (host, 465)
            assert kwargs['context'].check_hostname
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def login(self, username, secret):
            assert username == (mailbox or 'mail-user')
            assert secret == 'test ${mail} # = password'
        def send_message(self, message):
            messages.append(message)
    (tmp_path / '.env').write_text(
        "SEU_CARD_NUMBER=test-user\nSEU_PASSWORD='test ${literal} # = password'\n"
        "SEU_PREFERRED_LECTURES='[\"指定讲座\"]'\nSEU_PREFERRED_CAMPUS='[\"无锡校区\"]'\n"
        "SEU_ENABLE_ONLINE=false\nSEU_ENABLE_OFFLINE=true\nSEU_STATE_DIR=jobs\n" +
        (f"SMTP_USERNAME={mailbox}\nSMTP_PASSWORD='test ${{mail}} # = password'\n"
         "SMTP_HOST=\nSMTP_PORT=\nMAIL_FROM=\nMAIL_TO=\n" if mailbox else
         "SMTP_HOST=smtp.example.org\nSMTP_USERNAME=mail-user\nSMTP_PASSWORD='test ${mail} # = password'\n"
         "MAIL_FROM=sender@example.org\nMAIL_TO=receiver@example.org\n"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    monkeypatch.setattr('requests.Session', lambda: school)
    monkeypatch.setattr('smtplib.SMTP_SSL', SMTP)
    monkeypatch.setattr('ddddocr.DdddOcr', lambda **kwargs: SimpleNamespace(classification=lambda _: '1234'))
    school.close = lambda: None
    monkeypatch.setattr('seu_reserve.cli.run', lambda settings, **options:
                        run_application(settings, **options, sleep=interrupted, prompt=interrupted, clock=lambda: 1000))
    assert main([]) == 130
    assert school.submitted == ['chosen']
    assert len(messages) == 1
    assert messages[0]['From'] == (mailbox or 'sender@example.org')
    assert messages[0]['To'] == (mailbox or 'receiver@example.org')
    assert password not in capsys.readouterr().out
