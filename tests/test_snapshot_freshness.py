from dataclasses import replace

import pytest

import seu_reserve.app as app
from seu_reserve.client import RemoteError
from seu_reserve.poller import ActivityPoller
from test_app import (FakePoller, School, execute, interrupted, moment, native_activity,
                      seed_opening_selection)


@pytest.mark.parametrize(('age', 'error', 'ocr_delay', 'allowed'), [
    (1.0, None, 0, True),
    (2.0, None, 0, True),
    (2.001, None, 0, False),
    (0, None, 1.0, True),
    (1.0, 'RemoteError', 0, False),
    (1.0, 'AuthenticationExpired', 0, False),
    (1.0, 'throttled', 0, False),
])
def test_fast_opening_uses_last_success_while_next_query_is_in_flight(
        tmp_path, monkeypatch, age, error, ocr_delay, allowed):
    now = [moment('2026-10-18 12:00:01').timestamp()]
    published = now[0] - age
    rows = [native_activity()]
    class InFlightList(FakePoller):
        @property
        def in_flight_since(self):
            return published + .05
        def snapshot(self):
            return replace(super().snapshot(), rows=rows, at=published,
                started=published - .1, seconds=.1,
                error='RemoteError' if error == 'throttled' else error,
                reason='server_backoff' if error == 'throttled' else 'network_timeout' if error else None,
                server_directed=error == 'throttled',
                retry_after=20 if error == 'throttled' else 0,
                failed_at=now[0] if error else 0)
    monkeypatch.setattr(app, 'ActivityPoller', InFlightList)
    monkeypatch.setattr(FakePoller, 'next_rows', rows)
    monkeypatch.setattr(FakePoller, 'next_error', None)
    monkeypatch.setattr(FakePoller, 'on_timeout', None)
    monkeypatch.setattr(FakePoller, 'rows_override', None)
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:00+08:00')
    school = School()
    school.activities = rows
    school.submit_replies = [{'code': 500}]
    def recognize(_):
        now[0] += ocr_delay
        return '1234'
    assert execute(school, tmp_path, background_poller=True, clock=lambda: now[0],
        sleep=interrupted, recognize=recognize,
        settings=app.Settings(state_dir=tmp_path, prewarm=False, captcha_prefetch=False)) == 130
    assert school.submitted == (['one'] if allowed else [])
    assert not any(url.endswith('queryActivityList.do') for _, url, _ in school.requests)


def test_slow_success_is_fresh_on_completion_and_failure_does_not_refresh_its_age():
    now = [100.0]
    def query():
        now[0] += 1.5
        if now[0] > 102:
            raise RemoteError('network_timeout')
        return [{'WID': 'one'}]
    reader = ActivityPoller(query, interval=.1, clock=lambda: now[0])
    assert reader.poll_once()
    success = reader.snapshot()
    assert success.at == 101.5 and success.started == 100 and success.error is None
    assert now[0] - success.at == 0
    assert not reader.poll_once()
    failed = reader.snapshot()
    assert failed.at == success.at and failed.rows == success.rows
    assert failed.error == 'RemoteError' and failed.reason == 'network_timeout'
