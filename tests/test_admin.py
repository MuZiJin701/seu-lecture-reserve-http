import json

import pytest

from seu_reserve.admin import Administrators, HASH_PATTERN, SESSION_SECONDS, hash_password


@pytest.fixture(scope='module')
def shared():
    return {'SEU_WEB_ADMINS': json.dumps({'alice': hash_password('synthetic-password')})}


def test_hash_has_random_salt_and_validates_password(shared):
    first = json.loads(shared['SEU_WEB_ADMINS'])['alice']
    second = hash_password('synthetic-password')
    assert first != second and HASH_PATTERN.fullmatch(second)
    auth = Administrators(shared)
    with pytest.raises(ValueError, match='login_rejected'):
        auth.login('alice', 'wrong', 'local')
    with pytest.raises(ValueError, match='login_rejected'):
        auth.login('unknown', 'synthetic-password', 'local')
    token, session = auth.login('alice', 'synthetic-password', 'local')
    assert auth.session(token) == session and session['username'] == 'alice'


@pytest.mark.parametrize('raw', ['{}', '[]', '{', '{"a":""}', '{"a":null}',
                                  json.dumps({'a': 'x' * 1025}), '{"a":"pbkdf2_sha256$broken"}',
                                  '{"a":"one","a":"two"}'])
def test_invalid_or_missing_admins_never_enable_anonymous_access(raw):
    with pytest.raises(ValueError, match='admin_configuration_invalid'):
        Administrators({'SEU_WEB_ADMINS': raw})


def test_plaintext_and_hash_admins_load_from_dotenv_without_rewriting(tmp_path, shared):
    from seu_reserve.cli import configuration_from

    password = "测试 # ${KEEP_LITERAL} $ space \"quote\" 'single' \\path"
    admins = {'plain': password, 'alice': json.loads(shared['SEU_WEB_ADMINS'])['alice']}
    config = tmp_path / '.env'
    encoded = json.dumps(admins, ensure_ascii=False).replace('\\', '\\\\').replace("'", "\\'")
    original = "SEU_WEB_ADMINS='" + encoded + "'\n"
    config.write_text(original, encoding='utf-8')
    _, loaded = configuration_from(config, {})
    auth = Administrators(loaded)
    assert all(HASH_PATTERN.fullmatch(value) for value in auth.admins.values())
    assert auth.admins['plain'] != password and config.read_text(encoding='utf-8') == original
    assert auth.login('plain', password, 'local')[1]['username'] == 'plain'
    assert auth.login('alice', 'synthetic-password', 'local')[1]['username'] == 'alice'
    with pytest.raises(ValueError, match='login_rejected'):
        auth.login('plain', 'wrong', 'local')


def test_fixed_expiry_multiple_browsers_logout_and_restart(shared):
    now = [10.0]
    auth = Administrators(shared, clock=lambda: now[0])
    first, _ = auth.login('alice', 'synthetic-password', 'local')
    second, _ = auth.login('alice', 'synthetic-password', 'local')
    auth.logout(first)
    assert auth.session(first) is None and auth.session(second) is not None
    now[0] += SESSION_SECONDS - 1
    assert auth.session(second) is not None
    now[0] += 1
    assert auth.session(second) is None
    token, _ = auth.login('alice', 'synthetic-password', 'local')
    assert Administrators(shared).session(token) is None
    auth.clear()
    assert auth.session(token) is None


def test_login_attempts_are_bounded_and_cool_down(shared):
    now = [1.0]
    auth = Administrators(shared, clock=lambda: now[0])
    for _ in range(5):
        with pytest.raises(ValueError, match='login_rejected'):
            auth.login('alice', 'wrong', 'local')
    with pytest.raises(ValueError, match='login_rate_limited'):
        auth.login('alice', 'synthetic-password', 'local')
    now[0] += 60
    assert auth.login('alice', 'synthetic-password', 'local')[0]


def test_password_hash_command_needs_no_config(tmp_path, monkeypatch, capsys):
    from seu_reserve import web
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web, 'getpass', lambda label: 'synthetic-password')
    assert web.main(['--hash-password']) == 0
    output = capsys.readouterr().out.strip()
    assert HASH_PATTERN.fullmatch(output) and 'synthetic-password' not in output
    assert not list(tmp_path.iterdir())
