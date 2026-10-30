"""Regression tests: a CLI `--account` run owns the checkpoint, so the web supervisor must refuse.

The lock is taken with the real `Task` checkpoint object (same file lock the CLI uses) while the
supervisor is driven through the same fakes as tests/test_web.py; no process or network is used.
"""

import pytest

from seu_reserve.accounts import Accounts
from seu_reserve.state import Task
from test_web import MAIL_SHARED, account_data, make_manager, web_client


def test_manager_refuses_while_a_cli_run_holds_the_checkpoint(tmp_path):
    accounts = Accounts(tmp_path / 'data')
    account = accounts.create(account_data())
    manager = make_manager(accounts)
    checkpoint = Task(accounts.state_dir(account['id']), account['username'])
    try:
        with pytest.raises(ValueError, match='state_in_use'):
            manager.start(account['id'])
        with pytest.raises(ValueError, match='state_in_use'):
            manager.stop(account['id'])
        with pytest.raises(ValueError, match='state_in_use'):
            manager.update(account['id'], account_data().model_copy(update={'name': '不应写入'}))
        with pytest.raises(ValueError, match='state_in_use'):
            manager.delete(account['id'])
        saved = accounts.get(account['id'])
        # update must refuse before it writes the rename or restarts anything.
        assert saved['name'] == '测试账号 10001'
        assert saved['revision'] == 0
        assert saved['desired'] is False and saved['deleting'] is False
        assert manager.workers == {} and manager.retries == {} and manager.retry_at == {}
        assert (accounts.state_dir(account['id']) / 'task.json').exists()
    finally:
        checkpoint.close()
    # Once the external run releases the checkpoint, the web supervisor may start again.
    manager.start(account['id'])
    assert account['id'] in manager.workers
    manager.stop(account['id'])
    assert manager.workers == {}
    manager.close()


def test_web_api_maps_an_external_checkpoint_lock_to_409_state_in_use(tmp_path):
    with web_client(tmp_path) as (client, accounts, manager):
        created = client.post('/api/accounts', json=account_data().model_dump())
        assert created.status_code == 201
        identifier = created.json()['id']
        checkpoint = Task(accounts.state_dir(identifier), '10001')
        try:
            payload = account_data().model_dump()
            payload['name'] = '不应写入'
            for method, path, body in (
                    ('PUT', '/api/accounts/' + identifier, payload),
                    ('POST', f'/api/accounts/{identifier}/start', {}),
                    ('POST', f'/api/accounts/{identifier}/stop', {})):
                response = client.request(method, path, json=body)
                assert response.status_code == 409
                assert response.json() == {'detail': 'state_in_use'}
            saved = accounts.get(identifier)
            assert saved['name'] != '不应写入' and saved['revision'] == 0
            assert saved['desired'] is False
            listed = client.get('/api/accounts').json()
            assert listed[0]['external_running'] is True and listed[0]['running'] is True
            assert manager.workers == {}
        finally:
            checkpoint.close()
        assert client.post(f'/api/accounts/{identifier}/start', json={}).status_code == 200
        assert identifier in manager.workers
        assert client.post(f'/api/accounts/{identifier}/stop', json={}).status_code == 200
        assert manager.workers == {}


def test_external_lock_exit_does_not_burn_the_restart_budget_or_alert(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data(recipient='only@example.com', mail_enabled=True))
    now = [1000.0]
    manager = make_manager(accounts, MAIL_SHARED, clock=lambda: now[0])
    alerts = []
    monkeypatch.setattr(manager, '_alert_async', alerts.append)
    manager.start(account['id'])
    manager.retries[account['id']] = 2
    worker = manager.workers[account['id']]
    worker.updates.put(dict(type='finished', result=1, reason='state_in_use'))
    worker.process.alive = False
    manager.tick()
    saved = accounts.get(account['id'])
    assert saved['desired'] is False and saved['error'] == 'state_in_use'
    assert manager.retries[account['id']] == 2  # no extra restart attempt consumed
    assert account['id'] not in manager.retry_at and manager.workers == {}
    assert alerts == []
    assert manager.snapshot(account)['restart_count'] == 2


def test_start_failure_on_an_external_checkpoint_lock_clears_intent(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data())
    accounts.runtime(account['id'], desired=True)
    manager = make_manager(accounts)
    alerts = []
    monkeypatch.setattr(manager, '_alert_async', alerts.append)
    checkpoint = Task(accounts.state_dir(account['id']), account['username'])
    try:
        with pytest.raises(ValueError, match='state_in_use'):
            manager.start(account['id'])
        manager._start_or_retry(account['id'], manual=False)
        saved = accounts.get(account['id'])
        assert saved['desired'] is False and saved['error'] == 'state_in_use'
        assert account['id'] not in manager.retry_at
        assert manager.retries.get(account['id'], 0) == 0
        assert alerts == []
    finally:
        checkpoint.close()


def test_retry_that_meets_an_external_checkpoint_lock_stops_retrying(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path)
    account = accounts.create(account_data(recipient='only@example.com', mail_enabled=True))
    now = [500.0]
    manager = make_manager(accounts, MAIL_SHARED, clock=lambda: now[0])
    alerts = []
    monkeypatch.setattr(manager, '_alert_async', alerts.append)
    manager.start(account['id'])
    manager.workers[account['id']].process.alive = False
    manager.tick()
    assert manager.retries[account['id']] == 1
    assert manager.retry_at[account['id']] == 505.0
    checkpoint = Task(accounts.state_dir(account['id']), account['username'])
    try:
        now[0] += 5
        manager.tick()
        saved = accounts.get(account['id'])
        assert saved['desired'] is False and saved['error'] == 'state_in_use'
        assert manager.retries[account['id']] == 1  # the lock is not another crash
        assert manager.retry_at == {} and manager.workers == {}
        assert alerts == []
    finally:
        checkpoint.close()
