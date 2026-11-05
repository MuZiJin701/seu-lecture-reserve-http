import json
import threading
import time
from urllib.parse import urlsplit

import pytest
import requests

from seu_reserve.client import RemoteError
from seu_reserve.poller import ActivityPoller, WaitGate
from test_app import School, execute, lecture


@pytest.mark.parametrize('failure', ['timeout', 'format', 'ocr', 'ocr_error', 'http_503'])
def test_fresh_captcha_retries_a_transient_failure_in_the_same_scan(tmp_path, monkeypatch, failure):
    school = School()
    school.activities = [lecture()]
    request = school.request
    attempts = []

    def fail_once(method, url, **kwargs):
        if urlsplit(url).path.endswith('vcode.do'):
            attempts.append(1)
            if len(attempts) == 1:
                if failure == 'timeout':
                    raise requests.ReadTimeout('constructed captcha timeout')
                if failure == 'format':
                    raise RemoteError('captcha_invalid')
                if failure == 'http_503':
                    response = request(method, url, **kwargs)
                    response.status_code = 503
                    return response
        return request(method, url, **kwargs)

    monkeypatch.setattr(school, 'request', fail_once)

    def stop(_):
        assert school.submitted == ['one'] and len(attempts) == 2
        saved = json.loads((tmp_path / 'task.json').read_text())
        assert not saved.get('retry_at') and not saved['cooldowns']
        raise KeyboardInterrupt

    def recognize(_):
        if failure == 'ocr_error' and len(attempts) == 1:
            raise ValueError('constructed OCR failure')
        return '' if failure == 'ocr' and len(attempts) == 1 else '1234'

    assert execute(school, tmp_path, sleep=stop, recognize=recognize) == 130


@pytest.mark.parametrize('endpoint', [
    'queryActivityList.do', 'queryMyActivityList.do', 'vcode.do', 'addReservation.do'])
@pytest.mark.parametrize('timeout', [requests.ReadTimeout, requests.ConnectTimeout])
def test_timeout_never_persists_backoff_or_candidate_cooldown(tmp_path, monkeypatch, endpoint, timeout):
    school = School()
    school.activities = [lecture()]
    request = school.request
    failures = []

    def fail(method, url, **kwargs):
        if urlsplit(url).path.endswith(endpoint):
            failures.append(endpoint)
            raise timeout('constructed timeout')
        return request(method, url, **kwargs)

    monkeypatch.setattr(school, 'request', fail)

    def stop(seconds):
        saved = json.loads((tmp_path / 'task.json').read_text())
        assert saved['health']['phase'] != 'backoff'
        assert not saved.get('retry_at') and not saved['cooldowns']
        if endpoint == 'addReservation.do' and timeout is requests.ReadTimeout:
            assert saved['status'] == 'pending'
        raise KeyboardInterrupt

    assert execute(school, tmp_path, sleep=stop) == 130
    assert failures and set(failures) == {endpoint}


@pytest.mark.parametrize('server_error', [False, True])
def test_event_history_keeps_retrying_timeouts_without_failure_cap_or_backoff(server_error):
    calls, delays, recovered = [], [], threading.Event()
    gate = WaitGate()

    def query():
        calls.append(time.monotonic())
        if len(calls) <= 4:
            raise RemoteError('network_timeout')
        if server_error and len(calls) == 5:
            raise RemoteError('service_unavailable', http_status=503)
        recovered.set()
        return []

    def retry(count, snapshot):
        delays.append(count)
        return .01 if server_error else 1

    reader = ActivityPoller(query, interval=None, gate=gate,
                            retry_delay=retry, max_consecutive_failures=3)
    reader.start()
    try:
        assert recovered.wait(.5), 'timeouts must not back off or exhaust the history event'
        assert gate.remaining() == 0 and len(calls) == 5 + int(server_error)
        assert delays == ([1] if server_error else [])
    finally:
        reader.stop()


def test_list_timeout_keeps_regular_cadence_including_request_time():
    now, starts, waits = [0.0], [], []

    class Event:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            waits.append(seconds)
            now[0] += seconds
            return False

    def query():
        starts.append(now[0])
        now[0] += 2
        if len(starts) == 3:
            reader._stop.set()
            return []
        raise RemoteError('network_timeout')

    reader = ActivityPoller(query, interval=5, clock=lambda: now[0], retry_delay=lambda *_: 99)
    reader._stop = Event()
    reader._run()
    assert starts == [0, 5, 10]
    assert waits == [3, 3]
