"""Offline authenticated console checks, using synthetic school responses only."""

import hashlib
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seu_reserve.accounts import AccountInput, Accounts, account_environ
from seu_reserve.admin import COOKIE_NAME, SESSION_SECONDS, hash_password
from seu_reserve.manager import Manager
from seu_reserve.state import Task
from seu_reserve.web import create_app, main

SECRET = 'synthetic-school-secret'
ADMIN_PASSWORD = 'synthetic-admin-password'
ADMIN_SHARED = {'SEU_WEB_ADMINS': json.dumps({'admin': hash_password(ADMIN_PASSWORD)})}
CAMPUSES = ['九龙湖校区', '四牌楼校区', '丁家桥校区', '无锡校区', '苏州校区']
MAIL_SHARED = {'SMTP_HOST': 'smtp.example.com', 'SMTP_PORT': '465',
               'SMTP_USERNAME': 'sender@example.com', 'SMTP_PASSWORD': 'synthetic-mail-secret',
               'MAIL_FROM': 'sender@example.com'}


def account_data(number='10001', **changes):
    return AccountInput(username=number, name='测试账号 ' + number, password=SECRET, **changes)


class FakeProcess:
    def __init__(self, **kwargs):
        self.alive = False
        self.args = kwargs['args']

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        if self.args[-1].is_set():
            self.alive = False

    def terminate(self):
        self.alive = False

    kill = terminate

    def close(self):
        pass


class FakeQueue(queue.Queue):
    def close(self):
        pass


class FakeContext:
    Queue = FakeQueue
    Event = threading.Event
    Process = FakeProcess


def make_manager(accounts, shared=None, clock=None):
    manager = Manager(accounts, {} if shared is None else shared) if clock is None else \
        Manager(accounts, {} if shared is None else shared, clock=clock)
    manager.context = FakeContext()
    return manager


@contextmanager
def web_client(tmp_path, shared=None, *, authenticated=True):
    accounts = Accounts(tmp_path / 'data')
    manager = make_manager(accounts, shared)
    with TestClient(create_app(accounts, {**ADMIN_SHARED, **(shared or {})}, manager=manager),
                    base_url='http://testserver') as client:
        if authenticated:
            login = client.post('/api/login', json={'username': 'admin', 'password': ADMIN_PASSWORD})
            assert login.status_code == 200
            client.headers['X-CSRF-Token'] = login.json()['csrf']
        yield client, accounts, manager


@pytest.fixture
def web(tmp_path):
    with web_client(tmp_path) as value:
        yield value


def until(check, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.03)
    raise AssertionError('offline worker did not reach its expected state')


def assert_hardened(response):
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'same-origin'
    assert response.headers['content-security-policy'].startswith("default-src 'self'; script-src 'self'")
    assert 'strict-transport-security' not in response.headers


def test_authenticated_meta_and_public_login_view(web):
    client, _, _ = web
    response = client.get('/api/meta')
    assert response.status_code == 200
    body = response.json()
    assert len(body['categories']) == 5
    assert body['campuses'] == CAMPUSES
    assert_hardened(response)
    page = client.get('/')
    assert page.status_code == 200
    assert_hardened(page)
    assert 'id="dashboard" hidden' in page.text
    assert 'login-view' in page.text and 'admin-name' in page.text
    assert '<nav' not in page.text and '每一场讲座' not in page.text
    assert 'name="titles"' not in page.text and 'name="trust_device"' not in page.text
    assert 'set-cookie' not in page.headers
    assert client.get('/api/login').status_code == 405
    assert client.post('/api/login', json={}).status_code == 422
    assert client.get('/api/session').json()['username'] == 'admin'
    assert client.get('/docs').status_code == 404


def test_authenticated_requests_never_echo_credentials(web):
    client, _, _ = web
    assert client.get('/api/accounts').json() == []
    payload = account_data().model_dump()
    created = client.post('/api/accounts', json=payload)
    assert created.status_code == 201
    identifier = created.json()['id']
    assert SECRET not in created.text and 'password' not in created.json()
    assert SECRET not in client.get('/api/accounts').text
    payload['password'] = {'bad': SECRET}
    invalid = client.put('/api/accounts/' + identifier, json=payload)
    assert invalid.status_code == 422
    assert invalid.json() == {'detail': 'invalid_request'}
    assert SECRET not in invalid.text
    assert_hardened(invalid)


def test_non_get_requests_enforce_origin_content_type_and_length(web):
    client, _, _ = web
    payload = account_data().model_dump()
    rejected = client.post('/api/accounts', json=payload, headers={'Origin': 'http://evil.example'})
    assert rejected.status_code == 403 and rejected.json()['detail'] == 'origin_rejected'
    assert_hardened(rejected)
    accepted = client.post('/api/accounts', json=payload, headers={'Origin': 'http://testserver'})
    assert accepted.status_code == 201
    rejected = client.post('/api/accounts', content=b'{}', headers={'Content-Type': 'text/plain'})
    assert rejected.status_code == 415 and rejected.json()['detail'] == 'json_required'
    assert_hardened(rejected)
    rejected = client.post('/api/accounts', content=iter([b'{}']),
                           headers={'Content-Type': 'application/json'})
    assert rejected.status_code == 411
    assert_hardened(rejected)
    rejected = client.post('/api/accounts', content=b'x' * 65537,
                           headers={'Content-Type': 'application/json'})
    assert rejected.status_code == 413
    assert_hardened(rejected)
    # A curl-style client sends no Origin header and must still work; GET has no body checks.
    assert client.post('/api/accounts', json=account_data('10002').model_dump()).status_code == 201
    assert client.get('/api/accounts').status_code == 200


def test_account_crud_identity_restart_and_delete_protection(web):
    client, accounts, manager = web
    payload = account_data().model_dump()
    identifier = client.post('/api/accounts', json=payload).json()['id']
    assert client.post('/api/accounts', json=payload).status_code == 409
    assert client.post(f'/api/accounts/{identifier}/start', json={}).status_code == 200
    original = manager.workers[identifier]
    payload.update(password='', name='新名称')
    assert client.put('/api/accounts/' + identifier, json=payload).status_code == 200
    assert manager.workers[identifier] is not original
    assert accounts.get(identifier)['password'] == SECRET
    payload['username'] = '10002'
    before_invalid_edit = manager.workers[identifier]
    edited = client.put('/api/accounts/' + identifier, json=payload)
    assert edited.status_code == 409 and edited.json()['detail'] == 'account_identity_immutable'
    assert manager.workers[identifier] is before_invalid_edit
    assert client.post(f'/api/accounts/{identifier}/stop', json={}).status_code == 200
    task = Task(accounts.state_dir(identifier), '10001')
    task.data.update(status='pending', activity='synthetic-activity')
    task.save()
    task.close()
    protected = client.request('DELETE', f'/api/accounts/{identifier}', json={})
    assert protected.status_code == 409 and protected.json()['detail'] == 'reservation_protected'
    assert accounts.get(identifier)['id'] == identifier
    task = Task(accounts.state_dir(identifier), '10001')
    task.data.update(status='scanning', activity='',
                     holds={'synthetic': {'start': 1.0, 'end': 2.0, 'since': 1.0, 'until': 2.0,
                                          'attempts': 0, 'confirmed': False}})
    task.save()
    task.close()
    assert client.request('DELETE', f'/api/accounts/{identifier}', json={}).status_code == 409
    task = Task(accounts.state_dir(identifier), '10001')
    task.data['holds'] = {}
    task.save()
    task.close()
    assert client.request('DELETE', f'/api/accounts/{identifier}', json={}).status_code == 200
    assert accounts.list() == []
    assert not accounts.state_dir(identifier).exists()


def test_mail_is_scoped_to_account_and_invalid_configuration_blocks_start(tmp_path):
    shared = {'SMTP_USERNAME': 'sender@qq.com', 'SMTP_PASSWORD': 'synthetic-mail-secret',
              'MAIL_TO': 'wrong@example.com'}
    accounts = Accounts(tmp_path / 'scoped')
    first = accounts.create(account_data(recipient='first@example.com', mail_enabled=True))
    second = accounts.create(account_data('10002', recipient='second@example.com', mail_enabled=True))
    assert account_environ(first, shared)['MAIL_TO'] == 'first@example.com'
    assert account_environ(second, shared)['MAIL_TO'] == 'second@example.com'
    blank = accounts.create(account_data('10003', mail_enabled=True))
    assert blank['mail_enabled'] is False
    assert not any(key.startswith('SMTP_') or key.startswith('MAIL_') for key in account_environ(blank, shared))
    with web_client(tmp_path / 'invalid') as (client, broken_accounts, manager):
        broken = broken_accounts.create(account_data(recipient='only@example.com', mail_enabled=True))
        response = client.post(f'/api/accounts/{broken["id"]}/start', json={})
        assert response.status_code == 409 and response.json()['detail'] == 'mail_configuration_invalid'
        assert manager.workers == {}
        assert broken_accounts.get(broken['id'])['desired'] is False


def test_mail_toggle_and_test_message(tmp_path, monkeypatch):
    import seu_reserve.manager as module
    sent = []
    monkeypatch.setattr(module.Mailer, '__call__',
                        lambda self, event, details='': sent.append((self.recipient, event)))
    with web_client(tmp_path, MAIL_SHARED) as (client, accounts, manager):
        created = client.post('/api/accounts', json=account_data(
            recipient='only@example.com', mail_enabled=True).model_dump())
        assert created.status_code == 201
        identifier = created.json()['id']
        accepted = client.post(f'/api/accounts/{identifier}/test-mail', json={})
        assert accepted.status_code == 200 and accepted.json()['status'] == 'smtp_accepted'
        assert sent == [('only@example.com', 'test_notification')]
        payload = account_data(recipient='', mail_enabled=False).model_dump()
        assert client.put('/api/accounts/' + identifier, json=payload).status_code == 200
        disabled = client.post(f'/api/accounts/{identifier}/test-mail', json={})
        assert disabled.status_code == 409 and disabled.json()['detail'] == 'mail_disabled'


def test_restart_budget_and_fatal_errors(tmp_path):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    now = [1000.0]
    manager = make_manager(accounts, clock=lambda: now[0])
    manager.start(account['id'])
    for attempt in range(4):
        manager.workers[account['id']].process.alive = False
        manager.tick()
        if attempt < 3:
            assert manager.retry_at[account['id']] == now[0] + 5
            now[0] += 5
            manager.tick()
    assert not manager.workers and not accounts.get(account['id'])['desired']
    assert accounts.get(account['id'])['error'] == 'restart_exhausted'
    manager.start(account['id'])
    worker = manager.workers[account['id']]
    worker.updates.put(dict(type='finished', result=1, reason='authentication_rejected'))
    worker.process.alive = False
    manager.tick()
    assert not manager.workers and not manager.retry_at
    assert accounts.get(account['id'])['error'] == 'authentication_rejected'


def test_stable_run_resets_restart_budget_and_shutdown_preserves_intent(tmp_path):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    now = [1000.0]
    manager = make_manager(accounts, clock=lambda: now[0])
    manager.start(account['id'])
    manager.retries[account['id']] = 3
    now[0] += 600
    manager.tick()
    assert manager.retries[account['id']] == 0
    manager.close()
    assert accounts.get(account['id'])['desired']
    restored = make_manager(accounts)
    restored.open()
    try:
        assert account['id'] in restored.workers
        restored.stop(account['id'])
        restored.close()
        assert not accounts.get(account['id'])['desired']
    finally:
        restored.close()


def test_start_failure_during_retry_keeps_desired_intent_and_exhausts_budget(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    now = [1000.0]
    manager = make_manager(accounts, clock=lambda: now[0])
    manager.start(account['id'])
    manager.workers[account['id']].process.alive = False
    manager.tick()
    assert accounts.get(account['id'])['desired'] is True
    attempts = []

    def failing_launch(current):
        attempts.append(current['id'])
        raise OSError('synthetic launch failure')

    monkeypatch.setattr(manager, '_launch', failing_launch)
    try:
        for _ in range(6):
            if accounts.get(account['id'])['error'] == 'restart_exhausted':
                break
            assert accounts.get(account['id'])['desired'] is True
            now[0] += 5
            manager.tick()
        state = accounts.get(account['id'])
        assert state['desired'] is False and state['error'] == 'restart_exhausted'
        assert 1 <= len(attempts) <= 3
    finally:
        manager.close()


def test_start_failure_during_open_keeps_desired_intent(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    accounts.runtime(account['id'], desired=True)
    manager = make_manager(accounts, clock=lambda: 1000.0)
    attempts = []

    def failing_launch(current):
        attempts.append(current['id'])
        raise OSError('synthetic launch failure')

    monkeypatch.setattr(manager, '_launch', failing_launch)
    manager.open()
    try:
        assert account['id'] in attempts
        state = accounts.get(account['id'])
        assert state['desired'] is True and state['error'] != 'startup_failed'
    finally:
        manager.close()
    assert accounts.get(account['id'])['desired'] is True


@pytest.mark.parametrize('reason', ['account_already_running', 'service_stopping'])
def test_benign_start_failures_do_not_change_desired_intent(tmp_path, monkeypatch, reason):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    accounts.runtime(account['id'], desired=True)
    manager = make_manager(accounts, clock=lambda: 1000.0)

    def benign(current):
        raise ValueError(reason)

    monkeypatch.setattr(manager, '_launch', benign)
    manager.open()
    try:
        state = accounts.get(account['id'])
        assert state['desired'] is True
        assert state['error'] in ('', 'restarting')
    finally:
        manager.close()


def test_alert_after_delete_does_not_send_or_recreate_state(tmp_path, monkeypatch):
    import seu_reserve.manager as module
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data(recipient='only@example.com', mail_enabled=True))
    sent = []
    monkeypatch.setattr(module.Mailer, '__call__',
                        lambda self, event, details='': sent.append((self.recipient, event)))
    manager = make_manager(accounts, MAIL_SHARED)
    accounts.delete(account['id'])
    manager._alert(account)
    assert sent == []
    assert not accounts.state_dir(account['id']).exists()


def test_closed_manager_does_not_send_alerts(tmp_path, monkeypatch):
    import seu_reserve.manager as module
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data(recipient='only@example.com', mail_enabled=True))
    sent = []
    monkeypatch.setattr(module.Mailer, '__call__',
                        lambda self, event, details='': sent.append((self.recipient, event)))
    manager = make_manager(accounts, MAIL_SHARED)
    manager.start(account['id'])
    manager.workers[account['id']].updates.put(dict(type='finished', result=1,
                                                    reason='authentication_rejected'))
    manager.workers[account['id']].process.alive = False
    manager.close()
    manager._alert(account)
    assert sent == []


def test_second_manager_on_the_same_data_root_is_rejected(tmp_path):
    accounts = Accounts(tmp_path)
    first = make_manager(accounts)
    first.open()
    try:
        second = make_manager(accounts)
        with pytest.raises(ValueError, match='manager_already_running'):
            second.open()
    finally:
        first.close()


def test_state_dir_rejects_malformed_names_and_links(tmp_path):
    accounts = Accounts(tmp_path / 'data')
    with pytest.raises(ValueError):
        accounts.state_dir('not-hex')
    with pytest.raises(ValueError):
        accounts.state_dir('A' * 32)
    identifier = hashlib.sha256(b'synthetic').hexdigest()
    target = tmp_path / 'elsewhere'
    target.mkdir()
    link = accounts.directory / identifier
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip('creating a junction requires local command availability')
    else:
        link.symlink_to(target, target_is_directory=True)
    assert link.is_junction() if os.name == 'nt' else link.is_symlink()
    with pytest.raises(ValueError):
        accounts.state_dir(identifier)


def synthetic_worker(account, root, shared, commands, updates, stopped):
    directory = Path(root) / 'accounts' / account['id']
    task = Task(directory, account['username'])
    (directory / 'synthetic-marker').write_text(str(os.getpid()), encoding='utf-8')
    challenge = account['id']
    updates.put(dict(type='challenge', id=challenge, kind='sms', label='需要六位短信验证码'))
    while not stopped.is_set():
        try:
            message = commands.get(timeout=.05)
        except queue.Empty:
            continue
        if message['id'] == challenge:
            updates.put(dict(type='challenge_clear', id=challenge))
    task.close()
    updates.put(dict(type='finished', result=130, reason='stopped'))


def test_ten_real_spawned_processes_are_isolated(tmp_path):
    accounts = Accounts(tmp_path)
    entries = [accounts.create(account_data(str(20000 + i))) for i in range(10)]
    manager = Manager(accounts, {}, target=synthetic_worker)
    manager.open()
    try:
        for entry in entries:
            manager.start(entry['id'])
        until(lambda: all(manager.snapshot(entry)['challenge'] for entry in entries))
        pids = {(accounts.state_dir(entry['id']) / 'synthetic-marker').read_text() for entry in entries}
        assert len(pids) == 10
        first, second = entries[:2]
        with pytest.raises(ValueError, match='challenge_expired'):
            manager.answer(second['id'], first['id'], '654321')
        manager.answer(first['id'], first['id'], '654321')
        with pytest.raises(ValueError, match='challenge_expired'):
            manager.answer(first['id'], first['id'], '654321')
        # There is deliberately no ten-account hard cap.
        extra = accounts.create(account_data('20010'))
        manager.start(extra['id'])
        until(lambda: manager.snapshot(extra)['challenge'])
        assert len(manager.workers) == 11
        manager.stop(first['id'])
        assert all(manager.workers[entry['id']].process.is_alive() for entry in entries[1:])
        for entry in entries:
            saved = accounts.checkpoint(entry['id'])
            assert saved['account'] == hashlib.sha256(entry['username'].encode()).hexdigest()
    finally:
        manager.close()


def test_stop_before_submission_uses_existing_engine_checkpoint(tmp_path):
    from seu_reserve.app import Settings, run
    from test_app import School, lecture
    stopping = [False]

    class StoppingSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('queryActivityList.do'):
                stopping[0] = True
            return response

    school = StoppingSchool()
    school.activities = [lecture()]
    result = run(Settings(state_dir=tmp_path), session=school, stop=lambda: stopping[0],
                 environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'synthetic'},
                 recognize=lambda _: '1234', background_poller=False)
    assert result == 130 and school.submitted == []
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'scanning'


def test_parallel_start_cannot_overtake_configuration_restart(tmp_path):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    manager = make_manager(accounts)
    manager.start(account['id'])
    original = manager.workers[account['id']]
    entered, release = threading.Event(), threading.Event()

    def join(timeout=None):
        entered.set()
        assert release.wait(3)
        original.process.alive = False

    original.process.join = join
    results = []

    def update():
        manager.update(account['id'], account_data().model_copy(update={'name': '更新后'}))
        results.append('updated')

    def start():
        try:
            manager.start(account['id'])
        except ValueError as exc:
            results.append(str(exc))

    updater = threading.Thread(target=update)
    starter = threading.Thread(target=start)
    updater.start()
    assert entered.wait(2)
    starter.start()
    assert manager.snapshot(account)['stopping']
    release.set()
    updater.join(3)
    starter.join(3)
    assert sorted(results) == ['account_already_running', 'updated']
    assert len(manager.workers) == 1
    manager.close()


def test_real_engine_sms_bridge_uses_the_current_account_challenge(tmp_path, monkeypatch):
    import seu_reserve.manager as module
    from seu_reserve.app import run
    from test_app import School, interrupted
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data('test-user'))
    commands, updates, stopped = queue.Queue(), queue.Queue(), threading.Event()
    school = School(sms=True)
    monkeypatch.setattr(module.signal, 'signal', lambda *args: None)
    monkeypatch.setattr('seu_reserve.cli.load_recognizer', lambda: lambda _: '1234')
    monkeypatch.setattr(module, 'run', lambda settings, **kwargs: run(settings, session=school,
        sleep=interrupted, background_poller=False, **kwargs))
    seen = []

    def answer():
        for value in ('send', '654321'):
            while True:
                message = updates.get(timeout=5)
                if message['type'] == 'challenge':
                    break
            seen.append(message['id'])
            commands.put(dict(id=message['id'], answer=value))

    thread = threading.Thread(target=answer)
    thread.start()
    module.worker_main(account, str(accounts.root), {}, commands, updates, stopped)
    thread.join(5)
    assert school.sms_sends == 1 and school.authenticated and len(set(seen)) == 2
    assert '654321' not in (accounts.state_dir(account['id']) / 'runner.log').read_text()


def test_shutdown_reaps_all_children_even_when_catalogue_is_unavailable(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path)
    manager = make_manager(accounts)
    for number in ('10001', '10002'):
        account = accounts.create(account_data(number))
        manager.start(account['id'])
        manager.workers[account['id']].updates.put(dict(type='finished', result=0, reason='done'))
    processes = [worker.process for worker in manager.workers.values()]

    def broken(*args, **kwargs):
        import sqlite3
        raise sqlite3.OperationalError('synthetic storage failure')

    monkeypatch.setattr(accounts, 'get', broken)
    monkeypatch.setattr(accounts, 'runtime', broken)
    manager.close()
    assert not manager.workers and all(not process.alive for process in processes)


def test_corrupt_task_stops_before_ocr_and_does_not_enter_restart_loop(tmp_path, monkeypatch):
    import seu_reserve.manager as module
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    directory = accounts.state_dir(account['id'])
    directory.mkdir()
    (directory / 'task.json').write_text('not json')
    monkeypatch.setattr(module.signal, 'signal', lambda *args: None)

    def unexpected():
        raise AssertionError('OCR must not load for invalid state')

    monkeypatch.setattr('seu_reserve.cli.load_recognizer', unexpected)
    updates = queue.Queue()
    module.worker_main(account, str(tmp_path), {}, queue.Queue(), updates, threading.Event())
    result = updates.get_nowait()
    assert result == dict(type='finished', result=1, reason='configuration_or_state_invalid')
    manager = make_manager(accounts)
    manager.start(account['id'])
    worker = manager.workers[account['id']]
    worker.updates.put(result)
    worker.process.alive = False
    manager.tick()
    assert not manager.retry_at and not accounts.get(account['id'])['desired']


def test_supervisor_alert_is_once_per_task_and_uses_account_recipient(tmp_path, monkeypatch):
    import seu_reserve.manager as module
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data(recipient='only@example.com', mail_enabled=True))
    sent = []

    def deliver(self, event, details=''):
        sent.append((self.recipient, event))

    monkeypatch.setattr(module.Mailer, '__call__', deliver)
    manager = make_manager(accounts, MAIL_SHARED)
    manager._alert(account)
    manager._alert(account)
    assert sent == [('only@example.com', 'task_failed')]
    assert accounts.checkpoint(account['id'])['health']['last_mail_status'] == 'accepted'


def test_plain_http_round_trip_with_login(tmp_path):
    import requests
    import uvicorn
    accounts = Accounts(tmp_path / 'data')
    app = create_app(accounts, ADMIN_SHARED)
    config = uvicorn.Config(app, host='127.0.0.1', port=0, log_level='critical', access_log=False,
                            timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    address = f'http://127.0.0.1:{sock.getsockname()[1]}'
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        until(lambda: server.started)
        with requests.Session() as session:
            session.trust_env = False
            assert session.get(address + '/api/meta', timeout=3).status_code == 401
            login = session.post(address + '/api/login', json={'username': 'admin', 'password': ADMIN_PASSWORD}, timeout=3)
            assert login.status_code == 200
            session.headers['X-CSRF-Token'] = login.json()['csrf']
            meta = session.get(address + '/api/meta', timeout=3)
            assert meta.status_code == 200 and len(meta.json()['categories']) == 5
            created = session.post(address + '/api/accounts',
                                   json=account_data().model_dump(), timeout=3)
            assert created.status_code == 201
            assert SECRET not in created.text and 'password' not in created.json()
            listed = session.get(address + '/api/accounts', timeout=3)
            assert listed.status_code == 200 and len(listed.json()) == 1
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
    assert not thread.is_alive()


def test_main_reports_safe_configuration_reasons(tmp_path, capsys):
    config = tmp_path / '.env'
    config.write_text('SEU_WEB_PORT=0\nSEU_DATA_DIR=data\n', encoding='utf-8')
    assert main(['--config', str(config)]) == 1
    assert 'reason=invalid_port' in capsys.readouterr().out
    assert not (tmp_path / 'data').exists()
    config.write_text('SEU_DATA_DIR=\nSEU_WEB_PORT=8080\n', encoding='utf-8')
    assert main(['--config', str(config)]) == 1
    assert 'reason=invalid_data_directory' in capsys.readouterr().out
    config.write_text('SEU_UNKNOWN_WEB_FIELD=1\n', encoding='utf-8')
    assert main(['--config', str(config)]) == 1
    assert 'reason=unknown_configuration_key' in capsys.readouterr().out


def test_main_reports_held_manager_lock(tmp_path, capfd):
    root = tmp_path / 'data'
    accounts = Accounts(root)
    holder = make_manager(accounts)
    holder.open()
    try:
        config = tmp_path / '.env'
        config.write_text('SEU_DATA_DIR=data\nSEU_WEB_HOST=127.0.0.1\nSEU_WEB_PORT=18731\nSEU_WEB_ADMINS=' + ADMIN_SHARED['SEU_WEB_ADMINS'] + '\n',
                          encoding='utf-8')
        result = {}

        def run_main():
            result['code'] = main(['--config', str(config)])

        thread = threading.Thread(target=run_main, daemon=True)
        thread.start()
        thread.join(30)
        assert not thread.is_alive(), 'main hung instead of failing on a held manager lock'
        assert result['code'] == 1
        assert 'reason=manager_already_running' in capfd.readouterr().out
    finally:
        holder.close()


def test_main_prints_a_browsable_url_for_a_wildcard_listener(tmp_path, capsys, monkeypatch):
    import uvicorn

    calls = {}

    def fake_run(app, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, 'run', fake_run)
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\nSEU_WEB_HOST=0.0.0.0\nSEU_WEB_PORT=18080\nSEU_WEB_ADMINS=' + ADMIN_SHARED['SEU_WEB_ADMINS'] + '\n',
                      encoding='utf-8')
    assert main(['--config', str(config)]) == 0
    out = capsys.readouterr().out
    assert 'http://127.0.0.1:18080' in out
    assert '0.0.0.0 只是监听所有网卡' in out
    assert calls['host'] == '0.0.0.0' and calls['port'] == 18080


def test_delete_refuses_while_another_writer_holds_the_checkpoint(tmp_path):
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(account_data())
    directory = accounts.state_dir(account['id'])
    task = Task(directory, account['username'])
    try:
        task.data['status'] = 'scanning'
        task.save()
        with pytest.raises(ValueError, match='state_in_use'):
            accounts.delete(account['id'])
        assert accounts.get(account['id'])['deleting'] is False
        assert (directory / 'task.json').exists()
    finally:
        task.close()


def test_delete_marks_tombstone_and_holds_lock_through_cleanup(tmp_path, monkeypatch):
    from seu_reserve import accounts as module
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(account_data())
    directory = accounts.state_dir(account['id'])
    task = Task(directory, account['username'])
    task.data['status'] = 'scanning'
    task.save()
    task.close()
    observed = {}
    original = module.Accounts._remove_state_files

    def wrapper(self, target):
        observed['deleting'] = accounts.get(account['id'])['deleting']
        # A concurrent CLI writer must not be able to take the checkpoint lock mid-deletion.
        with pytest.raises(ValueError, match='state_in_use'):
            Task(target, account['username'])
        original(target)

    monkeypatch.setattr(module.Accounts, '_remove_state_files', wrapper)
    accounts.delete(account['id'])
    assert observed['deleting'] is True
    assert accounts.list() == []
    assert not directory.exists()


def test_read_only_account_environ_skips_mail_validation(tmp_path):
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(account_data(recipient='dest@example.com', mail_enabled=True))
    with pytest.raises(ValueError, match='mail_configuration_invalid'):
        account_environ(account, {})
    env = account_environ(account, {}, mail=False)
    assert env['SEU_USERNAME'] == '10001' and env['SEU_PASSWORD'] == SECRET
    assert not any(key.startswith(('SMTP_', 'MAIL_')) for key in env)


def test_delete_locks_before_a_missing_checkpoint(tmp_path, monkeypatch):
    from seu_reserve import accounts as module
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(account_data())
    directory = accounts.state_dir(account['id'])
    assert not (directory / 'task.json').exists()
    observed = {}
    original = module.Accounts._remove_state_files

    def wrapper(self, target):
        # A first CLI start must not be able to create task.json after the delete's file check.
        with pytest.raises(ValueError, match='state_in_use'):
            Task(target, account['username'])
        observed['locked'] = True
        original(target)

    monkeypatch.setattr(module.Accounts, '_remove_state_files', wrapper)
    accounts.delete(account['id'])
    assert observed.get('locked') is True
    assert accounts.list() == []
    assert not directory.exists()


@pytest.mark.parametrize('plaintext', [False, True])
def test_anonymous_access_csrf_logout_and_expiry(tmp_path, plaintext):
    shared = {'SEU_WEB_ADMINS': json.dumps({'admin': ADMIN_PASSWORD})} if plaintext else ADMIN_SHARED
    with web_client(tmp_path, shared, authenticated=False) as (client, accounts, manager):
        assert client.get('/').status_code == 200
        for path in ('meta', 'accounts', 'session'):
            denied = client.get('/api/' + path)
            assert denied.status_code == 401
            assert_hardened(denied)
        assert client.post('/api/accounts', json=account_data().model_dump()).status_code == 401
        login = client.post('/api/login', json={'username': 'admin', 'password': ADMIN_PASSWORD})
        assert login.status_code == 200
        assert 'HttpOnly' in login.headers['set-cookie'] and 'SameSite=strict' in login.headers['set-cookie']
        assert f'Max-Age={SESSION_SECONDS}' in login.headers['set-cookie']
        assert client.post('/api/accounts', json=account_data().model_dump()).status_code == 403
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        assert client.post('/api/accounts', json=account_data().model_dump()).status_code == 201
        account = accounts.list()[0]
        manager.start(account['id'])
        assert client.post('/api/logout', json={}).status_code == 200
        assert manager.workers[account['id']].process.is_alive()
        assert client.get('/api/accounts').status_code == 401
        login = client.post('/api/login', json={'username': 'admin', 'password': ADMIN_PASSWORD})
        auth = client.app.state.administrators
        token = client.cookies.get(COOKIE_NAME)
        auth.sessions[token]['expires'] = 0
        assert client.get('/api/accounts').status_code == 401


def test_stale_configuration_does_not_stop_or_overwrite_running_account(web):
    client, accounts, manager = web
    payload = account_data().model_dump()
    created = client.post('/api/accounts', json=payload).json()
    identifier = created['id']
    payload['name'] = 'first update'
    updated = client.put('/api/accounts/' + identifier, json=payload)
    assert updated.status_code == 200 and updated.json()['revision'] == 1
    manager.start(identifier)
    worker = manager.workers[identifier]
    payload['name'] = 'stale update'
    rejected = client.put('/api/accounts/' + identifier, json=payload)
    assert rejected.status_code == 409 and rejected.json()['detail'] == 'configuration_conflict'
    assert manager.workers[identifier] is worker and worker.process.is_alive()
    assert accounts.get(identifier)['name'] == 'first update'


def test_legacy_managed_options_are_normalized_without_losing_state(tmp_path):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    legacy = {**account['options'], 'titles': ['old filter'], 'trust_device': False,
              'poller_align': False, 'mail_success': False}
    with accounts.connect() as db:
        db.execute('UPDATE accounts SET options=?, recipient=?, mail_enabled=0 WHERE id=?',
                   (json.dumps(legacy), 'synthetic@example.com', account['id']))
    normalized = accounts.get(account['id'])
    assert normalized['options']['titles'] == []
    assert all(normalized['options'][key] for key in ('trust_device', 'prewarm', 'captcha_prefetch',
                                                     'history_background', 'poller_align', 'mail_success'))
    assert normalized['mail_enabled'] is True
    assert normalized['password'] == SECRET and normalized['revision'] == 0


def test_reservation_snapshot_is_local_and_protection_is_separate(tmp_path):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    task = Task(accounts.state_dir(account['id']), account['username'])
    task.data.update(status='pending', activity='unconfirmed', pending_start=time.time()+100,
                     pending_end=time.time()+200,
                     holds=None,
                     reservations={'at': time.time(), 'items': [dict(wid='booked', title='synthetic lecture',
                         start=time.time()+300, end=time.time()+400, status='reserved', password=SECRET)]})
    task.save()
    task.close()
    manager = make_manager(accounts)
    snapshot = manager.snapshot(account)
    assert snapshot['reservations']['stale'] is True
    assert snapshot['reservations']['items'][0]['wid'] == 'booked'
    assert SECRET not in json.dumps(snapshot)
    assert snapshot['protections'][0]['wid'] == 'unconfirmed'
    assert manager.snapshot(accounts.create(account_data('10002')))['reservations'] is None


def test_existing_catalogue_gains_revision_without_touching_checkpoint(tmp_path):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    task = Task(accounts.state_dir(account['id']), account['username'])
    task.data.update(status='pending', activity='synthetic-pending')
    task.save()
    task.close()
    before = (accounts.state_dir(account['id']) / 'task.json').read_bytes()
    with accounts.connect() as db:
        db.execute('ALTER TABLE accounts DROP COLUMN revision')
    db.close()
    reopened = Accounts(tmp_path)
    assert reopened.get(account['id'])['revision'] == 0
    assert reopened.get(account['id'])['password'] == SECRET
    assert (accounts.state_dir(account['id']) / 'task.json').read_bytes() == before


def test_web_without_admin_configuration_refuses_before_creating_data(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv('SEU_WEB_ADMINS', raising=False)
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\n')
    assert main(['--config', str(config)]) == 1
    assert 'reason=admin_configuration_invalid' in capsys.readouterr().out
    assert not (tmp_path / 'data').exists()


def test_reservation_snapshot_freshness_tracks_running_state_and_query_failures(tmp_path):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    directory = accounts.state_dir(account['id'])
    now = time.time()
    task = Task(directory, account['username'])
    task.data['reservations'] = {'at': now, 'items': [dict(
        wid='booked', title='synthetic lecture', category='人文与科学素养系列讲座_心理健康',
        location='九龙湖校区', online=False, start=now + 300, end=now + 400, status='reserved')]}
    task.save()
    task.close()
    manager = make_manager(accounts)
    manager.start(account['id'])
    try:
        fresh = manager.snapshot(account)
        assert fresh['running'] is True
        assert fresh['reservations']['stale'] is False
        assert fresh['reservations']['items'][0]['wid'] == 'booked'
        task = Task(directory, account['username'])
        task.data['health'] = {'failure_count': 1}
        task.save()
        task.close()
        # A failing query keeps the last good display but marks it stale; items are not dropped.
        stale = manager.snapshot(account)
        assert stale['reservations']['stale'] is True
        assert stale['reservations']['at'] == now
        assert [row['wid'] for row in stale['reservations']['items']] == ['booked']
    finally:
        manager.close()


def test_console_reads_never_contact_the_school(web, monkeypatch):
    client, _, _ = web
    created = client.post('/api/accounts', json=account_data().model_dump())
    assert created.status_code == 201
    identifier = created.json()['id']

    def forbidden(*args, **kwargs):
        raise AssertionError('console pages must never contact the school')

    monkeypatch.setattr('requests.Session.request', forbidden)
    assert client.get('/').status_code == 200
    assert client.get('/api/meta').json()['campuses'] == CAMPUSES
    listed = client.get('/api/accounts')
    assert listed.status_code == 200 and listed.json()[0]['id'] == identifier


def test_second_client_lifecycle_invalidates_the_old_session_token(tmp_path):
    accounts = Accounts(tmp_path / 'data')
    app = create_app(accounts, ADMIN_SHARED, manager=make_manager(accounts))
    with TestClient(app, base_url='http://testserver') as first:
        login = first.post('/api/login', json={'username': 'admin', 'password': ADMIN_PASSWORD})
        assert login.status_code == 200
        token = first.cookies.get(COOKIE_NAME)
        assert token and first.get('/api/accounts').status_code == 200
    # Lifespan shutdown/startup clears process-local sessions; a restart must not trust old cookies.
    assert app.state.administrators.session(token) is None
    with TestClient(app, base_url='http://testserver') as second:
        second.cookies.set(COOKIE_NAME, token)
        assert second.get('/api/accounts').status_code == 401
        assert second.post('/api/login', json={'username': 'admin',
                                              'password': ADMIN_PASSWORD}).status_code == 200


def test_created_account_options_are_forced_to_the_managed_policy(web):
    client, accounts, _ = web
    payload = account_data().model_dump()
    payload['options'] = dict(
        titles=['旧筛选'], categories=['人文与科学素养系列讲座_心理健康', '“SEU咖啡间”系列沙龙活动'],
        campuses=['九龙湖校区'], online=False, offline=True, trust_device=False, prewarm=False,
        captcha_prefetch=False, poller_align=False, history_background=False, mail_success=False)
    created = client.post('/api/accounts', json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body['options']) == {'categories', 'campuses', 'online', 'offline'}
    assert body['options'] == {'categories': payload['options']['categories'],
                               'campuses': ['九龙湖校区'], 'online': False, 'offline': True}
    saved = accounts.get(body['id'])
    assert saved['options']['titles'] == []
    assert all(saved['options'][key] is True for key in
               ('trust_device', 'prewarm', 'captcha_prefetch', 'poller_align',
                'history_background', 'mail_success'))


@pytest.mark.parametrize('changes, status, detail', [
    ({'password': ''}, 409, 'password_required'),
    ({'recipient': 'not-an-address'}, 422, 'invalid_request'),
    ({'options': {'categories': []}}, 422, 'invalid_request'),
])
def test_account_input_validation_never_writes_a_partial_account(web, changes, status, detail):
    client, accounts, manager = web
    payload = account_data().model_dump()
    payload.update(changes)
    response = client.post('/api/accounts', json=payload)
    assert response.status_code == status
    assert response.json() == {'detail': detail}
    assert accounts.list() == [] and manager.workers == {}


def test_login_shell_and_assets_are_public(tmp_path):
    with web_client(tmp_path, authenticated=False) as (client, _, _):
        page = client.get('/')
        assert page.status_code == 200
        assert 'name="viewport"' in page.text
        assert 'id="confirm-dialog"' in page.text
        assert '尚未同步' in page.text
        for path, media_type in (('/assets/app.js', 'text/javascript'),
                                 ('/assets/style.css', 'text/css'),
                                 ('/assets/logo.png', 'image/png')):
            asset = client.get(path)
            assert asset.status_code == 200
            assert asset.headers['content-type'].startswith(media_type)


def test_revision_migration_tolerates_a_concurrent_duplicate_column(tmp_path):
    import sqlite3

    accounts = Accounts(tmp_path / 'data')
    identifier = accounts.create(account_data())['id']
    with accounts.connect() as db:
        db.execute('ALTER TABLE accounts DROP COLUMN revision')

    class RacingConnection:
        """Proxy that simulates another process adding `revision` between PRAGMA and ALTER."""

        def __init__(self, db, path):
            self.db = db
            self.path = path

        def __getattr__(self, name):
            return getattr(self.db, name)

        def __enter__(self):
            return self.db

        def __exit__(self, *exc_info):
            return self.db.__exit__(*exc_info)

        def execute(self, statement, *args):
            if statement.startswith('ALTER TABLE accounts ADD COLUMN revision'):
                with sqlite3.connect(self.path) as peer:
                    peer.execute(statement)
                raise sqlite3.OperationalError('duplicate column name: revision')
            return self.db.execute(statement, *args)

    class RacingAccounts(Accounts):
        def connect(self):
            return RacingConnection(Accounts.connect(self), self.path)

    reopened = RacingAccounts(tmp_path / 'data')
    saved = reopened.get(identifier)
    assert saved['revision'] == 0
    assert saved['password'] == SECRET


def test_main_constructs_the_administrator_registry_once_and_injects_it(tmp_path, monkeypatch):
    import seu_reserve.web as module
    instances = []
    original = module.Administrators

    class CountingAdministrators(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    captured = {}

    def fake_create_app(accounts, shared, **options):
        captured.update(options)
        return object()

    monkeypatch.setattr(module, 'Administrators', CountingAdministrators)
    monkeypatch.setattr(module, 'create_app', fake_create_app)
    monkeypatch.setattr('uvicorn.run', lambda app, **options: None)
    config = tmp_path / '.env'
    config.write_text('SEU_DATA_DIR=data\nSEU_WEB_HOST=127.0.0.1\nSEU_WEB_PORT=18080\n'
                      'SEU_WEB_ADMINS=' + ADMIN_SHARED['SEU_WEB_ADMINS'] + '\n', encoding='utf-8')
    assert module.main(['--config', str(config)]) == 0
    # Plaintext passwords are hashed while building the registry; a second build would re-hash.
    assert len(instances) == 1
    assert captured['administrators'] is instances[0]
