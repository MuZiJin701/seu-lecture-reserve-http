import json

import pytest

from seu_reserve.admin import Administrators, COOKIE_NAME, HASH_PATTERN, SESSION_SECONDS, hash_password


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


def test_repeated_successful_logins_never_consume_the_failure_budget(shared):
    now = [100.0]
    auth = Administrators(shared, clock=lambda: now[0])
    tokens = [auth.login('alice', 'synthetic-password', 'local')[0] for _ in range(6)]
    assert len(set(tokens)) == 6
    assert not list(auth.attempts)


def test_a_successful_login_clears_this_sources_failed_attempts(shared):
    now = [100.0]
    auth = Administrators(shared, clock=lambda: now[0])
    for _ in range(4):
        with pytest.raises(ValueError, match='login_rejected'):
            auth.login('alice', 'wrong', 'local')
    auth.login('alice', 'synthetic-password', 'local')
    assert not any(address == 'local' for _, address in auth.attempts)
    # Four failures before the success plus four after would exceed the cap if they were not reset.
    for _ in range(4):
        with pytest.raises(ValueError, match='login_rejected'):
            auth.login('alice', 'wrong', 'local')
    with pytest.raises(ValueError, match='login_rejected'):
        auth.login('alice', 'wrong', 'local')
    with pytest.raises(ValueError, match='login_rate_limited'):
        auth.login('alice', 'synthetic-password', 'local')


def test_global_failure_budget_refuses_a_brand_new_source(shared, monkeypatch):
    import seu_reserve.admin as module
    # 600k PBKDF2 rounds per attempt would make 100 real failures take tens of seconds; the
    # counting logic is what this test pins, the digest comparison is covered elsewhere.
    monkeypatch.setattr(module.hashlib, 'pbkdf2_hmac', lambda *args: b'\x00' * 32)
    now = [10.0]
    auth = Administrators(shared, clock=lambda: now[0])
    for index in range(100):
        with pytest.raises(ValueError, match='login_rejected'):
            auth.login('alice', 'wrong', f'198.51.100.{index}')
    assert len(auth.attempts) == 100
    with pytest.raises(ValueError, match='login_rate_limited'):
        auth.login('alice', 'synthetic-password', '203.0.113.7')


def test_web_login_rate_limit_response_carries_retry_after(tmp_path, shared, monkeypatch):
    import seu_reserve.admin as module
    from fastapi.testclient import TestClient
    from seu_reserve.accounts import Accounts
    from seu_reserve.manager import Manager
    from seu_reserve.web import create_app
    monkeypatch.setattr(module.hashlib, 'pbkdf2_hmac', lambda *args: b'\x00' * 32)
    accounts = Accounts(tmp_path / 'data')
    app = create_app(accounts, shared, manager=Manager(accounts, {}))
    with TestClient(app, base_url='http://testserver') as client:
        for _ in range(5):
            rejected = client.post('/api/login', json={'username': 'alice', 'password': 'wrong'})
            assert rejected.status_code == 401
            assert rejected.json() == {'detail': 'login_rejected'}
        limited = client.post('/api/login', json={'username': 'alice', 'password': 'synthetic-password'})
        assert limited.status_code == 429
        assert limited.json() == {'detail': 'login_rate_limited'}
        assert limited.headers['retry-after'] == '60'


def test_create_app_reuses_an_injected_administrators_instance(tmp_path, shared):
    from fastapi.testclient import TestClient
    from seu_reserve.accounts import Accounts
    from seu_reserve.manager import Manager
    from seu_reserve.web import create_app
    accounts = Accounts(tmp_path / 'data')
    auth = Administrators(shared)
    app = create_app(accounts, shared, manager=Manager(accounts, {}), administrators=auth)
    assert app.state.administrators is auth
    with TestClient(app, base_url='http://testserver') as client:
        login = client.post('/api/login', json={'username': 'alice', 'password': 'synthetic-password'})
        assert login.status_code == 200
        assert auth.session(client.cookies.get(COOKIE_NAME)) is not None
        assert client.get('/api/accounts').json() == []


def test_concurrent_failed_logins_cannot_exceed_the_source_budget(shared, monkeypatch):
    import threading

    import seu_reserve.admin as admin_module

    now = [1000.0]
    auth = Administrators(shared, clock=lambda: now[0])
    barrier = threading.Barrier(12)
    results = []
    calls = [0]
    real = admin_module.hashlib.pbkdf2_hmac

    def counted(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(admin_module.hashlib, 'pbkdf2_hmac', counted)
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        try:
            auth.login('alice', 'wrong', '10.0.0.9')
            outcome = 'ok'
        except ValueError as exc:
            outcome = str(exc)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls[0] == 5
    assert results.count('login_rejected') == 5
    assert results.count('login_rate_limited') == 7
    assert auth.pending_total == 0 and auth.pending == {}
