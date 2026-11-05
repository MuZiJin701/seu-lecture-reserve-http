import json
import threading
import time
from urllib.parse import urlsplit

import pytest
import requests

import seu_reserve.app as app
from seu_reserve.client import RemoteError
from test_app import (FakeHistoryPoller, FakePoller, School, execute, lecture,
                      moment, native_activity, seed_opening_selection)
from test_unified_workers import install_reader_sessions


@pytest.mark.parametrize('reason', ['connection_failure', 'invalid_json', 'business_rows_invalid'])
def test_history_event_keeps_retrying_transient_errors_without_backoff(reason):
    recovered = threading.Event()
    calls, backoffs = [], []

    def query():
        calls.append(1)
        if len(calls) <= 4:
            raise RemoteError(reason)
        recovered.set()
        return []

    def retry(*_):
        backoffs.append(1)
        return 5

    reader = app.new_history_poller(query, interval=None, clock=time.monotonic,
                                    retry_delay=retry, retry_interval=lambda: .01)
    reader.start()
    try:
        assert recovered.wait(.5)
        assert len(calls) == 5 and not backoffs
    finally:
        reader.stop()


@pytest.mark.parametrize('endpoint', ['queryActivityList.do', 'vcode.do', 'addReservation.do'])
@pytest.mark.parametrize('error', [requests.ConnectionError, lambda _: RemoteError('invalid_json'),
                                   lambda _: RemoteError('http_failure', http_status=500)])
def test_transient_request_errors_never_save_backoff(tmp_path, monkeypatch, endpoint, error):
    school = School()
    school.activities = [lecture()]
    request = school.request

    def fail(method, url, **kwargs):
        if urlsplit(url).path.endswith(endpoint):
            raise error('constructed failure')
        return request(method, url, **kwargs)

    monkeypatch.setattr(school, 'request', fail)

    def stop(_):
        saved = json.loads((tmp_path / 'task.json').read_text())
        assert saved['health']['phase'] != 'backoff'
        assert not saved.get('retry_at') and not saved['cooldowns']
        if endpoint == 'addReservation.do':
            assert saved['status'] == 'pending'
        raise KeyboardInterrupt

    assert execute(school, tmp_path, sleep=stop) == 130


@pytest.mark.parametrize(('opening', 'age'), [(False, 9), (True, 7)])
def test_captcha_remains_usable_past_five_seconds_within_phase_timeout(tmp_path, monkeypatch, opening, age):
    start = moment('2026-10-18 12:00:00').timestamp()
    now = [start + 1 if opening else 1000.0]
    row = native_activity() if opening else lecture()
    school = School()
    school.activities = [row]
    for key, value in dict(instances=[], next_rows=[row], rows_override=None, at_offset=0,
                           next_error=None, next_wake_once=False, on_timeout=None,
                           error_retry_after=0, error_server_directed=False).items():
        monkeypatch.setattr(FakePoller, key, value)
    for key, value in dict(rows=[], error=None, reason=None, retry_after=0, server_directed=False).items():
        monkeypatch.setattr(FakeHistoryPoller, key, value)
    monkeypatch.setattr(app, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app, 'new_history_poller', FakeHistoryPoller)
    if opening:
        seed_opening_selection(tmp_path, start='2026-10-18T12:00:00+08:00')

    def recognize(_):
        now[0] += age
        return '1234'

    assert execute(school, tmp_path, background_poller=True, clock=lambda: now[0],
                   recognize=recognize, settings=app.Settings(state_dir=tmp_path,
                   captcha_prefetch=False)) == 130
    assert school.submitted == ['one']


def test_next_candidate_submits_while_confirmed_success_history_is_in_flight(tmp_path, monkeypatch):
    history_started, release, second_submitted, stopped = (threading.Event() for _ in range(4))
    one = lecture()
    two = lecture('two', KSSJ='2030-01-01 12:00:00', JSSJ='2030-01-01 13:00:00')

    class SlowHistorySchool(School):
        def request(self, method, url, **kwargs):
            endpoint = urlsplit(url).path.rsplit('/', 1)[-1]
            if endpoint == 'queryMyActivityList.do' and self.submitted:
                self.activities = [one, two]
                history_started.set()
                assert release.wait(4)
            result = super().request(method, url, **kwargs)
            if endpoint == 'addReservation.do' and kwargs['data']['wid'] == 'two':
                second_submitted.set()
            return result

    school = SlowHistorySchool()
    school.activities = [one]
    school.submit_replies = [{'code': 1, 'msg': '您已预约该活动'}, {'code': 1}]
    install_reader_sessions(monkeypatch, school, [])
    monkeypatch.setattr(app.random, 'uniform', lambda *_: .02)
    results = []
    runner = threading.Thread(target=lambda: results.append(app.run(
        app.Settings(state_dir=tmp_path, captcha_prefetch=False), session=school,
        environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
        recognize=lambda _: '1234', stop=stopped.is_set)))
    runner.start()
    try:
        assert history_started.wait(2), 'confirmation must notify the history reader'
        assert second_submitted.wait(2), 'an in-flight history refresh must not delay another submission'
    finally:
        stopped.set()
        release.set()
        runner.join(4)
    assert results == [130] and school.submitted == ['one', 'two']
