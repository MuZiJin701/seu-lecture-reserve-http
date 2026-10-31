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


@pytest.mark.parametrize('text, expected', [
    ('SEU_PASSWORD="SIMULATED_SECRET\n', 'event=configuration_or_state_invalid'),
    ('SEU_PREFERRED_LECTURES=SIMULATED_SECRET\n', 'event=configuration_or_state_invalid'),
    ('SEU_REQUIRED_CATEGORIES=\'[]\'\n', 'event=configuration_or_state_invalid'),
    ('SEU_ENABLE_ONLNE=SIMULATED_SECRET\n', 'event=unknown_configuration_key'),
    ('SEU_CARD_NUMBER=test-user\nSEU_PASSWORD=test-password\nSEU_VPN_IP=SIMULATED_SECRET\n',
     'event=configuration_or_state_invalid'),
])
def test_invalid_dotenv_never_echoes_values_or_starts_authentication(tmp_path, text, expected):
    (tmp_path / '.env').write_text(text)
    result = cli(tmp_path, '--check')
    assert result.returncode == 1
    assert result.stdout.splitlines()[-1] == expected
    assert 'SIMULATED_SECRET' not in result.stdout + result.stderr


@pytest.mark.parametrize('args, expected', [
    ((), 1), (('--check',), 1), (('--new-task',), 1), (('--status',), 0), (('--help',), 0),
])
def test_startup_credit_appears_once_except_status_and_help(tmp_path, args, expected):
    result = cli(tmp_path, *args)
    assert result.stdout.count('by MuZiJin701') == expected
    assert result.stdout.count('https://github.com/MuZiJin701/seu-lecture-reserve-http') == expected


def test_example_is_valid_dotenv_with_the_confirmed_default_categories(tmp_path):
    from seu_reserve.cli import configuration_from
    from seu_reserve.model import COFFEE_CATEGORY, GOAL_CATEGORIES
    example = Path(__file__).resolve().parents[1] / '.env.example'
    (tmp_path / '.env').write_text(example.read_text())
    result = cli(tmp_path, '--status')
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == 'status=not_started'
    settings, _ = configuration_from(tmp_path / '.env', {})
    assert settings.categories == GOAL_CATEGORIES + (COFFEE_CATEGORY,)
    assert COFFEE_CATEGORY not in GOAL_CATEGORIES


def test_gateway_ip_is_loaded_from_dotenv_and_process_takes_precedence(tmp_path):
    from seu_reserve.cli import configuration_from

    config = tmp_path / '.env'
    config.write_text('SEU_VPN_IP=192.0.2.1\n')
    assert configuration_from(config, {})[1]['SEU_VPN_IP'] == '192.0.2.1'
    assert configuration_from(config, {'SEU_VPN_IP': '192.0.2.2'})[1]['SEU_VPN_IP'] == '192.0.2.2'


def test_opening_speed_switches_default_on_and_can_be_disabled(tmp_path):
    from seu_reserve.cli import configuration_from

    empty = tmp_path / 'empty.env'
    empty.write_text('')
    defaults, _ = configuration_from(empty, {})
    assert defaults.prewarm is True
    assert defaults.captcha_prefetch is True
    assert defaults.poller_align is False
    assert defaults.history_background is True
    config = tmp_path / '.env'
    config.write_text('SEU_PREWARM=false\nSEU_CAPTCHA_PREFETCH=false\n'
                      'SEU_POLLER_ALIGN=true\nSEU_HISTORY_BACKGROUND=false\n')
    settings, _ = configuration_from(config, {})
    assert settings.prewarm is False
    assert settings.captcha_prefetch is False
    assert settings.poller_align is True
    assert settings.history_background is False
    assert configuration_from(config, {'SEU_PREWARM': 'true'})[0].prewarm is True


def test_trust_device_switch_defaults_off_and_is_opt_in(tmp_path):
    from seu_reserve.cli import configuration_from

    empty = tmp_path / 'empty.env'
    empty.write_text('')
    assert configuration_from(empty, {})[0].trust_device is False
    config = tmp_path / '.env'
    config.write_text('SEU_TRUST_DEVICE=true\n')
    assert configuration_from(config, {})[0].trust_device is True
    # Process environment wins over the dotenv file, like every other switch.
    assert configuration_from(config, {'SEU_TRUST_DEVICE': 'false'})[0].trust_device is False
    invalid = tmp_path / 'invalid.env'
    invalid.write_text('SEU_TRUST_DEVICE=on\n')
    with pytest.raises(ValueError, match='invalid_switch'):
        configuration_from(invalid, {})
    with pytest.raises(ValueError, match='invalid_switch'):
        configuration_from(config, {'SEU_TRUST_DEVICE': 'yes'})


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
        booked = False
        def request(self, method, url, **kwargs):
            if method == 'POST' and url.endswith('casLogin'):
                assert kwargs['json']['username'] == 'test-user'
                decoded = self.key.decrypt(base64.b64decode(kwargs['json']['password']), padding.PKCS1v15()).decode()
                assert decoded == password
            if self.booked and url.endswith('queryActivityList.do'):
                raise OSError('PRIVATE failure after booking')
            if method == 'POST' and url.endswith('addReservation.do'):
                self.booked = True
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
    assert main([]) == 1
    assert school.submitted == ['chosen']
    assert len(messages) == 1
    assert messages[0]['From'] == (mailbox or 'sender@example.org')
    assert messages[0]['To'] == (mailbox or 'receiver@example.org')
    assert password not in capsys.readouterr().out


def test_resolve_pending_requires_an_interactive_terminal(tmp_path, monkeypatch, capsys):
    from seu_reserve.cli import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    assert main(['--resolve-pending']) == 1
    assert 'event=interactive_terminal_required' in capsys.readouterr().out


def test_account_mode_reads_managed_status_and_unknown_account_reports_event(tmp_path):
    from seu_reserve.accounts import AccountInput, Accounts
    (tmp_path / '.env').write_text('SEU_DATA_DIR=data\nSEU_CARD_NUMBER=99999\nSEU_PASSWORD=synthetic\n')
    accounts = Accounts(tmp_path / 'data')
    accounts.create(AccountInput(username='10001', name='测试账号', password='synthetic'))
    result = cli(tmp_path, '--account', '10001', '--status')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == 'status=not_started'
    created = accounts.list()[0]
    by_id = cli(tmp_path, '--account', created['id'], '--status')
    assert by_id.returncode == 0, by_id.stderr
    assert by_id.stdout.splitlines()[0] == 'status=not_started'
    missing = cli(tmp_path, '--account', 'missing', '--status')
    assert missing.returncode == 1
    assert missing.stdout.splitlines()[-1] == 'event=account_not_found'


def test_empty_data_directory_is_rejected(tmp_path):
    (tmp_path / '.env').write_text('SEU_CARD_NUMBER=1\nSEU_PASSWORD=x\nSEU_DATA_DIR=\n')
    result = cli(tmp_path, '--status')
    assert result.returncode == 1
    assert result.stdout.splitlines()[-1] == 'event=invalid_data_directory'


def test_running_checkpoint_blocks_a_second_reservation_loop(tmp_path):
    from seu_reserve.state import Task
    (tmp_path / '.env').write_text('SEU_CARD_NUMBER=1\nSEU_PASSWORD=synthetic\n')
    task = Task(tmp_path / 'state', '1')
    try:
        task.data['status'] = 'scanning'
        task.save()
        result = cli(tmp_path)
        assert result.returncode == 1
        assert result.stdout.splitlines()[-1] == 'event=state_in_use'
    finally:
        task.close()


def test_check_and_resolve_pending_do_not_require_shared_mail_configuration(tmp_path, monkeypatch, capsys):
    from seu_reserve import cli
    from seu_reserve.accounts import AccountInput, Accounts
    (tmp_path / '.env').write_text('SEU_DATA_DIR=data\n', encoding='utf-8')
    Accounts(tmp_path / 'data').create(AccountInput(
        username='10001', name='邮件配置缺失', password='synthetic',
        recipient='dest@example.com', mail_enabled=True))
    captured = {}

    def fake_run(settings, **options):
        captured.update(options)
        return 0

    monkeypatch.setattr(cli, 'run', fake_run)
    assert cli.main(['--config', str(tmp_path / '.env'), '--account', '10001', '--check']) == 0
    assert captured['check_only'] is True
    assert captured['environ']['SEU_USERNAME'] == '10001'
    assert not any(key.startswith(('SMTP_', 'MAIL_')) for key in captured['environ'])

    called = {}

    class TtyLike:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, 'stdin', TtyLike())

    def fake_resolve(settings, **options):
        called.update(options)
        return 0

    monkeypatch.setattr(cli, 'resolve_pending', fake_resolve)
    assert cli.main(['--config', str(tmp_path / '.env'), '--account', '10001', '--resolve-pending']) == 0
    assert called['environ']['SEU_USERNAME'] == '10001'
    assert not any(key.startswith(('SMTP_', 'MAIL_')) for key in called['environ'])

    # A normal start still validates the shared mail configuration.
    assert cli.main(['--config', str(tmp_path / '.env'), '--account', '10001']) == 1
    assert 'event=mail_configuration_invalid' in capsys.readouterr().out


def test_normal_run_holds_the_checkpoint_through_startup_and_blocks_delete(tmp_path, monkeypatch):
    from seu_reserve import cli
    from seu_reserve.accounts import AccountInput, Accounts
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\n', encoding='utf-8')
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(AccountInput(username='10001', name='竞争测试', password='synthetic'))
    seen = {}

    def fake_recognizer():
        # OCR loading happens before the engine starts; the checkpoint lock must already be held.
        with pytest.raises(ValueError, match='state_in_use'):
            accounts.delete(account['id'])
        seen['delete_blocked'] = True
        return lambda _: '1234'

    monkeypatch.setattr(cli, 'load_recognizer', fake_recognizer)

    def fake_run(settings, **options):
        seen['checkpoint'] = options.get('checkpoint')
        seen['check_only'] = options.get('check_only')
        return 0

    monkeypatch.setattr(cli, 'run', fake_run)
    assert cli.main(['--config', str(config), '--account', '10001']) == 0
    assert seen['delete_blocked'] is True
    assert seen['check_only'] is False
    assert seen['checkpoint'] is not None
    assert accounts.get(account['id'])['id'] == account['id']
    seen['checkpoint'].close()


def test_normal_run_rechecks_the_account_before_entering_the_engine(tmp_path, monkeypatch, capsys):
    from seu_reserve import accounts as module
    from seu_reserve import cli
    from seu_reserve.accounts import AccountInput, Accounts
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\n', encoding='utf-8')
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(AccountInput(username='10001', name='已删除测试', password='synthetic'))
    original = module.account_environ

    def deleted_after_lookup(account_data, shared, **options):
        environ = original(account_data, shared, **options)
        with module.closing(accounts.connect()) as db, db:
            db.execute('DELETE FROM accounts WHERE id=?', (account['id'],))
        return environ

    monkeypatch.setattr(module, 'account_environ', deleted_after_lookup)
    called = []
    monkeypatch.setattr(cli, 'run', lambda *args, **options: called.append(options) or 0)
    assert cli.main(['--config', str(config), '--account', '10001']) == 1
    assert 'event=account_not_found' in capsys.readouterr().out
    assert called == []
    assert not accounts.state_dir(account['id']).exists()


def no_recognizer():
    raise ImportError('synthetic missing ddddocr')


def test_ocr_unavailable_removes_a_checkpoint_created_for_a_new_account(tmp_path, monkeypatch, capsys):
    from seu_reserve import cli
    from seu_reserve.accounts import AccountInput, Accounts
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\n', encoding='utf-8')
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(AccountInput(username='10001', name='OCR 不可用', password='synthetic'))
    directory = accounts.state_dir(account['id'])
    assert not directory.exists()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    monkeypatch.setattr(cli, 'load_recognizer', no_recognizer)
    assert cli.main(['--config', str(config), '--account', '10001']) == 1
    assert 'event=ocr_unavailable' in capsys.readouterr().out
    assert not directory.exists()


def test_ocr_unavailable_leaves_an_existing_directory_without_a_task(tmp_path, monkeypatch, capsys):
    from seu_reserve import cli
    from seu_reserve.accounts import AccountInput, Accounts
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\n', encoding='utf-8')
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(AccountInput(username='10001', name='已有目录', password='synthetic'))
    directory = accounts.state_dir(account['id'])
    directory.mkdir()
    (directory / 'events.log').write_text('', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    monkeypatch.setattr(cli, 'load_recognizer', no_recognizer)
    assert cli.main(['--config', str(config), '--account', '10001']) == 1
    assert 'event=ocr_unavailable' in capsys.readouterr().out
    assert directory.exists()
    assert not (directory / 'task.json').exists()
    assert (directory / 'events.log').exists()


def test_ocr_unavailable_keeps_a_preexisting_checkpoint(tmp_path, monkeypatch, capsys):
    from seu_reserve import cli
    from seu_reserve.accounts import AccountInput, Accounts
    from seu_reserve.state import Task
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\n', encoding='utf-8')
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(AccountInput(username='10001', name='已有检查点', password='synthetic'))
    task = Task(accounts.state_dir(account['id']), '10001')
    task.close()
    before = (accounts.state_dir(account['id']) / 'task.json').read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    monkeypatch.setattr(cli, 'load_recognizer', no_recognizer)
    assert cli.main(['--config', str(config), '--account', '10001']) == 1
    assert 'event=ocr_unavailable' in capsys.readouterr().out
    assert (accounts.state_dir(account['id']) / 'task.json').read_bytes() == before


def test_account_mode_forces_the_managed_settings_before_the_engine_starts(tmp_path, monkeypatch):
    from seu_reserve import cli
    from seu_reserve.accounts import AccountInput, Accounts
    from seu_reserve.app import DEFAULT_CATEGORIES
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\n', encoding='utf-8')
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(AccountInput(username='10001', name='托管设置', password='synthetic'))
    legacy = {**account['options'], 'titles': ['旧筛选'], 'trust_device': False, 'prewarm': False,
              'captcha_prefetch': False, 'poller_align': False, 'history_background': False,
              'mail_success': False, 'online': False, 'offline': True}
    with accounts.connect() as db:
        db.execute('UPDATE accounts SET options=? WHERE id=?', (json.dumps(legacy), account['id']))
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    monkeypatch.setattr(cli, 'load_recognizer', lambda: lambda _: '1234')

    def fake_run(settings, **options):
        captured.update(settings=settings, options=options)
        return 0

    monkeypatch.setattr(cli, 'run', fake_run)
    assert cli.main(['--config', str(config), '--account', '10001']) == 0
    settings = captured['settings']
    assert settings.titles == ()
    assert all(getattr(settings, key) is True for key in
               ('trust_device', 'prewarm', 'captcha_prefetch', 'poller_align',
                'history_background', 'mail_success'))
    assert settings.categories == DEFAULT_CATEGORIES
    assert settings.online is False and settings.offline is True
    captured['options']['checkpoint'].close()


def test_account_mode_rereads_configuration_after_taking_the_lock(tmp_path, monkeypatch):
    from seu_reserve import cli
    from seu_reserve.accounts import AccountInput, Accounts
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\n', encoding='utf-8')
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(AccountInput(username='10001', name='重读配置', password='synthetic'))
    stale = dict(account)
    stale['options'] = {**account['options'], 'online': True, 'offline': True}
    fresh = dict(account)
    fresh['options'] = {**account['options'], 'online': False, 'offline': True}
    monkeypatch.setattr(Accounts, 'list', lambda self: [stale])
    monkeypatch.setattr(Accounts, 'get', lambda self, identifier: fresh)
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    monkeypatch.setattr(cli, 'load_recognizer', lambda: lambda _: '1234')

    def fake_run(settings, **options):
        captured.update(settings=settings, options=options)
        return 0

    monkeypatch.setattr(cli, 'run', fake_run)
    assert cli.main(['--config', str(config), '--account', '10001']) == 0
    assert captured['settings'].online is False and captured['settings'].offline is True
    captured['options']['checkpoint'].close()
