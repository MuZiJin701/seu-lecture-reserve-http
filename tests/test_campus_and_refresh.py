import json
from urllib.parse import quote, urlencode, urlsplit

import pytest
import requests

import seu_reserve.app as app_module
from seu_reserve.app import Settings
from seu_reserve.client import AuthenticationRejected, Client, ENTRY
from seu_reserve.model import moment
from test_app import (FakeHistoryPoller, FakePoller, School, execute, interrupted,
                      lecture, native_activity, seed_opening_selection)


class CampusSchool(School):
    service = 'http://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do'

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        if path.endswith('loginRedirect'):
            self.requests.append((method, url, kwargs))
            self.authenticated = True
            response = requests.Response()
            response.url = url
            response.status_code = 302
            response.headers['Location'] = ENTRY + '?ticket=SYNTHETIC'
            return response
        response = super().request(method, url, **kwargs)
        if path.endswith('index.do') and not self.authenticated:
            response.headers['Location'] = 'https://auth.seu.edu.cn/dist/#/login?' + urlencode(
                {'service': self.service})
        elif path.endswith('casLogin') and method == 'POST':
            payload = json.loads(response.content)
            payload['redirectUrl'] = quote(self.service, safe='')
            response._content = json.dumps(payload).encode()
        return response


@pytest.mark.parametrize('sms', [False, True])
def test_campus_cas_login_does_not_visit_vpn(tmp_path, sms):
    school = CampusSchool(sms=sms)
    answers = iter(['send', '654321'])
    assert execute(school, tmp_path, check_only=True,
                   prompt=lambda _: next(answers)) == 0
    assert school.sms_sends == int(sms)
    assert not school.submitted
    assert not any(urlsplit(url).hostname == 'vpn.seu.edu.cn'
                   for _, url, _ in school.requests)
    logins = [kwargs['json'] for method, url, kwargs in school.requests
              if method == 'POST' and url.endswith('casLogin')]
    assert logins and all(login['service'] == school.service for login in logins)
    assert all(url.startswith('https://') and kwargs['verify']
               for _, url, kwargs in school.requests)


def test_campus_rejected_credentials_do_not_fall_back_to_vpn():
    school = CampusSchool()
    school.login_reply = {'code': 500, 'info': '用户名或密码错误'}
    client = Client(school, {'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
                    interrupted, lambda _: None)
    with pytest.raises(AuthenticationRejected):
        client.authenticate()
    assert not any(urlsplit(url).hostname == 'vpn.seu.edu.cn'
                   for _, url, _ in school.requests)


class CampusDirectUnavailable(CampusSchool):
    def __init__(self):
        super().__init__()
        self.login_redirects = 0

    def request(self, method, url, **kwargs):
        if urlsplit(url).path.endswith('loginRedirect'):
            self.login_redirects += 1
            if self.login_redirects == 1:
                self.requests.append((method, url, kwargs))
                response = requests.Response()
                response.url = url
                response.status_code = 200
                response._content = b'<html>not the application</html>'
                return response
        return super().request(method, url, **kwargs)


def test_campus_direct_failure_falls_back_to_gateway(tmp_path):
    school = CampusDirectUnavailable()
    assert execute(school, tmp_path, check_only=True) == 0
    assert school.login_redirects >= 2
    assert any(urlsplit(url).hostname == 'vpn.seu.edu.cn'
               for _, url, _ in school.requests)


class CampusNoService(School):
    def request(self, method, url, **kwargs):
        if method == 'GET' and urlsplit(url).path.endswith('index.do') and not self.authenticated:
            self.requests.append((method, url, kwargs))
            response = requests.Response()
            response.url = url
            response.status_code = 302
            response.headers['Location'] = 'https://auth.seu.edu.cn/dist/#/dist/main/login'
            return response
        return super().request(method, url, **kwargs)


def test_campus_missing_service_falls_back_to_gateway(tmp_path):
    school = CampusNoService()
    assert execute(school, tmp_path, check_only=True) == 0
    assert any(urlsplit(url).hostname == 'vpn.seu.edu.cn'
               for _, url, _ in school.requests)


class CampusSmsSendFails(CampusSchool):
    def __init__(self):
        super().__init__(sms=True)
        self.send_attempts = 0

    def request(self, method, url, **kwargs):
        if urlsplit(url).path.endswith('sendStage2Code'):
            self.send_attempts += 1
            response = requests.Response()
            response.url = url
            response.status_code = 200
            response._content = json.dumps({'code': 500}).encode()
            return response
        return super().request(method, url, **kwargs)


def test_sms_send_failure_limits_immediate_resends():
    school = CampusSmsSendFails()
    answers = iter(['send', '', 'send', '', '654321'])
    labels = []

    def prompt(label):
        labels.append(label)
        return next(answers, '654321')

    events = []
    client = Client(school, {'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
                    prompt, events.append)
    client.authenticate()
    assert school.send_attempts == 1
    assert any('短信发送失败' in label for label in labels)
    assert any('等待约' in label for label in labels)
    assert 'sms_send_failed' in events


def test_new_background_history_replaces_a_still_cached_history(tmp_path, monkeypatch):
    seed_opening_selection(tmp_path)
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    intervals = []

    def new_history_poller(query, **options):
        intervals.append(options['interval'])
        return FakeHistoryPoller(query, **options)

    monkeypatch.setattr(app_module, 'new_history_poller', new_history_poller)
    school = School()
    school.activities = [native_activity(opens='2026-10-18 12:00:10')]
    for key, value in dict(instances=[], rows_override=None, at_offset=0.0,
                           next_rows=school.activities, next_error=None,
                           error_retry_after=0.0, error_server_directed=False,
                           next_wake_once=False).items():
        monkeypatch.setattr(FakePoller, key, value)
    for key, value in dict(rows=[], error=None, retry_after=0.0,
                           server_directed=False, seconds=0.0, events=[]).items():
        monkeypatch.setattr(FakeHistoryPoller, key, value)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []

    def advance(seconds):
        waits.append(seconds)
        if len(waits) > 1:
            raise KeyboardInterrupt
        now[0] += 12
        FakeHistoryPoller.rows = [lecture('external', YYZT='已预约')]

    monkeypatch.setattr(FakePoller, 'on_timeout', advance)
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=interrupted,
                   background_poller=True,
                   settings=Settings(state_dir=tmp_path, prewarm=False,
                                     captcha_prefetch=False)) == 130
    assert not school.submitted
    assert intervals and all(interval == 5 for interval in intervals)
    assert FakePoller.instances and all(poller.interval == .10 for poller in FakePoller.instances)
    assert not any(url.endswith('queryMyActivityList.do') for _, url, _ in school.requests)
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['selection']['activities'][0]['reason'] == 'time_conflict'
