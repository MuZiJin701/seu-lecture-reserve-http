import base64
import json
import socket
import sys
import threading
import types
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from seu_reserve.app import (Settings, normalize_hold_deadlines, pending_hold_until,
                             progress_rows, resolve_pending, run, scan_delay)
from seu_reserve.model import moment, progress


class School:
    """Controlled external HTTP service; never opens a socket."""

    def __init__(self, *, sms=False):
        self.sms = sms
        self.authenticated = False
        self.requests = []
        self.activities = []
        self.mine = []
        self.hdrs = []
        self.server_now = None
        self.submitted = []
        self.submit_timeout = False
        self.query_statuses = []
        self.submit_replies = []
        self.sms_sends = 0
        self.login_reply = None
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        path = urlsplit(url).path
        response = requests.Response()
        response.url = url
        response.status_code = 200
        payload = {"code": 0, "data": {}}
        if path.endswith('index.do') and self.authenticated:
            response._content = b'<script src="indexjsp.js"></script>'
            return response
        if path.endswith('index.do') or path.endswith('/casLogin') and method == 'GET':
            response.status_code = 302
            response.headers['Location'] = 'https://auth.seu.edu.cn/dist/#/login?service=https%3A%2F%2Fvpn.seu.edu.cn%2Fcallback'
        elif path == '/dist/':
            response._content = b'<html>CAS</html>'
            return response
        elif path.endswith('authConfig'):
            payload = {"code": 0, "data": {"security": {"csrfToken": "test-csrf"}, "isLogin": int(self.authenticated)}}
        elif path.endswith('getChiperKey'):
            public = self.key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
            payload = {"success": True, "publicKey": base64.urlsafe_b64encode(public).decode()}
        elif path.endswith('casLogin'):
            encrypted = kwargs['json'].get('mobileVerifyCode')
            if self.sms and encrypted:
                self.sms = self.key.decrypt(base64.b64decode(encrypted), padding.PKCS1v15()).decode() != '654321'
            payload = {"code": 502 if self.sms else 200, "success": not self.sms,
                       "redirectUrl": 'https%3A%2F%2Fvpn.seu.edu.cn%2Fcallback'}
            if self.login_reply is not None:
                payload = self.login_reply
        elif path.endswith('sendStage2Code'):
            assert kwargs['json'] == {'userId': 'test-user'}
            self.sms_sends += 1
            payload = {'code': 200}
        elif path.endswith('loginRedirect'):
            assert parse_qs(urlsplit(url).query)['redirectUrl'] == ['https://vpn.seu.edu.cn/callback']
            self.authenticated = True
        elif path.endswith('queryActivityList.do'):
            if self.query_statuses:
                response.status_code = self.query_statuses.pop(0)
            datas = {'hdlbList': self.activities}
            if self.hdrs:
                datas['hdrsList'] = self.hdrs
            if self.server_now is not None:
                datas['xtdqsj'] = self.server_now
            payload = {'code': 0, 'datas': datas}
        elif path.endswith('queryMyActivityList.do'):
            payload = {'code': 0, 'datas': {'wdhdList': self.mine}}
        elif path.endswith('vcode.do'):
            import io
            from PIL import Image
            image = io.BytesIO()
            Image.new('RGB', (50, 20), 'white').save(image, format='PNG')
            payload = {'code': 0, 'datas': {'vcode': 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()}}
        elif path.endswith('addReservation.do'):
            wid = kwargs['data']['wid']
            self.submitted.append(wid)
            if self.submit_timeout:
                raise requests.Timeout('SECRET must never enter logs')
            if self.submit_replies:
                payload = self.submit_replies.pop(0)
                response._content = json.dumps(payload).encode()
                return response
            self.mine.append({**next(row for row in self.activities if row.get('WID') == wid), 'YYZT': '已预约', 'HD_WID': wid})
            payload = {'code': 0}
        elif path.endswith('needCaptcha'):
            payload = {"code": 200}
        response._content = json.dumps(payload).encode()
        return response


def interrupted(_):
    raise KeyboardInterrupt


def test_sms_authentication_never_queries_or_submits_business(tmp_path):
    school = School(sms=True)
    notifications = []
    result = run(Settings(state_dir=tmp_path), background_poller=False, session=school,
                 environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
                 prompt=interrupted, notify=notifications.append)
    assert result == 130
    assert 'authentication_required' in notifications
    assert not any('/modules/hdyy/' in url for _, url, _ in school.requests)


@pytest.mark.parametrize('check_only', [False, True])
def test_initial_gateway_dns_failure_restarts_authentication_through_app(tmp_path, check_only, capsys):
    class GatewaySchool(School):
        def __init__(self):
            super().__init__()
            self.mounts = []
            self.gateway_attempts = []
        def mount(self, prefix, adapter):
            self.mounts.append((prefix, adapter))
        def request(self, method, url, **kwargs):
            if url.startswith('https://vpn.seu.edu.cn/'):
                self.gateway_attempts.append(bool(self.mounts))
                if not self.mounts:
                    raise requests.ConnectionError(socket.gaierror('SECRET DNS failure'))
            return super().request(method, url, **kwargs)
    school = GatewaySchool()
    school.activities = [lecture()]
    school.submit_timeout = True
    options = dict(session=school, background_poller=False, environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password',
                   'SEU_VPN_IP': '192.0.2.1'}, sleep=interrupted, prompt=interrupted,
                   recognize=lambda _: '1234', notify=lambda _: None)
    result = run(Settings(state_dir=tmp_path), check_only=check_only, **options)
    assert result == (0 if check_only else 130)
    assert school.gateway_attempts[0] is False
    assert all(school.gateway_attempts[1:])
    assert [prefix for prefix, _ in school.mounts] == [
        'https://vpn.seu.edu.cn/', 'https://vpn.seu.edu.cn:443/']
    assert school.mounts[0][1] is school.mounts[1][1]
    assert school.submitted == ([] if check_only else ['one'])
    assert 'SECRET' not in capsys.readouterr().out
    if not check_only:
        assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending'
        assert run(Settings(state_dir=tmp_path), **options) == 130
        assert school.submitted == ['one']


def seed_pending(school, directory):
    """Stop at the persisted submit, before the first verification query."""
    def pending():
        path = directory / 'task.json'
        return path.exists() and json.loads(path.read_text()).get('status') == 'pending'
    return execute(school, directory, stop=pending)


def lecture(wid='one', **changes):
    return {'WID': wid, 'HDMC': '测试讲座', 'HDLX_DISPLAY': '人文与科学素养系列讲座_心理健康',
            'KSSJ': '2030-01-01 10:00:00', 'JSSJ': '2030-01-01 11:00:00',
            'IS_ONLINE': True, 'JZDD': '线上', 'YYZT': '预约', 'SFDK': '0', 'SFWY': '0', **changes}


def execute(school, directory, **options):
    options.setdefault('clock', lambda: moment('2026-10-18 12:00:00').timestamp())
    options.setdefault('sleep', interrupted)
    options.setdefault('notify', lambda _: None)
    options.setdefault('prompt', interrupted)
    options.setdefault('recognize', lambda image: '1234')
    # The background poller adds real threads and requests; tests exercise it explicitly.
    options.setdefault('background_poller', False)
    if options['background_poller']:
        original_clock = options['clock']
        previous_time, same_time_calls, clock_calls, tripped = None, 0, 0, False
        def bounded_clock():
            nonlocal previous_time, same_time_calls, clock_calls, tripped
            clock_calls += 1
            current = original_clock()
            same_time_calls = same_time_calls + 1 if current == previous_time else 0
            previous_time = current
            if (same_time_calls > 1000 or clock_calls > 20000) and not tripped:
                tripped = True
                raise AssertionError('constructed reader did not advance the injected clock')
            return current
        options['clock'] = bounded_clock
    def wait_event(event, seconds):
        if event.is_set():
            return True
        fake = globals().get('FakePoller')
        readers = getattr(fake, 'instances', [])
        if readers and getattr(fake, 'on_timeout', None) is not None:
            return readers[-1].wait_update(seconds)
        options['sleep'](seconds)
        return False
    options.setdefault('wait_for_update', wait_event)
    settings = options.pop('settings', Settings(state_dir=directory))
    import seu_reserve.app as app_module
    from unittest.mock import patch
    def invoke():
        if (options['background_poller'] and isinstance(app_module.ActivityPoller, type)
                and issubclass(app_module.ActivityPoller, FakePoller)):
            from concurrent.futures import Future
            # Constructed clocks/readers use immediate jobs; production scheduling has real-thread tests.
            class ConstructedCaptcha(app_module.CaptchaWorker):
                def __init__(self, on_update):
                    self.number = 0
                    self._current = None
                    self.on_update = on_update
                def submit(self, prepare, *, generation):
                    self.number += 1
                    self.generation = generation
                    self._current = Future()
                    try:
                        self._current.set_result(prepare())
                    except BaseException as exc:
                        self._current.set_exception(exc)
                    self.on_update()
                def close(self):
                    pass
            with patch.object(app_module, 'CaptchaWorker', ConstructedCaptcha):
                return run(settings, session=school,
                           environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'}, **options)
        return run(settings, session=school,
                   environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'}, **options)
    if (options['background_poller'] and isinstance(app_module.ActivityPoller, type)
            and issubclass(app_module.ActivityPoller, FakePoller)
            and app_module.new_history_poller.__module__ == 'seu_reserve.app'):
        class ConstructedHistory(FakeHistoryPoller):
            def __init__(self, query, **kwargs):
                super().__init__(query, **kwargs)
                self.latest = None
                self.source = app_module.Client(school, {}, interrupted, lambda _: None)
            def start(self):
                super().start()
                self.request_refresh()
            def request_refresh(self):
                from seu_reserve.poller import Snapshot, error_reason, server_directed_error
                self.polls += 1
                started = self.clock()
                try:
                    rows = self.source.mine()
                    self.latest = Snapshot(rows, self.clock(), started=started)
                except Exception as error:
                    self.latest = Snapshot([], self.clock(), error=type(error).__name__,
                        reason=error_reason(error), retry_after=getattr(error,'retry_after',0),
                        server_directed=server_directed_error(error), failed_at=self.clock(), started=started)
                return self.polls
            def snapshot(self):
                return self.latest
        with patch.object(app_module, 'new_history_poller', ConstructedHistory):
            return invoke()
    return invoke()


def test_one_confirmed_reservation_continues_goal_and_restart_does_not_duplicate(tmp_path):
    school = School()
    school.activities = [lecture(), lecture('two')]
    events = []
    assert execute(school, tmp_path, notify=events.append) == 130
    assert school.submitted == ['one']
    assert events == []
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()
    assert execute(school, tmp_path, notify=events.append) == 130
    assert school.submitted == ['one']


def test_success_mail_is_sent_asynchronously_when_enabled(tmp_path):
    school = School()
    school.activities = [lecture()]
    events = []
    sent = threading.Event()
    def notify(event):
        events.append(event)
        sent.set()
    assert execute(school, tmp_path, settings=Settings(state_dir=tmp_path, mail_success=True),
                   notify=notify) == 130
    assert sent.wait(2)
    assert events == ['reservation_succeeded']


def test_unknown_submission_is_queried_and_survives_restart_without_resubmission(tmp_path, capsys):
    school = School()
    school.activities = [lecture(), lecture('two')]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']
    school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约'}]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']
    assert 'SECRET' not in capsys.readouterr().out


def test_school_reservation_fields_preserve_conflicts_including_touching_times(tmp_path):
    school = School()
    school.mine = [{'WID': 'reservation-row', 'HD_WID': 'old', 'YY_WID': 'reservation-id',
                    'FBZT': '1', 'SFDK': '1', 'SFWY': '0', 'JZXL_DISPLAY': '人文与科学素养系列讲座_心理健康',
                    'JZSJ': '2030-01-01 09:00:00', 'HDJSSJ': '2030-01-01 10:00:00'}]
    school.activities = [lecture('touching'),
                         lecture('later', KSSJ='2030-01-01 11:00:00', JSSJ='2030-01-01 12:00:00')]
    assert execute(school, tmp_path, sleep=interrupted) == 130
    assert school.submitted == ['later']


def test_school_cancelled_activity_does_not_block_booking(tmp_path):
    school = School()
    school.mine = [{'WID': 'reservation-row', 'HD_WID': 'one', 'FBZT': '-1'}]
    school.activities = [lecture()]
    assert execute(school, tmp_path, sleep=interrupted) == 130
    assert school.submitted == ['one']


@pytest.mark.parametrize('changes', [{'FBZT': 'unknown'}, {'HD_WID': None}, {'YYZT': 'unknown'},
                                  {'JZSJ': 'invalid'}, {'HDJSSJ': None}])
def test_unknown_school_reservation_fields_still_block_submission(tmp_path, changes):
    school = School()
    school.mine = [{'WID': 'reservation-row', 'HD_WID': 'old', 'FBZT': '1',
                    'JZSJ': '2030-01-01 09:00:00', 'HDJSSJ': '2030-01-01 10:00:00', **changes}]
    school.activities = [lecture()]
    assert execute(school, tmp_path, sleep=interrupted) == 130
    assert not school.submitted


def test_pending_submission_is_confirmed_by_school_activity_id_after_restart(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    school.mine = [{'WID': 'reservation-row', 'HD_WID': 'one', 'YY_WID': 'reservation-id', 'FBZT': '1',
                    'SFDK': '0', 'SFWY': '0', 'JZXL_DISPLAY': '人文与科学素养系列讲座_心理健康',
                    'JZSJ': '2030-01-01 10:00:00', 'HDJSSJ': '2030-01-01 11:00:00'}]
    assert execute(school, tmp_path, sleep=interrupted) == 130
    assert school.submitted == ['one']


def test_query_failure_alerts_once_after_five_minutes_and_recovery_clears_it(tmp_path):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    school.query_statuses = [503] * 400
    events, delays = [], []
    now = 1000.0
    def wait(seconds):
        nonlocal now
        delays.append(seconds)
        now += seconds
        if events or len(delays) > 200:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, sleep=wait, clock=lambda: now) == 130
    assert events == ['query_failure']
    assert delays[:4] == [1, 2, 4, 5]
    assert max(delays) <= 5
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['query_failure_since'] == 1000 and 'query_failure' in saved['alerts']
    saved['retry_at'] = 0
    (tmp_path / 'task.json').write_text(json.dumps(saved))
    school.query_statuses = []
    delays.clear()
    def stop(seconds):
        delays.append(seconds)
        raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, sleep=stop, clock=lambda: now) == 130
    assert events == ['query_failure']
    assert 'event=query_recovered' in (tmp_path / 'events.log').read_text()
    recovered = json.loads((tmp_path / 'task.json').read_text())
    assert 'query_failure_since' not in recovered and 'query_failure' not in recovered.get('alerts', {})


def test_four_explicit_captcha_rejections_try_the_next_candidate_without_cooldown(tmp_path):
    school = School()
    school.activities = [lecture(), lecture('two')]
    school.submit_replies = [{'code': 1, 'msg': '验证码错误'}] * 4
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one', 'one', 'one', 'one', 'two']


def test_unsupported_activity_schema_alerts_instead_of_looking_like_empty_list(tmp_path):
    school = School()
    school.activities = [lecture(IS_ONLINE=None)]
    events = []
    waits = []
    def stop_after_three(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, sleep=stop_after_three) == 130
    assert events == []
    assert school.submitted == []


@pytest.mark.parametrize('mail', [
    {'SMTP_USERNAME': 'sender@gmail.com'},
    {'SMTP_PASSWORD': 'SECRET-app-password'},
    {'SMTP_USERNAME': '@gmail.com', 'SMTP_PASSWORD': 'SECRET-app-password'},
    {'SMTP_USERNAME': 'sender@@gmail.com', 'SMTP_PASSWORD': 'SECRET-app-password'},
    {'SMTP_USERNAME': 'sender@sub.qq.com', 'SMTP_PASSWORD': 'SECRET'},
    {'SMTP_USERNAME': 'sender@qq.com.evil.test', 'SMTP_PASSWORD': 'SECRET'},
    {'SMTP_USERNAME': 'sender@foxmail.com', 'SMTP_PASSWORD': 'SECRET'},
    {'SMTP_USERNAME': 'sender@example.org', 'SMTP_PASSWORD': 'SECRET-app-password'},
    {'SMTP_USERNAME': 'sender@gmail.com', 'SMTP_PASSWORD': 'SECRET-app-password', 'MAIL_TO': 'invalid'},
    {'SMTP_PORT': 'SECRET-invalid-port'},
    {'SMTP_PORT': '587'},
    {'SMTP_HOST': 'smtp.example.org', 'MAIL_FROM': 'sender@example.org',
     'MAIL_TO': 'receiver@example.org', 'SMTP_PORT': 'SECRET-invalid-port'},
    {'SMTP_HOST': 'smtp.example.org', 'MAIL_FROM': 'sender@example.org',
     'MAIL_TO': 'receiver@example.org', 'SMTP_USERNAME': 'SECRET-user'},
])
def test_invalid_mail_configuration_explains_startup_failure_without_network(tmp_path, capsys, mail):
    school = School()
    env = {'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'SECRET-password', **mail}
    assert run(Settings(state_dir=tmp_path), background_poller=False, session=school, environ=env) == 1
    output = capsys.readouterr().out
    assert 'event=mail_configuration_invalid' in output
    assert all(key in output for key in ('.env', 'SMTP_HOST', 'MAIL_FROM', 'MAIL_TO'))
    assert 'SECRET' not in output
    assert not school.requests


@pytest.mark.parametrize('mail', [{}, {
    'SMTP_HOST': '', 'SMTP_PORT': '465', 'SMTP_USERNAME': '',
    'SMTP_PASSWORD': '', 'MAIL_FROM': '', 'MAIL_TO': '',
}])
def test_no_mail_configuration_still_books_and_records_success(tmp_path, monkeypatch, capsys, mail):
    def unexpected_smtp(*args, **kwargs):
        pytest.fail('disabled mail must not open an SMTP connection')
    monkeypatch.setattr('smtplib.SMTP_SSL', unexpected_smtp)
    school = School()
    school.activities = [lecture()]
    env = {'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password', **mail}
    assert run(Settings(state_dir=tmp_path), background_poller=False, session=school, environ=env,
               recognize=lambda _: '1234', sleep=interrupted, clock=lambda: 1000) == 130
    assert school.submitted == ['one']
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'scanning'
    assert 'event=reservation_succeeded' in capsys.readouterr().out
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


@pytest.mark.parametrize('username', [None, 'sender@gmail.com', 'sender@qq.com',
                                     'sender@163.com', 'sender@126.com'])
def test_default_email_contains_only_event_and_mail_failure_does_not_repeat_booking(
        tmp_path, monkeypatch, capsys, username):
    messages = []
    logins = []
    class SMTP:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == ('smtp.example.org', 2465)
            assert kwargs['context'].check_hostname
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def login(self, username, password):
            logins.append((username, password))
        def send_message(self, message):
            messages.append(message)
            raise OSError('SECRET mail password')
    monkeypatch.setattr('smtplib.SMTP_SSL', SMTP)
    class FailedAfterBooking(School):
        fail_activities = True
        def request(self, method, url, **kwargs):
            if self.fail_activities and self.submitted and url.endswith('queryActivityList.do'):
                raise OSError('SECRET disk or network failure')
            return super().request(method, url, **kwargs)
    school = FailedAfterBooking()
    school.activities = [lecture(HDMC='PRIVATE TITLE')]
    env = {'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password',
           'SMTP_HOST': 'smtp.example.org', 'SMTP_PORT': '2465',
           'MAIL_FROM': 'sender@example.org', 'MAIL_TO': 'receiver@example.org'}
    if username:
        env.update(SMTP_USERNAME=username, SMTP_PASSWORD='private-mail-password')
    assert run(Settings(state_dir=tmp_path), background_poller=False, session=school, environ=env,
               recognize=lambda _: '1234', sleep=interrupted, clock=lambda: 1000) == 1
    assert len(messages) == 1
    assert logins == ([(username, 'private-mail-password')] if username else [])
    assert messages[0]['From'] == 'sender@example.org'
    assert messages[0]['To'] == 'receiver@example.org'
    assert 'task_failed' in messages[0].get_content()
    assert 'PRIVATE TITLE' not in messages[0].get_content()
    assert 'SECRET' not in capsys.readouterr().out
    assert 'event=notification_failed' in (tmp_path / 'events.log').read_text()
    school.fail_activities = False
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']


def test_read_only_check_fetches_captcha_without_creating_task_or_submitting(tmp_path):
    school = School()
    school.activities = [lecture()]
    directory = tmp_path / 'unused-state'
    assert execute(school, directory, check_only=True) == 0
    assert school.submitted == []
    assert any(urlsplit(url).path.endswith('vcode.do') for _, url, _ in school.requests)
    assert not directory.exists()


@pytest.mark.parametrize('endpoint, stage', [
    ('index.do', 'authentication'),
    ('queryMyActivityList.do', 'my_activities'),
    ('queryActivityList.do', 'activities'),
    ('vcode.do', 'captcha'),
])
def test_read_only_http_failure_reports_stage_and_status_without_response_data(
        tmp_path, capsys, endpoint, stage):
    class FailedSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if urlsplit(url).path.endswith(endpoint):
                response.status_code = 502
                response._content = b'SECRET response'
            return response
    school = FailedSchool()
    directory = tmp_path / 'unused-state'
    assert execute(school, directory, check_only=True) == 1
    assert capsys.readouterr().out.splitlines()[-1] == (
        f'event=task_failed stage={stage} reason=service_unavailable http_status=502')
    assert not directory.exists()
    assert not school.submitted


@pytest.mark.parametrize('error, reason', [
    (requests.exceptions.SSLError, 'tls_failure'),
    (requests.exceptions.ProxyError, 'proxy_failure'),
    (requests.Timeout, 'network_timeout'),
    (requests.ConnectionError, 'connection_failure'),
    (requests.RequestException, 'network_failure'),
])
def test_read_only_network_failure_reports_safe_reason(tmp_path, capsys, error, reason):
    class FailedSchool(School):
        def request(self, method, url, **kwargs):
            assert kwargs['verify'] is True
            raise error('SECRET URL, account, password and ticket')
    directory = tmp_path / 'unused-state'
    assert execute(FailedSchool(), directory, check_only=True) == 1
    extra = ' host=ehall.seu.edu.cn tls_reason=unknown' if error is requests.exceptions.SSLError else ''
    assert capsys.readouterr().out == f'event=task_failed stage=authentication reason={reason}{extra}\n'
    assert not directory.exists()


@pytest.mark.parametrize('tls_reason, proxy_used', [
    ('CERTIFICATE_VERIFY_FAILED', False),
    ('UNSAFE_LEGACY_RENEGOTIATION_DISABLED', False),
    ('UNEXPECTED_EOF_WHILE_READING', True),
    ('DH_KEY_TOO_SMALL', False),
    ('SSLV3_ALERT_HANDSHAKE_FAILURE', True),
    ('TLSV1_ALERT_PROTOCOL_VERSION', False),
    ('WRONG_VERSION_NUMBER', True),
])
def test_read_only_tls_diagnostic_does_not_expose_proxy_or_exception(
        tmp_path, capsys, monkeypatch, tls_reason, proxy_used):
    with requests.Session() as session:
        session.trust_env = False
        if proxy_used:
            session.proxies = {'https': 'http://SECRET:SECRET@proxy.example:1234'}
        def fail(request, **kwargs):
            assert kwargs['verify'] is True
            assert bool(requests.utils.select_proxy(request.url, kwargs['proxies'])) == proxy_used
            raise requests.exceptions.SSLError(f'[SSL: {tls_reason}] SECRET URL and ticket')
        monkeypatch.setattr(session, 'send', fail)
        directory = tmp_path / 'unused-state'
        assert run(Settings(state_dir=directory), background_poller=False, session=session, environ={}, check_only=True) == 1
    assert capsys.readouterr().out == (
        'event=task_failed stage=authentication reason=tls_failure host=ehall.seu.edu.cn '
        f'tls_reason={tls_reason.lower()} proxy_used={str(proxy_used).lower()}\n')
    assert not directory.exists()


@pytest.mark.parametrize('error', [ValueError, OSError])
def test_read_only_local_error_does_not_echo_exception(tmp_path, capsys, monkeypatch, error):
    def fail(_):
        raise error('SECRET input')
    monkeypatch.setattr('seu_reserve.client.Client.mine', fail)
    assert execute(School(), tmp_path / 'unused-state', check_only=True) == 1
    assert capsys.readouterr().out == (
        f'event=task_failed stage=my_activities reason={error.__name__}\n')


def test_read_only_check_passes_cas_browser_user_agent_requirement(tmp_path, capsys):
    class BrowserOnlySchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if urlsplit(url).path == '/dist/':
                if not kwargs.get('headers', {}).get('User-Agent', '').startswith('Mozilla/5.0'):
                    response.status_code = 403
            return response

    school = BrowserOnlySchool()
    assert execute(school, tmp_path / 'unused-state', check_only=True) == 0
    assert 'event=read_only_complete' in capsys.readouterr().out
    assert not school.submitted
    gateway_headers = [options['headers'] for _, url, options in school.requests
                       if 'x-csrf-token' in options.get('headers', {})]
    assert gateway_headers
    assert all(headers['x-csrf-token'] == 'test-csrf'
               and headers['Referer'].startswith('https://auth.seu.edu.cn/')
               for headers in gateway_headers)


def test_sms_is_only_sent_on_user_command_and_challenge_email_is_not_repeated(tmp_path):
    school = School(sms=True)
    school.activities = [lecture()]
    answers = iter(['send', '111111', '654321'])
    events = []
    assert execute(school, tmp_path, prompt=lambda _: next(answers), notify=events.append) == 130
    assert school.sms_sends == 1
    assert events == ['authentication_required']
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


def test_unknown_authentication_challenge_waits_without_business_requests(tmp_path):
    school = School()
    school.login_reply = {'code': 499, 'info': 'unmodeled challenge'}
    events = []
    assert execute(school, tmp_path, notify=events.append) == 130
    assert events == ['authentication_required']
    assert not school.submitted


@pytest.mark.parametrize('payload', [
    {'code': False, 'datas': {'hdlbList': [lecture()]}},
    {'code': 0, 'datas': {'hdlbList': None}},
    {'code': 0, 'datas': {'hdlbList': [lecture()], 'total': 2}},
])
def test_invalid_business_response_never_allows_submission(tmp_path, payload):
    class InvalidSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('queryActivityList.do'):
                response._content = json.dumps(payload).encode()
            return response
    school = InvalidSchool()
    school.activities = [lecture()]
    assert execute(school, tmp_path, sleep=interrupted) == 130
    assert school.submitted == []


@pytest.mark.parametrize('changes, filters, mine', [
    ({'HDLX_DISPLAY': '未允许类别'}, {'titles': ('测试讲座',)}, []),
    ({'IS_ONLINE': False, 'JZDD': '无锡校区教室'}, {'titles': ('测试讲座',)}, []),
    ({}, {'titles': ('其他标题',)}, []),
    ({}, {'online': False}, []),
    ({'IS_ONLINE': False, 'JZDD': '无锡校区教室'}, {'offline': False, 'campuses': ('无锡校区',)}, []),
    ({'KSSJ': 'invalid'}, {}, []),
    ({}, {}, [{**lecture('old'), 'YYZT': '已预约', 'JSSJ': '2030-01-01 10:00:00', 'KSSJ': '2030-01-01 09:00:00'}]),
    ({}, {}, [{**lecture('old'), 'YYZT': '已预约', 'KSSJ': 'unknown'}]),
    ({}, {}, [{**lecture('old'), 'YYZT': 'unknown'}]),
])
def test_all_filters_and_conflict_uncertainty_block_booking(tmp_path, changes, filters, mine):
    school = School()
    school.activities = [lecture(**changes)]
    school.mine = mine
    assert execute(school, tmp_path, settings=Settings(state_dir=tmp_path, **filters), sleep=interrupted) == 130
    assert school.submitted == []


def test_category_order_precedes_time_and_title_order(tmp_path):
    school = School()
    school.activities = [lecture('early', HDMC='first title', HDLX_DISPLAY='人文与科学素养系列讲座_法律'),
                         lecture('late', HDMC='second title', KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 11:00:00')]
    settings = Settings(state_dir=tmp_path, titles=('first title', 'second title'))
    assert execute(school, tmp_path, settings=settings) == 130
    assert school.submitted == ['late', 'early']


def test_new_task_only_replaces_completed_task_and_preserves_pending_submission(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    assert execute(school, tmp_path, new_task=True) == 1
    assert school.submitted == ['one']
    school.mine = [{**lecture(), 'YYZT': '已预约'}]
    assert execute(school, tmp_path) == 130
    school.submit_timeout = False
    school.activities = [lecture('two', KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 11:00:00')]
    assert execute(school, tmp_path, new_task=True) == 1
    assert school.submitted == ['one']


def test_corrupt_checkpoint_stops_before_any_network_request(tmp_path):
    school = School()
    (tmp_path / 'task.json').write_text('{invalid')
    assert execute(school, tmp_path) == 1
    assert not school.requests


@pytest.mark.parametrize('target', [
    'http://auth.seu.edu.cn/unsafe', 'https://attacker.example/collect?ticket=SECRET',
    'https://auth.seu.edu.cn@attacker.example/collect', 'https://auth.seu.edu.cn:8443/collect',
])
def test_redirects_cannot_send_credentials_or_tickets_to_untrusted_origins(tmp_path, target, capsys):
    class RedirectSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('index.do'):
                response.headers['Location'] = target
            return response
    school = RedirectSchool()
    assert execute(school, tmp_path, sleep=interrupted) == 130
    assert len(school.requests) == 1
    assert 'SECRET' not in capsys.readouterr().out


def test_server_retry_after_is_respected(tmp_path):
    class LimitedSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('queryActivityList.do'):
                response.status_code = 429
                response.headers['Retry-After'] = '600'
            return response
    delays = []
    def stop(seconds):
        delays.append(seconds)
        raise KeyboardInterrupt
    assert execute(LimitedSchool(), tmp_path, sleep=stop, notify=None) == 130
    assert delays == [600]


def test_explicit_bad_credentials_stop_without_repeated_login(tmp_path):
    school = School()
    school.login_reply = {'code': 500, 'info': '用户名或密码错误'}
    events = []
    assert execute(school, tmp_path, notify=events.append) == 1
    assert events == ['authentication_rejected']
    lines = (tmp_path / 'events.log').read_text().splitlines()
    assert sum(line.split(' ', 1)[-1] == 'event=authentication_rejected' for line in lines) == 1
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert 'task_failed' in saved['alerts']
    assert 'authentication_rejected' not in saved['alerts']
    assert len([url for method, url, _ in school.requests if method == 'POST' and url.endswith('casLogin')]) == 1

    # A restart that is rejected again must still write only one event line: the persisted alert
    # suppresses the mail, not the per-process event-record dedupe.
    rejected = School()
    rejected.login_reply = {'code': 500, 'info': '用户名或密码错误'}
    before = (tmp_path / 'events.log').read_text()
    assert execute(rejected, tmp_path, notify=events.append) == 1
    appended = (tmp_path / 'events.log').read_text()[len(before):]
    assert appended.count('event=authentication_rejected') == 1
    assert events == ['authentication_rejected']  # the persisted alert does not resend the mail

    # Correcting the credentials must restart normally: the rejection is persisted under the
    # existing task_failed dedupe key, not an unknown state key.
    school.login_reply = None
    assert execute(school, tmp_path, notify=events.append) == 130
    assert 'event=state_invalid' not in (tmp_path / 'events.log').read_text()


def test_legacy_authentication_rejected_alert_is_migrated_on_restart(tmp_path):
    assert execute(School(), tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    # A build with the regression briefly wrote this unknown key; loading must normalize it
    # instead of bricking the account with state_invalid.
    saved['alerts'] = {'authentication_rejected': 1000}
    path.write_text(json.dumps(saved))
    from seu_reserve.state import Task
    task = Task(tmp_path, 'test-user')
    try:
        migrated = task.data['alerts']
        assert migrated.get('task_failed') == 1000
        assert 'authentication_rejected' not in migrated
    finally:
        task.close()
    assert execute(School(), tmp_path) == 130
    assert 'event=state_invalid' not in (tmp_path / 'events.log').read_text()


def test_empty_ocr_output_has_bounded_attempts_and_does_not_block_other_candidates(tmp_path):
    school = School()
    school.activities = [lecture(), lecture('two')]
    answers = iter(['', '', '', '', '1234'])
    assert execute(school, tmp_path, recognize=lambda _: next(answers)) == 130
    assert school.submitted == ['two']


def test_gateway_steps_and_application_cas_reuse_preserve_service_but_use_https(tmp_path, capsys):
    class GatewaySchool(School):
        application_ready = False
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            path = urlsplit(url).path
            if path.endswith('loginRedirect'):
                response.status_code = 302
                query = urlencode({'nextService': 'auth/authCheck', 'taskId': 'test-task',
                                   'data': json.dumps({'env': {'need': True}, 'ticket': 'SIMULATED_TICKET'})})
                response.headers['Location'] = 'https://vpn.seu.edu.cn/callback?' + query
            elif path.endswith('index.do') and self.authenticated and not self.application_ready:
                response.status_code = 302
                response.headers['Location'] = 'https://auth.seu.edu.cn/dist/#/login?' + urlencode({'service': 'http://ehall.seu.edu.cn/app'})
            elif path.endswith('verifyTgt'):
                assert kwargs['json']['service'] == 'http://ehall.seu.edu.cn/app'
                self.application_ready = True
                response._content = json.dumps({'code': 201, 'redirectUrl': 'http://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do?ticket=SIMULATED_TICKET'}).encode()
            elif path.endswith('reportEnv'):
                assert kwargs['json']['ticket'] == 'SIMULATED_TICKET'
                assert kwargs['json']['env']['endpoint']['device']['type'] == 'browser'
            return response
    school = GatewaySchool()
    school.activities = [lecture()]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']
    assert any(url.endswith('reportEnv') for _, url, _ in school.requests)
    assert any(url.endswith('auth/authCheck') for _, url, _ in school.requests)
    assert all(url.startswith('https://') and kwargs['verify'] for _, url, kwargs in school.requests)
    assert 'SIMULATED_TICKET' not in capsys.readouterr().out


def test_rate_limited_submission_waits_before_any_confirmation_query(tmp_path):
    class LimitedSubmission(School):
        limited = False
        queries_after_limit = 0
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('addReservation.do'):
                self.limited = True
                response.status_code = 429
                response.headers['Retry-After'] = '600'
            elif self.limited and url.endswith('queryMyActivityList.do'):
                self.queries_after_limit += 1
            return response
    school = LimitedSubmission()
    school.activities = [lecture()]
    delays, events = [], []
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def stop(seconds):
        delays.append(seconds)
        now[0] += seconds
        if len(delays) == 2:
            raise KeyboardInterrupt
    execute(school, tmp_path, sleep=stop, notify=events.append, clock=lambda: now[0])
    assert delays == [300, 300]
    assert events == ['reservation_step_failure']
    assert school.queries_after_limit == 0


def test_short_rate_limit_retry_after_is_not_inflated_to_twenty_seconds(tmp_path):
    class LimitedSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('queryActivityList.do'):
                response.status_code = 429
                response.headers['Retry-After'] = '1'
            return response
    delays = []
    def stop(seconds):
        delays.append(seconds)
        raise KeyboardInterrupt
    assert execute(LimitedSchool(), tmp_path, sleep=stop) == 130
    assert delays == [1]


def test_persistent_activity_query_failure_backs_off_and_notifies(tmp_path):
    class FailedActivities(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('queryActivityList.do'):
                response.status_code = 503
            return response
    school = FailedActivities()
    school.activities = [lecture()]
    delays, events = [], []
    def stop(seconds):
        delays.append(seconds)
        if len(delays) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop, notify=events.append) == 130
    assert delays == [1, 2, 4]
    assert events == []


def test_scan_delay_uses_random_phases_and_wakes_at_each_boundary(monkeypatch):
    import seu_reserve.app as app_module
    monkeypatch.setattr(app_module.random, 'uniform', lambda low, high: high)
    start = moment('2030-01-01 10:00:00').timestamp()
    details = [dict(title='讲座', reason='reservation_not_started',
                    reservation_start='2030-01-01T10:00:00+08:00',
                    reservation_end='2030-01-01T12:00:00+08:00')]
    assert scan_delay(details, start - 100, 0, 5) == (6, False)
    assert scan_delay(details, start - 31, 0.2, 5) == (1, False)
    assert scan_delay(details, start - 30, 0, 5) == (1.5, True)
    assert scan_delay(details, start - 5.02, 0, 5)[0] == pytest.approx(.02, abs=1e-6)
    assert scan_delay(details, start - 5, 0, 5) == (.10, True)
    assert scan_delay(details, start, 0.4, 5) == (0, True)
    assert scan_delay(details, start + 8.98, 0, 5)[0] == pytest.approx(.02, abs=1e-6)
    assert scan_delay(details, start + 9, 0, 5) == (1.5, True)
    assert scan_delay(details, start + 100, .4, 5) == (1.1, True)
    assert scan_delay(details, start + 299.8, 0, 5)[0] == pytest.approx(.2, abs=1e-6)
    assert scan_delay(details, start + 300, 0, 5) == (6, False)
    assert scan_delay([], start, 0, 5) == (6, False)
    known = [(start, start + 600)]
    assert scan_delay([], start - 30, 0, 5, known) == (1.5, True)
    assert scan_delay([], start + 300, 0, 5, known) == (6, False)


def test_random_schedule_uses_both_ends_of_each_range_and_prioritizes_overlaps(monkeypatch):
    import seu_reserve.app as app_module
    windows = [(1000, 1300), (1080, 1380)]
    draws = []
    def low(lower, upper):
        draws.append((lower, upper))
        return lower
    monkeypatch.setattr(app_module.random, 'uniform', low)
    assert scan_delay([], 1000, 0, 5, windows) == (.05, True)
    assert scan_delay([], 1040, 0, 5, windows) == (.5, True)
    assert scan_delay([], 1075, 0, 5, windows) == (.05, True)
    assert scan_delay([], 1380, 100, 5, windows) == (4, False)
    assert draws == [(.05, .1), (.5, 1.5), (.05, .1), (4, 6)]
    # An early reservation deadline ends polling even before the 30 s burst ends.
    assert scan_delay([], 1020, 0, 5, [(1000, 1020)]) == (4, False)


def test_opening_targets_retire_only_on_explicit_evidence_and_keep_missing_siblings():
    from seu_reserve.app import merge_openings, opening_windows
    start = moment('2030-01-01 10:00:00').timestamp()
    assert merge_openings({}, None, start) == {}
    base = dict(title='同名讲座', reason='candidate',
                reservation_start='2030-01-01T10:00:00+08:00',
                reservation_end='2030-01-01T12:00:00+08:00')
    known = merge_openings({}, [{**base, 'wid': 'a'}, {**base, 'wid': 'b'}], start)
    known = merge_openings(known, [{**base, 'wid': 'a', 'reason': 'already_reserved'}], start)
    assert opening_windows(list(known.values())) == [(start, start + 300)]
    known = merge_openings(known, [], start)
    assert 'b' in known
    known = merge_openings(known, [{**base, 'wid': 'b', 'reason': 'invalid_activity_time'}], start)
    assert known['b']['reason'] == 'candidate'
    known = merge_openings(known, [{**base, 'wid': 'b', 'reason': 'title_not_selected'}], start)
    assert not opening_windows(list(known.values()))
    assert not merge_openings(known, [], start + 302)


def test_connect_timeout_does_not_leave_the_task_pending(tmp_path):
    class ConnectTimeoutSchool(School):
        attempts = 0
        def request(self, method, url, **kwargs):
            if url.endswith('addReservation.do'):
                self.attempts += 1
                raise requests.exceptions.ConnectTimeout('SECRET connect timeout')
            return super().request(method, url, **kwargs)
    school = ConnectTimeoutSchool()
    school.activities = [lecture()]
    assert execute(school, tmp_path, sleep=interrupted) == 130
    assert school.attempts == 1
    assert school.submitted == []
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['status'] == 'scanning' and not saved['activity']


def test_connect_timeout_does_not_consume_a_retry_of_the_original_activity(tmp_path):
    class ConnectTimeoutSchool(School):
        def request(self, method, url, **kwargs):
            if url.endswith('addReservation.do'):
                raise requests.exceptions.ConnectTimeout('SECRET connect timeout')
            return super().request(method, url, **kwargs)
    seed = School()
    assert execute(seed, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    now = moment('2026-10-18 12:00:00').timestamp()
    saved['holds'] = {'one': dict(start=moment('2030-01-01 10:00:00').timestamp(),
                                  end=moment('2030-01-01 11:00:00').timestamp(),
                                  since=now, until=now + 600, attempts=1, confirmed=False)}
    path.write_text(json.dumps(saved))

    school = ConnectTimeoutSchool()
    school.activities = [lecture('one')]
    assert execute(school, tmp_path) == 130
    saved = json.loads(path.read_text())
    assert saved['health']['last_error'] == 'connect_timeout'
    # The request never left, so the retry budget of the original activity is unchanged.
    assert saved['holds']['one']['attempts'] == 1
    assert saved['status'] == 'scanning' and not saved['activity']


def test_withdrawn_history_row_does_not_confirm_a_pending_submission(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['retry_at'] = 0
    path.write_text(json.dumps(saved))

    school.submit_timeout = False
    school.activities = []
    # FBZT=-1 is excluded by progress(); it must not be success evidence for pending either.
    school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约', 'FBZT': '-1'}]
    assert execute(school, tmp_path) == 130

    saved = json.loads(path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved.get('pending_checks') == 1
    assert 'event=reservation_succeeded' not in (tmp_path / 'events.log').read_text()


def test_resolve_pending_ignores_a_withdrawn_history_row(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    school.submit_timeout = False
    school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约', 'FBZT': '-1'}]
    asked = []
    assert resolve_pending(Settings(state_dir=tmp_path), session=school,
                           environ={'SEU_USERNAME': 'test-user'},
                           prompt=lambda question: asked.append(question) or 'cancel') == 1
    assert len(asked) == 1  # it asked for manual confirmation instead of confirming by itself
    events = (tmp_path / 'events.log').read_text()
    assert 'event=pending_not_in_history' in events
    assert 'event=pending_confirmed' not in events
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending'


def test_resolve_pending_confirms_when_school_history_shows_it(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    school.submit_timeout = False
    school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约'}]
    assert resolve_pending(Settings(state_dir=tmp_path), session=school,
                           environ={'SEU_USERNAME': 'test-user'},
                           prompt=lambda _: 'confirm') == 0
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'scanning'
    assert school.submitted == ['one']
    assert 'event=pending_confirmed' in (tmp_path / 'events.log').read_text()


def test_resolve_pending_requires_explicit_confirmation_when_absent(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    school.submit_timeout = False
    school.mine = []
    settings = Settings(state_dir=tmp_path)
    environ = {'SEU_USERNAME': 'test-user'}
    assert resolve_pending(settings, session=school, environ=environ,
                           prompt=lambda _: 'wrong') == 1
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending'
    assert 'event=pending_resolution_cancelled' in (tmp_path / 'events.log').read_text()
    assert resolve_pending(settings, session=school, environ=environ,
                           prompt=lambda _: 'confirm') == 0
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'scanning'
    assert school.submitted == ['one']
    log = (tmp_path / 'events.log').read_text()
    assert 'event=pending_released_by_operator' in log


def test_pending_misses_auto_release_and_resume_scanning(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    assert school.submitted == ['one']
    school.submit_timeout = False
    waits, events = [], []
    later = moment('2026-10-18 12:05:00').timestamp()
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=wait, notify=events.append, clock=lambda: later) == 130
    assert waits[:2] == [2, 2] and 4 <= waits[2] <= 6 and len(waits) == 3
    assert school.submitted == ['one', 'one']
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['status'] == 'scanning' and not saved['activity']
    assert 'pending_checks' not in saved and 'pending_since' not in saved
    events_log = (tmp_path / 'events.log').read_text()
    assert 'event=reservation_unconfirmed' in events_log
    assert 'event=reservation_unconfirmed_released' in events_log
    assert 'event=reservation_succeeded' in events_log


def test_first_rate_limited_query_waits_without_a_probe(tmp_path):
    class MineLimited(School):
        mine_calls = 0
        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do'):
                self.mine_calls += 1
                if self.mine_calls == 1:
                    response = requests.Response()
                    response.url = url
                    response.status_code = 429
                    response.headers['Retry-After'] = '120'
                    response._content = b'{}'
                    self.requests.append((method, url, kwargs))
                    return response
            return super().request(method, url, **kwargs)
    school = MineLimited()
    waits = []
    def stop(seconds):
        waits.append(seconds)
        raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    assert waits == [120]
    assert school.mine_calls == 1


def test_sustained_captcha_failure_raises_a_step_alert(tmp_path):
    class CaptchaTimeout(School):
        def request(self, method, url, **kwargs):
            if url.endswith('vcode.do'):
                raise requests.Timeout('PRIVATE captcha timeout')
            return super().request(method, url, **kwargs)
    school = CaptchaTimeout()
    school.activities = [lecture()]
    events, waits = [], []
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds
        if events or len(waits) > 200:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=wait, clock=lambda: now[0],
                   notify=events.append) == 130
    assert events == ['reservation_step_failure']
    assert all(4 <= seconds <= 6 for seconds in waits[:4])
    assert max(waits) <= 6


def test_unconfirmed_hold_blocks_a_conflicting_activity(tmp_path):
    class LostResponse(School):
        hide_history = True
        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do') and self.hide_history:
                response = super().request(method, url, **kwargs)
                payload = json.loads(response._content)
                payload['datas']['wdhdList'] = []
                response._content = json.dumps(payload).encode()
                return response
            if url.endswith('addReservation.do') and kwargs['data']['wid'] == 'one':
                self.submitted.append('one')
                self.mine.append({**next(row for row in self.activities if row.get('WID') == 'one'),
                                  'YYZT': '已预约', 'HD_WID': 'one', 'SFDK': '0', 'SFWY': '0'})
                self.activities = [row for row in self.activities if row.get('WID') != 'one']
                raise requests.Timeout('response lost after the server applied it')
            return super().request(method, url, **kwargs)
    school = LostResponse()
    school.activities = [lecture('one'), lecture('two')]
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    state_path.write_text(json.dumps(saved))
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    saved = json.loads(state_path.read_text())
    assert school.submitted == ['one']
    assert saved['holds']['one']['start'] < saved['holds']['one']['end']
    assert 'event=reservation_unconfirmed_released' in (tmp_path / 'events.log').read_text()
    assert 'event=reservation_succeeded' not in (tmp_path / 'events.log').read_text()


def test_unconfirmed_hold_retries_the_original_activity(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    school.submit_timeout = False
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 5:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert school.submitted == ['one', 'one']
    assert saved['status'] == 'scanning'
    assert not saved.get('holds') or saved['holds'].get('one', {}).get('confirmed') is True
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


def test_unconfirmed_hold_clears_on_history_evidence(tmp_path):
    class LostResponse(School):
        hide_history = True
        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do') and self.hide_history:
                response = super().request(method, url, **kwargs)
                payload = json.loads(response._content)
                payload['datas']['wdhdList'] = []
                response._content = json.dumps(payload).encode()
                return response
            if url.endswith('addReservation.do') and kwargs['data']['wid'] == 'one':
                self.submitted.append('one')
                self.mine.append({**next(row for row in self.activities if row.get('WID') == 'one'),
                                  'YYZT': '已预约', 'HD_WID': 'one', 'SFDK': '0', 'SFWY': '0'})
                self.activities = [row for row in self.activities if row.get('WID') != 'one']
                raise requests.Timeout('response lost after the server applied it')
            return super().request(method, url, **kwargs)
    school = LostResponse()
    school.activities = [lecture('one'), lecture('two')]
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    state_path.write_text(json.dumps(saved))
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    assert 'holds' in json.loads(state_path.read_text())
    school.hide_history = False
    assert execute(school, tmp_path) == 130
    still = json.loads(state_path.read_text())
    assert still.get('status') == 'scanning'
    assert 'one' not in still.get('holds', {})
    assert school.submitted == ['one']
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


def test_already_reserved_reply_confirms_instead_of_pending(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_replies = [{'code': -1, 'msg': '您已预约该活动'}]
    assert execute(school, tmp_path) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['status'] == 'scanning' and not saved['activity']
    assert saved['holds']['one']['confirmed'] is True
    assert 'event=reservation_hold_confirmed' in (tmp_path / 'events.log').read_text()
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


def test_unconfirmed_hold_expires_and_allows_conflicting_activity(tmp_path):
    class LostResponse(School):
        hide_history = True
        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do') and self.hide_history:
                response = super().request(method, url, **kwargs)
                payload = json.loads(response._content)
                payload['datas']['wdhdList'] = []
                response._content = json.dumps(payload).encode()
                return response
            if url.endswith('addReservation.do') and kwargs['data']['wid'] == 'one':
                self.submitted.append('one')
                self.mine.append({**next(row for row in self.activities if row.get('WID') == 'one'),
                                  'YYZT': '已预约', 'HD_WID': 'one', 'SFDK': '0', 'SFWY': '0'})
                self.activities = [row for row in self.activities if row.get('WID') != 'one']
                raise requests.Timeout('response lost after the server applied it')
            return super().request(method, url, **kwargs)
    school = LostResponse()
    school.activities = [lecture('one'), lecture('two', HDLX_DISPLAY='人文与科学素养系列讲座_法律')]
    start = moment('2026-10-18 12:00:00').timestamp()
    assert execute(school, tmp_path, clock=lambda: start) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    state_path.write_text(json.dumps(saved))
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop, clock=lambda: start) == 130
    assert 'holds' in json.loads(state_path.read_text())
    later = start + 601
    assert execute(school, tmp_path, clock=lambda: later) == 130
    assert school.submitted == ['one', 'two']
    assert 'event=reservation_hold_expired' in (tmp_path / 'events.log').read_text()


def test_hold_for_a_survives_confirming_a_different_activity(tmp_path):
    class LostA(School):
        def request(self, method, url, **kwargs):
            if url.endswith('addReservation.do') and kwargs['data']['wid'] == 'one':
                self.submitted.append('one')
                self.activities = [row for row in self.activities if row.get('WID') != 'one']
                raise requests.Timeout('response lost after the server applied it')
            return super().request(method, url, **kwargs)
    school = LostA()
    school.activities = [lecture('one'), lecture('three'),
                         lecture('two', KSSJ='2030-01-01 14:00:00', JSSJ='2030-01-01 15:00:00')]
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    state_path.write_text(json.dumps(saved))
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    saved = json.loads(state_path.read_text())
    assert school.submitted == ['one', 'two']
    assert 'one' in saved.get('holds', {})
    assert 'three' not in school.submitted


def test_second_timeout_gets_its_own_hold(tmp_path):
    class LostA(School):
        def request(self, method, url, **kwargs):
            if url.endswith('addReservation.do') and kwargs['data']['wid'] == 'one':
                self.submitted.append('one')
                self.activities = [row for row in self.activities if row.get('WID') != 'one']
                raise requests.Timeout('response lost after the server applied it')
            return super().request(method, url, **kwargs)
    school = LostA()
    school.submit_timeout = True
    school.activities = [lecture('one'), lecture('two', KSSJ='2030-01-01 14:00:00',
                                                  JSSJ='2030-01-01 15:00:00')]
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    state_path.write_text(json.dumps(saved))
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 6:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    saved = json.loads(state_path.read_text())
    assert {'one', 'two'} <= set(saved.get('holds', {}))


def test_already_reserved_reply_does_not_keep_old_candidates(tmp_path):
    school = School()
    school.activities = [lecture('one'), lecture('three')]
    school.submit_replies = [{'code': -1, 'msg': '您已预约该活动'}]
    assert execute(school, tmp_path) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert school.submitted == ['one']
    assert saved.get('holds', {}).get('one', {}).get('confirmed') is True
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


def test_activity_list_confirmation_keeps_hold_until_history_syncs(tmp_path):
    class LostA(School):
        def request(self, method, url, **kwargs):
            if url.endswith('addReservation.do') and kwargs['data']['wid'] == 'one':
                self.submitted.append('one')
                self.activities = [row for row in self.activities if row.get('WID') != 'one']
                raise requests.Timeout('response lost after the server applied it')
            return super().request(method, url, **kwargs)
    school = LostA()
    first = lecture('one')
    school.activities = [first, lecture('three')]
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    state_path.write_text(json.dumps(saved))
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    assert 'holds' in json.loads(state_path.read_text())
    school.activities = [{**first, 'YYZT': '已预约'}, lecture('three')]
    assert execute(school, tmp_path, sleep=interrupted) == 130
    saved = json.loads(state_path.read_text())
    assert school.submitted == ['one']
    assert saved.get('holds', {}).get('one', {}).get('confirmed') is True
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


def test_pending_mine_rate_limit_does_not_query_the_activity_list(tmp_path):
    class HistoryLimited(School):
        limited = False
        list_calls = 0
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                self.list_calls += 1
            if self.limited and url.endswith('queryMyActivityList.do'):
                response = requests.Response()
                response.url = url
                response.status_code = 429
                response.headers['Retry-After'] = '600'
                response._content = b'{}'
                self.requests.append((method, url, kwargs))
                return response
            return super().request(method, url, **kwargs)
    school = HistoryLimited()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))
    school.limited = True
    school.list_calls = 0
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if sum(waits) >= 600:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    assert sum(waits) >= 600
    assert all(seconds >= 100 for seconds in waits)
    assert school.list_calls == 0


def test_pending_activity_list_rate_limit_does_not_count_a_check(tmp_path):
    class ActivityLimited(School):
        limited = False
        def request(self, method, url, **kwargs):
            if self.limited and url.endswith('queryActivityList.do'):
                response = requests.Response()
                response.url = url
                response.status_code = 429
                response.headers['Retry-After'] = '600'
                response._content = b'{}'
                self.requests.append((method, url, kwargs))
                return response
            return super().request(method, url, **kwargs)
    school = ActivityLimited()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))
    school.limited = True
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if sum(waits) >= 600:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=stop) == 130
    saved = json.loads(state_path.read_text())
    assert sum(waits) >= 600
    assert all(seconds >= 100 for seconds in waits)
    assert saved.get('pending_checks', 0) == 0


def test_failed_verification_source_does_not_advance_auto_release(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved.update(retry_at=0)
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))

    clock = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        clock[0] += seconds
        if len(waits) >= 6:
            raise KeyboardInterrupt

    broken = School()
    broken.mine = []
    broken.activities = []
    broken.query_statuses = [503] * 50
    assert execute(broken, tmp_path, sleep=stop, clock=lambda: clock[0]) == 130

    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved.get('pending_checks', 0) == 0
    logs = (tmp_path / 'diagnostics.jsonl').read_text()
    assert 'reservation_checked' not in logs
    assert 'reservation_unconfirmed_released' not in logs
    assert any(url.endswith('queryActivityList.do') for _, url, _ in broken.requests)


def test_failed_check_does_not_count_but_later_valid_checks_still_release(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved.update(retry_at=0)
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))

    class Tracked(School):
        list_calls = 0

        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                self.list_calls += 1
            return super().request(method, url, **kwargs)

    clock = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        clock[0] += seconds
        if 'event=reservation_unconfirmed_released' in (tmp_path / 'events.log').read_text():
            raise KeyboardInterrupt
        if len(waits) >= 20:
            raise KeyboardInterrupt

    later = Tracked()
    later.mine = []
    later.activities = [lecture('two')]
    later.query_statuses = [503, 200, 200]   # one failed check, then two valid ones
    assert execute(later, tmp_path, sleep=stop, clock=lambda: clock[0]) == 130

    saved = json.loads(state_path.read_text())
    logs = (tmp_path / 'diagnostics.jsonl').read_text()
    assert saved['status'] == 'scanning' and not saved['activity']
    # The failed check no longer counts: three valid checks were needed, so the release only
    # happened on the fourth list query of the pending period.
    assert later.list_calls == 5
    assert logs.count('"event": "reservation_checked"') == 3
    assert 'reservation_unconfirmed_released' in logs


def test_midway_failed_check_restarts_the_consecutive_count(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved.update(retry_at=0)
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))

    clock = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        clock[0] += seconds
        if 'event=reservation_unconfirmed_released' in (tmp_path / 'events.log').read_text():
            raise KeyboardInterrupt
        if len(waits) >= 20:
            raise KeyboardInterrupt

    later = School()
    later.mine = []
    later.activities = [lecture('two')]
    # valid, valid, failed, valid, valid, valid
    later.query_statuses = [200, 200, 503, 200, 200, 200]
    assert execute(later, tmp_path, sleep=stop, clock=lambda: clock[0]) == 130

    events = [json.loads(line) for line in (tmp_path / 'diagnostics.jsonl').read_text().splitlines()]
    checks = [event['checks'] for event in events
              if event.get('event') == 'reservation_checked']
    assert checks == [1, 2, 1, 2, 3]     # the failed check restarted the count
    assert any(event.get('event') == 'reservation_unconfirmed_released' for event in events)


def test_midway_429_restarts_the_consecutive_count(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved.update(retry_at=0)
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))

    class MineLimited(School):
        calls = 0

        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do'):
                self.calls += 1
                if self.calls == 3:
                    response = requests.Response()
                    response.url = url
                    response.status_code = 429
                    response.headers['Retry-After'] = '600'
                    response._content = b'{}'
                    self.requests.append((method, url, kwargs))
                    return response
            return super().request(method, url, **kwargs)

    clock = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        clock[0] += seconds
        if 'event=reservation_unconfirmed_released' in (tmp_path / 'events.log').read_text():
            raise KeyboardInterrupt
        if len(waits) >= 20:
            raise KeyboardInterrupt

    later = MineLimited()
    later.mine = []
    later.activities = [lecture('two')]
    # valid, valid, 429 wait, valid, valid, valid
    assert execute(later, tmp_path, sleep=stop, clock=lambda: clock[0]) == 130

    events = [json.loads(line) for line in (tmp_path / 'diagnostics.jsonl').read_text().splitlines()]
    checks = [event['checks'] for event in events
              if event.get('event') == 'reservation_checked']
    assert checks == [1, 2, 1, 2, 3]     # the server-directed wait also restarted the count
    assert any(event.get('event') == 'reservation_unconfirmed_released' for event in events)


def test_midway_unreadable_history_restarts_the_consecutive_count(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved.update(retry_at=0)
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))

    class HistoryUnreadable(School):
        calls = 0

        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do'):
                self.calls += 1
                self.mine = ([{'YYZT': 'unknown-status'}]   # reserved() raises ValueError
                             if self.calls == 3 else [])
            return super().request(method, url, **kwargs)

    clock = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        clock[0] += seconds
        if 'event=reservation_unconfirmed_released' in (tmp_path / 'events.log').read_text():
            raise KeyboardInterrupt
        if len(waits) >= 20:
            raise KeyboardInterrupt

    later = HistoryUnreadable()
    later.mine = []
    later.activities = [lecture('two')]
    # valid, valid, unreadable history row, valid, valid, valid
    assert execute(later, tmp_path, sleep=stop, clock=lambda: clock[0]) == 130

    events = [json.loads(line) for line in (tmp_path / 'diagnostics.jsonl').read_text().splitlines()]
    checks = [event['checks'] for event in events
              if event.get('event') == 'reservation_checked']
    assert checks == [1, 2, 1, 2, 3]     # the unreadable check also restarted the count
    assert any(event.get('event') == 'reservation_unconfirmed_released' for event in events)


def test_success_reply_saves_pending_clear_and_confirmed_hold_together(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    snapshots = []
    original_save = app_module.Task.save
    def save(self):
        original_save(self)
        snapshots.append(json.loads(self.path.read_text()))
    monkeypatch.setattr(app_module.Task, 'save', save)
    school = School()
    school.activities = [lecture()]
    school.submit_replies = [{'code': -1, 'msg': '您已预约该活动'}]
    assert execute(school, tmp_path) == 130
    seen_pending = False
    dangerous = []
    for snapshot in snapshots:
        if snapshot.get('status') == 'pending':
            seen_pending = True
        if (seen_pending and snapshot.get('status') == 'scanning'
                and not snapshot.get('activity')
                and snapshot.get('holds', {}).get('one', {}).get('confirmed') is not True):
            dangerous.append(snapshot)
    assert dangerous == []


def test_confirmed_hold_counts_toward_reference_progress_and_does_not_stop_booking(tmp_path):
    now = moment('2026-10-18 12:00:00').timestamp()
    categories = ['人文与科学素养系列讲座_心理健康', '人文与科学素养系列讲座_法律',
                  '人文与科学素养系列讲座-艺术类', '人文与科学素养系列讲座_其他']
    history = [lecture(f'p{index}', HDLX_DISPLAY=categories[index % 4],
                       KSSJ=f'2030-01-0{index + 1} 10:00:00',
                       JSSJ=f'2030-01-0{index + 1} 11:00:00', YYZT='已预约')
               for index in range(7)]
    holds = {'held': {'start': moment('2030-02-01 10:00:00').timestamp(),
                      'end': moment('2030-02-01 11:00:00').timestamp(),
                      'since': now, 'until': now + 600, 'attempts': 0,
                      'confirmed': True, 'category': '人文与科学素养系列讲座_其他'}}
    current = progress(progress_rows(history, holds, now), now)
    assert current.total == 8 and current.complete and not current.missing
    school = School()
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['holds'] = holds
    state_path.write_text(json.dumps(saved))
    school.mine = history
    school.activities = [lecture('other', KSSJ='2030-03-01 10:00:00',
                                 JSSJ='2030-03-01 11:00:00')]
    assert execute(school, tmp_path, sleep=interrupted) == 130
    # The reference target is informational only: reaching it must not stop new reservations.
    assert school.submitted == ['other']
    log = (tmp_path / 'events.log').read_text()
    assert 'event=goal_completed' in log
    assert 'event=category_not_needed' not in log
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'scanning'
    assert 'reserved' in saved['health']['progress'] and 'planned' not in saved['health']['progress']


def test_pending_confirms_from_activity_list_when_history_fails(tmp_path):
    class HistoryDown(School):
        down = False
        mine_calls = 0
        def request(self, method, url, **kwargs):
            if self.down and url.endswith('queryMyActivityList.do'):
                self.mine_calls += 1
                raise requests.Timeout('history down')
            return super().request(method, url, **kwargs)
    school = HistoryDown()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    state_path.write_text(json.dumps(saved))
    school.submit_timeout = False
    school.down = True
    school.activities[0]['YYZT'] = '已预约'
    assert execute(school, tmp_path, sleep=interrupted) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert school.mine_calls >= 1
    assert saved['status'] == 'scanning' and not saved['activity']
    assert any(url.endswith('queryActivityList.do') for _, url, _ in school.requests)
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


@pytest.mark.parametrize('source', ['submit_reply', 'pending_release'])
def test_interrupt_before_hold_creation_keeps_pending_and_blocks_conflicts(tmp_path, source):
    school = School()
    first = lecture('one')
    school.activities = [first, lecture('two')]
    if source == 'submit_reply':
        school.submit_replies = [{'code': -1, 'msg': '您已预约该活动'}]
    else:
        school.submit_timeout = True
        assert execute(school, tmp_path) == 130
        path = tmp_path / 'task.json'
        saved = json.loads(path.read_text())
        saved['retry_at'] = 0
        path.write_text(json.dumps(saved))
        school.activities = [lecture('two')]

    interrupted_hold = False
    def interrupt(frame, event, arg):
        nonlocal interrupted_hold
        if (event == 'call' and frame.f_code.co_filename == run.__code__.co_filename
                and frame.f_code.co_name == 'ensure_hold' and not interrupted_hold):
            interrupted_hold = True
            raise KeyboardInterrupt
        return interrupt
    previous_trace = sys.gettrace()
    try:
        sys.settrace(interrupt)
        assert execute(school, tmp_path, sleep=lambda _: None) == 130
    finally:
        sys.settrace(previous_trace)
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert interrupted_hold
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved['pending_start'] < saved['pending_end']

    school.submit_timeout = False
    school.activities = [{**first, 'YYZT': '已预约'}, lecture('two')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']


@pytest.mark.parametrize('missing', ['pending_start', 'pending_end'])
def test_pending_without_a_complete_interval_does_not_resume_submissions(tmp_path, missing):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved.update(retry_at=0, pending_checks=2)
    saved.pop(missing)
    path.write_text(json.dumps(saved))
    school.activities = [lecture('two')]
    assert execute(school, tmp_path) == 130
    saved = json.loads(path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert school.submitted == ['one']


def test_null_optional_holds_still_preserve_success_evidence(tmp_path):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['holds'] = None
    path.write_text(json.dumps(saved))
    school.activities = [lecture('one'), lecture('two')]
    school.submit_replies = [{'code': -1, 'msg': '您已预约该活动'}]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']
    assert json.loads(path.read_text())['holds']['one']['confirmed'] is True


@pytest.mark.parametrize('found', [False, True])
@pytest.mark.parametrize('existing_holds', [False, True])
def test_resolve_pending_preserves_another_activitys_legacy_hold(tmp_path, found, existing_holds):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    now = moment('2026-10-18 12:00:00').timestamp()
    hold = dict(activity='one', start=moment('2030-01-01 10:00:00').timestamp(),
                end=moment('2030-01-01 11:00:00').timestamp(),
                since=now, until=now + 600, attempts=1)
    saved.update(status='pending', activity='two', hold=hold,
                 pending_start=moment('2030-01-01 14:00:00').timestamp(),
                 pending_end=moment('2030-01-01 15:00:00').timestamp())
    if existing_holds:
        saved['holds'] = {'three': dict(start=hold['start'] + 86400, end=hold['end'] + 86400,
                                       since=now, until=now + 600, attempts=0, confirmed=False)}
    path.write_text(json.dumps(saved))
    if found:
        school.mine = [lecture('two', YYZT='已预约', KSSJ='2030-01-01 14:00:00',
                               JSSJ='2030-01-01 15:00:00')]
    assert resolve_pending(Settings(state_dir=tmp_path), session=school,
                           environ={'SEU_USERNAME': 'test-user'},
                           prompt=lambda _: 'confirm') == 0
    assert json.loads(path.read_text())['hold'] == hold
    school.activities = [lecture('conflict')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == []
    migrated = json.loads(path.read_text())['holds']
    assert 'one' in migrated
    if existing_holds:
        assert 'three' in migrated


def test_restart_discards_legacy_generic_wait(tmp_path):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['retry_at'] = moment('2026-10-18 12:00:00').timestamp() + 100
    saved.pop('retry_server', None)
    path.write_text(json.dumps(saved))
    waits = []

    def stop(seconds):
        waits.append(seconds)
        raise KeyboardInterrupt

    assert execute(school, tmp_path, sleep=stop) == 130
    assert len(waits) == 1 and 4 <= waits[0] <= 6
    assert not json.loads((tmp_path / 'task.json').read_text()).get('retry_at')


def test_ocr_model_is_warmed_once_before_the_first_submission(tmp_path, monkeypatch):
    created = []

    class Engine:
        def __init__(self, show_ad=False):
            assert show_ad is False
            created.append('engine')

        def classification(self, image):
            return '1234'

    fake = types.ModuleType('ddddocr')
    fake.DdddOcr = Engine
    monkeypatch.setitem(sys.modules, 'ddddocr', fake)
    school = School()
    school.activities = [lecture()]
    assert execute(school, tmp_path, recognize=None) == 130
    assert created == ['engine']
    assert school.submitted == ['one']
    lines = [json.loads(line) for line in
             (tmp_path / 'diagnostics.jsonl').read_text().splitlines()]
    warm = [line for line in lines if line['event'] == 'ocr_ready']
    assert len(warm) == 1 and warm[0]['duration_seconds'] >= 0
    started = next(line for line in lines if line['event'] == 'submission_started')
    assert warm[0]['at'] <= started['at']
    assert started['captcha_seconds'] >= 0
    assert started['ocr_seconds'] >= 0


class FakePoller:
    """Deterministic ActivityPoller replacement for opening-window integration tests."""

    instances = []
    next_rows = []
    rows_override = None
    next_wake_once = False
    at_offset = 0.0
    on_timeout = None
    error_retry_after = 0.0
    error_server_directed = False
    next_error = None

    def __init__(self, query, *, interval, clock, **_kwargs):
        from seu_reserve.poller import Snapshot
        self._snapshot_cls = Snapshot
        self.query = query
        self.interval = interval
        self.clock = clock
        self.rows = list(FakePoller.next_rows)
        self.error = FakePoller.next_error
        self.polls = 0
        self.running = True
        self.waits = []
        self.versioned_reads = 0
        self.wake_once = FakePoller.next_wake_once
        FakePoller.instances.append(self)

    def snapshot(self):
        rows = (FakePoller.rows_override if FakePoller.rows_override is not None
                else self.rows)
        error = FakePoller.next_error if FakePoller.next_error is not None else self.error
        now = self.clock() + FakePoller.at_offset
        return self._snapshot_cls(rows=list(rows), at=now,
                                  error=error,
                                  retry_after=(FakePoller.error_retry_after
                                               if error is not None else 0.0),
                                  failed_at=now if error is not None else 0.0,
                                  server_directed=(FakePoller.error_server_directed
                                                   if error is not None else False))

    def snapshot_with_version(self):
        self.versioned_reads += 1
        return self.snapshot(), self.polls

    def start(self):
        self.running = True

    def stop(self, *, join: bool = True):
        self.running = False

    def request_refresh(self):
        self.polls += 1
        return self.polls

    def wait_update(self, timeout, since=None):
        self.waits.append(timeout)
        if self.wake_once:
            self.wake_once = False
            return True
        if FakePoller.on_timeout is not None:
            FakePoller.on_timeout(timeout)
        return False


def diagnostics_rows(directory):
    return [json.loads(line) for line in
            (directory / 'diagnostics.jsonl').read_text().splitlines()]


def native_activity(wid='one', *, opens='2026-10-18 12:00:00', closes='2026-10-18 13:00:00'):
    from test_activities import activity
    return activity(wid, YYKSSJ=opens, YYJSSJ=closes)


def test_pending_confirmation_uses_poller_snapshot_before_history(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    from test_activities import activity
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    school = School()
    school.activities = [native_activity()]
    school.submit_replies = [{'code': 500}]
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.next_rows = school.activities
    FakePoller.next_wake_once = False
    clock = lambda: moment('2026-10-18 12:00:00').timestamp()
    assert execute(school, tmp_path, clock=clock, background_poller=True) == 130
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending'

    FakePoller.rows_override = [activity('one', YY_WID='one',
                                         YYKSSJ='2026-10-18 12:00:00',
                                         YYJSSJ='2026-10-18 13:00:00')]
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def run_clock():
        return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if now[0] > moment('2026-10-18 12:00:10').timestamp():
            raise KeyboardInterrupt
    FakePoller.on_timeout = wait
    try:
        assert execute(school, tmp_path, clock=run_clock, sleep=wait,
                       background_poller=True) == 130
    finally:
        FakePoller.on_timeout = None
    lines = diagnostics_rows(tmp_path)
    assert any(line['event'] == 'pending_snapshot_confirmed' for line in lines)
    confirmed = [line for line in lines if line['event'] == 'reservation_confirmed']
    assert confirmed and confirmed[-1]['source'] == 'pending_snapshot'
    assert school.mine == []
    assert school.submitted == ['one']


def test_pending_confirmation_rejects_a_snapshot_older_than_the_submission(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    from test_activities import activity
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    school = School()
    school.activities = [native_activity()]
    school.submit_replies = [{'code': 500}]
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_rows = school.activities
    FakePoller.next_wake_once = False
    clock = lambda: moment('2026-10-18 12:00:00').timestamp()
    assert execute(school, tmp_path, clock=clock, background_poller=True) == 130
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending'

    FakePoller.rows_override = [activity('one', YY_WID='one',
                                         YYKSSJ='2026-10-18 12:00:00',
                                         YYJSSJ='2026-10-18 13:00:00')]
    FakePoller.at_offset = -30.0
    now = [moment('2026-10-18 12:00:00').timestamp()]
    stop_at = moment('2026-10-18 12:00:08').timestamp()
    def run_clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if now[0] >= stop_at:
            raise KeyboardInterrupt
    FakePoller.on_timeout = wait
    try:
        assert execute(school, tmp_path, clock=run_clock, sleep=wait,
                       background_poller=True) == 130
    finally:
        FakePoller.on_timeout = None
    lines = diagnostics_rows(tmp_path)
    assert not any(line['event'] == 'pending_snapshot_confirmed' for line in lines)
    confirmed = [line for line in lines if line['event'] == 'reservation_confirmed']
    assert all(line['source'] != 'pending_snapshot' for line in confirmed)


@pytest.mark.parametrize(('save_advance', 'snapshot_offset', 'confirmed'), [
    (100.0, -30.0, False), (0.5, 0.1, True)])
def test_pending_positive_evidence_requires_a_post_checkpoint_snapshot(
        tmp_path, monkeypatch, save_advance, snapshot_offset, confirmed):
    """A snapshot from the save window is not evidence; one after the save is."""
    import seu_reserve.app as app_module
    from seu_reserve.state import Task
    from test_activities import activity
    switched = [False]
    candidate = native_activity()
    already = activity('one', YY_WID='one', YYKSSJ='2026-10-18 12:00:00',
                       YYJSSJ='2026-10-18 13:00:00')
    class SwitchPoller(FakePoller):
        def snapshot(self):
            if switched[0]:
                FakePoller.rows_override = [already]
            return super().snapshot()
    monkeypatch.setattr(app_module, 'ActivityPoller', SwitchPoller)
    monkeypatch.setattr(FakePoller, 'rows_override', None)
    monkeypatch.setattr(FakePoller, 'at_offset', 0.0)
    FakePoller.instances.clear()
    FakePoller.next_rows = [candidate]
    FakePoller.next_error = None
    school = School()
    school.activities = [candidate]
    school.submit_replies = [{'code': 500}]
    now = [moment('2026-10-18 12:00:00').timestamp()]
    original_save = Task.save
    def save_then_advance(task):
        result = original_save(task)
        if task.data.get('status') == 'pending' and not switched[0]:
            switched[0] = True
            now[0] += save_advance
            FakePoller.at_offset = snapshot_offset
        return result
    monkeypatch.setattr(Task, 'save', save_then_advance)
    waits = []
    def wait(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if len(waits) >= 4:
            raise KeyboardInterrupt
    FakePoller.on_timeout = wait
    try:
        assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait,
                       background_poller=True) == 130
    finally:
        FakePoller.on_timeout = None
    events = [line['event'] for line in diagnostics_rows(tmp_path)]
    assert ('pending_snapshot_confirmed' in events) is confirmed
    assert (json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending') is (not confirmed)


def test_rejected_prefetched_captcha_falls_back_to_a_fresh_fetch(tmp_path):
    school = School()
    school.activities = [native_activity()]
    school.submit_replies = [{'code': 1, 'msg': '验证码错误'}]
    now = [moment('2026-10-18 11:59:57').timestamp()]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if school.submitted or len(school.requests) > 500:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait) == 130
    lines = diagnostics_rows(tmp_path)
    assert any(line['event'] == 'captcha_prefetch_rejected' for line in lines)
    sources = [line['captcha_source'] for line in lines
               if line['event'] == 'submission_started']
    assert sources[:2] == ['prefetch', 'fresh']
    assert school.submitted == ['one', 'one']
    assert len([1 for _, url, _ in school.requests if url.endswith('vcode.do')]) == 2


def test_preparation_targets_the_next_not_yet_open_lecture(tmp_path):
    from test_activities import activity
    school = School()
    # The first window is already open and full but still running: it must not hide the second
    # lecture's preparation target.
    school.activities = [
        activity('full', HDZRS=100, YYKSSJ='2026-10-18 12:00:00',
                 YYJSSJ='2026-10-18 12:10:00'),
        activity('open', YYKSSJ='2026-10-18 12:00:05', YYJSSJ='2026-10-18 13:00:00'),
    ]
    school.hdrs = [{'HD_WID': 'full', 'YYRS': 100}]
    full_start = moment('2026-10-18 12:00:00').timestamp()
    now = [moment('2026-10-18 11:59:57').timestamp()]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if school.submitted:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait) == 130
    lines = diagnostics_rows(tmp_path)
    assert school.submitted == ['open']
    assert not any(line['event'] == 'prewarm_history_requested' and line['at'] >= full_start
               for line in lines)
    assert any(line['event'] == 'captcha_prefetched' and line['at'] >= full_start
               for line in lines)
    started = [line for line in lines
               if line['event'] == 'submission_started' and line['wid'] == 'open']
    assert started and started[0]['captcha_source'] == 'prefetch'


class FakeHistoryPoller:
    """Deterministic background 'my activities' refresher for offline tests."""

    rows = []
    error = None
    reason = None
    seconds = 0.4
    retry_after = 0.0
    server_directed = False
    events = []
    starts = 0

    def __init__(self, query, *, interval, clock, **_kwargs):
        self.query = query
        self.clock = clock
        self.running = True
        self.polls = 0

    def snapshot_with_version(self):
        return self.snapshot(), self.polls

    def request_refresh(self):
        self.polls += 1
        return self.polls

    def start(self):
        self.running = True
        FakeHistoryPoller.starts += 1

    def stop(self, *, join: bool = True):
        self.running = False
        FakeHistoryPoller.events.append('history_stop')

    def snapshot(self):
        from seu_reserve.poller import Snapshot
        now = self.clock()
        has_error = FakeHistoryPoller.error is not None
        return Snapshot(rows=list(FakeHistoryPoller.rows), at=now,
                        error=FakeHistoryPoller.error,
                        reason=FakeHistoryPoller.reason,
                        retry_after=FakeHistoryPoller.retry_after,
                        failed_at=now if has_error else 0.0,
                        server_directed=FakeHistoryPoller.server_directed,
                        seconds=FakeHistoryPoller.seconds)


class RecordingHistory(FakeHistoryPoller):
    """History owner whose snapshot only changes on an explicit refresh request."""

    refreshes = []

    def __init__(self, query, **kwargs):
        super().__init__(query, **kwargs)
        self.published = None

    def start(self):
        self.running = True
        RecordingHistory.starts += 1
        self.request_refresh()

    def request_refresh(self):
        from seu_reserve.poller import Snapshot
        RecordingHistory.refreshes.append(self.clock())
        self.polls += 1
        self.published = Snapshot(rows=list(FakeHistoryPoller.rows), at=self.clock(),
                                  started=self.clock(), seconds=FakeHistoryPoller.seconds)
        return self.polls

    def snapshot(self):
        return self.published

    def snapshot_with_version(self):
        return self.published, self.polls


def seed_opening_selection(directory, *, start='2026-10-18T12:00:10+08:00'):
    from seu_reserve.state import Task
    task = Task(directory, 'test-user')
    task.data['selection'] = dict(updated_at=0, activities=[dict(
        title='测试讲座', reason='reservation_not_started',
        reservation_start=start, reservation_end='2026-10-18T13:00:00+08:00')])
    task.save()
    task.close()


def test_failed_history_owner_keeps_the_event_retry_without_a_sync_read(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path)
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    school = School()
    school.activities = [native_activity(opens='2026-10-18 12:00:10')]
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_rows = school.activities
    FakePoller.next_wake_once = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = 'RemoteError'
    FakeHistoryPoller.reason = 'network_timeout'
    now = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if len(waits) >= 4:
            raise KeyboardInterrupt
    FakePoller.on_timeout = stop
    try:
        assert execute(school, tmp_path, clock=lambda: now[0], sleep=stop,
                       background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
        FakeHistoryPoller.error = None
        FakeHistoryPoller.reason = None
    # The failing refresh stays with its owner, without scan-level backoff or a synchronous read.
    assert all(seconds > 0 for seconds in waits)
    assert not json.loads((tmp_path / 'task.json').read_text()).get('retry_at')
    events = (tmp_path / 'events.log').read_text().splitlines()
    assert any(line.endswith('event=network_timeout') for line in events)
    hist_queries = [url for _, url, _ in school.requests if url.endswith('queryMyActivityList.do')]
    assert hist_queries == []


def test_prefetch_and_submission_use_host_clock_despite_school_current_time(tmp_path):
    start = moment('2026-10-18 12:00:03').timestamp()
    now = [start - 4.1]
    school = School()
    school.server_now = '2026-10-18 11:59:50'
    school.activities = [native_activity(opens='2026-10-18 12:00:03')]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if school.submitted or now[0] > start + 10:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait) == 130
    lines = diagnostics_rows(tmp_path)
    prefetched = [line for line in lines if line['event'] == 'captcha_prefetched']
    assert prefetched and prefetched[0]['at'] - start == pytest.approx(-2.7, abs=.01)
    started = [line for line in lines if line['event'] == 'submission_started']
    assert started and started[0]['captcha_source'] == 'prefetch'
    assert started[0]['at'] - start == pytest.approx(0, abs=.1)


def test_history_rate_limit_during_captcha_blocks_the_submission(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:00+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_rows = [native_activity(opens='2026-10-18 12:00:00')]
    FakePoller.next_wake_once = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    class RateLimitedHistory(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if urlsplit(url).path.endswith('vcode.do'):
                FakeHistoryPoller.error = 'RemoteError'
                FakeHistoryPoller.retry_after = 120.0
                FakeHistoryPoller.server_directed = True
            return response
    school = RateLimitedHistory()
    school.activities = list(FakePoller.next_rows)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait,
                   background_poller=True,
                   settings=Settings(state_dir=tmp_path, prewarm=False,
                                     captcha_prefetch=False)) == 130
    assert school.submitted == []
    lines = diagnostics_rows(tmp_path)
    assert not any(line['event'] == 'submission_started' for line in lines)
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved.get('retry_server') is True
    assert saved['retry_at'] > moment('2026-10-18 12:00:00').timestamp()


def test_opening_pending_check_does_not_fabricate_a_network_failure(
        tmp_path, monkeypatch):
    """The fast path's 'none' source is an unavailable source, not a failed request."""
    import seu_reserve.app as app_module
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    saved['retry_at'] = 0
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    saved.pop('pending_checks', None)
    state_path.write_text(json.dumps(saved))

    seed_opening_selection(tmp_path, start='2026-10-18T12:00:04+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.next_rows = []
    FakePoller.next_error = None
    FakePoller.at_offset = -5.0
    FakePoller.next_wake_once = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    school = School()
    school.activities = []
    school.mine = []
    now = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []

    def clock():
        return now[0]

    def wait(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if len(waits) >= 2:
            raise KeyboardInterrupt

    try:
        assert execute(school, tmp_path, clock=clock, sleep=wait,
                       background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.at_offset = 0.0
    events = (tmp_path / 'events.log').read_text()
    assert 'event=network_timeout' not in events
    assert 'event=query_failure' not in events
    health = json.loads(state_path.read_text())['health']
    assert health['last_error'] == 'none' and health['last_error_active'] is False
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved.get('pending_checks', 0) == 0
    # Missing snapshots wait for publication or a deadline instead of fast scan polling.
    assert waits == [5.0, 5.0]
    assert any(line['event'] == 'reader_snapshot_missing'
               for line in diagnostics_rows(tmp_path))


def test_opening_pending_miss_does_not_query_history_every_round(tmp_path, monkeypatch):
    """A missing list snapshot must not turn the pending check into ~10 history queries/s."""
    import seu_reserve.app as app_module
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    saved['retry_at'] = 0
    for field in ('step_failure_since', 'query_failure_since', 'pending_checks', 'pending_since'):
        saved.pop(field, None)
    state_path.write_text(json.dumps(saved))
    before = (tmp_path / 'events.log').read_text()

    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.next_rows = []
    FakePoller.next_error = None
    FakePoller.at_offset = -5.0
    FakePoller.next_wake_once = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    school = School()
    school.activities = []
    school.mine = []
    now = [moment('2026-10-18 12:00:00').timestamp()]
    stop_at = now[0] + 4.2

    def clock():
        return now[0]

    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if now[0] >= stop_at:
            raise KeyboardInterrupt

    FakePoller.on_timeout = wait
    try:
        assert execute(school, tmp_path, clock=clock, sleep=wait,
                       background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    history_requests = [url for _, url, _ in school.requests
                        if url.endswith('queryMyActivityList.do')]
    # Two completed checks over ~4.2 s; the old fast path issued one history request for every
    # 0.1 s round where the reader had no snapshot (roughly 8 here).
    assert len(history_requests) <= 3
    appended = (tmp_path / 'events.log').read_text()[len(before):]
    # The miss resets pending_checks, so a completed check can reach count 1 again; the event must
    # still be written once per pending episode, not once per completed check.
    assert appended.count('event=reservation_unconfirmed') == 0
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved.get('pending_checks', 0) in (0, 1, 2)
    # The per-episode marker is persisted so a restart cannot announce the same episode again.
    assert saved.get('pending_unconfirmed_notified') is not True


def test_opening_poller_server_wait_is_honored_without_a_sync_fallback(
        tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.next_rows = [native_activity(opens='2026-10-18 12:00:10')]
    FakePoller.next_wake_once = False
    FakePoller.at_offset = 0.0
    FakePoller.next_error = 'RemoteError'
    FakePoller.error_retry_after = 120.0
    FakePoller.error_server_directed = True
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    FakeHistoryPoller.starts = 0
    school = School()
    school.activities = list(FakePoller.next_rows)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def clock(): return now[0]
    def wait(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if seconds >= 100 or len(waits) >= 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait,
                   background_poller=True,
                   settings=Settings(state_dir=tmp_path, prewarm=False,
                                     captcha_prefetch=False)) == 130
    # The fast path must not issue additional synchronous list requests inside a server-directed
    # wait; the first scan may still bootstrap the poller before the opening flag is set.
    list_queries = [url for _, url, _ in school.requests if url.endswith('queryActivityList.do')]
    assert len(list_queries) <= 1
    assert any(seconds >= 100 for seconds in waits)
    # The history reader must not restart inside the list reader's server-directed wait.
    assert FakeHistoryPoller.starts == 0
    FakePoller.next_error = None
    FakePoller.error_retry_after = 0.0
    FakePoller.error_server_directed = False


def test_activity_list_without_an_id_does_not_advance_pending_checks(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    saved.update(retry_at=0)
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))
    clock = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        clock[0] += max(seconds, 0.0)
        if len(waits) >= 6:
            raise KeyboardInterrupt
    later = School()
    later.mine = []
    # The second row is unreadable (no activity ID): the list cannot rule out that the pending
    # activity is present, so this check must not count as valid absence evidence.
    later.activities = [lecture('two'), {'HDMC': '缺少活动 ID 的记录'}]
    assert execute(later, tmp_path, sleep=stop, clock=lambda: clock[0]) == 130
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved.get('pending_checks', 0) == 0
    assert 'reservation_checked' not in (tmp_path / 'diagnostics.jsonl').read_text()
    events = (tmp_path / 'events.log').read_text()
    assert 'event=invalid_activity_data' in events
    assert 'event=reservation_unconfirmed_released' not in events


def test_invalid_background_history_is_rejected_and_never_read_synchronously(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    activities = [native_activity(opens='2026-10-18 12:00:10')]
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_error = None
    FakePoller.next_wake_once = False
    FakeHistoryPoller.rows = [{'HD_WID': 'hist1', 'YYZT': '已预约',
                               'JZXL_DISPLAY': '人文与科学素养系列讲座_心理健康',
                               'KSSJ': 'not-a-time', 'JSSJ': 'also-bad',
                               'SFDK': '0', 'SFWY': '0'}]
    FakeHistoryPoller.error = None
    FakeHistoryPoller.server_directed = False
    FakeHistoryPoller.retry_after = 0.0
    school = School()
    school.activities = list(activities)
    FakePoller.next_rows = list(activities)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock(): return now[0]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if len(waits) >= 4:
            raise KeyboardInterrupt
    FakePoller.on_timeout = stop
    try:
        assert execute(school, tmp_path, clock=clock, sleep=stop, background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    lines = diagnostics_rows(tmp_path)
    invalid = [line for line in lines if line['event'] == 'history_invalid']
    assert invalid and invalid[0].get('reason') == 'time_invalid'
    assert not any(line['event'] == 'history_refreshed' for line in lines)
    history_queries = [url for _, url, _ in school.requests
                       if url.endswith('queryMyActivityList.do')]
    assert history_queries == []
    assert 'event=invalid_activity_data' not in (tmp_path / 'events.log').read_text()
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['health'].get('failure_count', 0) == 0


def test_conflicting_history_does_not_confirm_a_pending_submission(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    saved['retry_at'] = 0
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))

    # The school returned the same activity ID twice with contradictory reservation states:
    # one row reserves it and one row cancels it. Neither success nor absence can be derived.
    school.submit_timeout = False
    school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约'},
                   {**lecture(), 'HD_WID': 'one', 'YYZT': '已取消'}]
    school.activities = [lecture('two')]
    assert execute(school, tmp_path) == 130

    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved.get('pending_checks', 0) == 0
    assert 'event=reservation_succeeded' not in (tmp_path / 'events.log').read_text()
    lines = diagnostics_rows(tmp_path)
    assert not any(line['event'] == 'reservation_confirmed' for line in lines)


def test_conflicting_activity_list_does_not_confirm_a_pending_submission(tmp_path):
    from test_activities import activity
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    saved['retry_at'] = 0
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))

    # The same activity appears twice in the list, once showing a reservation and once not.
    school.submit_timeout = False
    school.mine = []
    window = dict(YYKSSJ='2026-10-18 12:00:00', YYJSSJ='2026-10-18 13:00:00')
    school.activities = [activity('one', YY_WID='reservation-id', **window),
                         activity('one', **window)]
    assert execute(school, tmp_path) == 130

    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved.get('pending_checks', 0) == 0
    assert 'event=reservation_succeeded' not in (tmp_path / 'events.log').read_text()


def test_scan_level_server_wait_restarts_the_saved_pending_check_count(tmp_path, monkeypatch):
    import seu_reserve.app as app_module

    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert seed_pending(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    saved['retry_at'] = 0
    saved.pop('step_failure_since', None)
    saved.pop('query_failure_since', None)
    state_path.write_text(json.dumps(saved))

    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_error = None
    FakePoller.next_wake_once = False
    FakePoller.error_retry_after = 0.0
    FakePoller.error_server_directed = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    activities = [native_activity('two', opens='2026-10-18 12:00:10')]
    school = School()
    school.activities = list(activities)
    school.mine = []
    FakePoller.next_rows = list(activities)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock():
        return now[0]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if len(waits) == 2:
            # Two valid checks are persisted; while the scan pauses the background list
            # reader learns a server-directed wait.
            FakePoller.instances[-1].error = 'RemoteError'
            FakePoller.error_retry_after = 120.0
            FakePoller.error_server_directed = True
            return
        if len(waits) >= 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=stop, background_poller=True,
                   settings=Settings(state_dir=tmp_path, prewarm=False,
                                     captcha_prefetch=False)) == 130

    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved.get('pending_checks', 0) == 0
    assert 'event=reservation_unconfirmed_released' not in (tmp_path / 'events.log').read_text()


def test_resolve_pending_refuses_conflicting_history(tmp_path):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    now = moment('2026-10-18 12:00:00').timestamp()
    hold = dict(start=now + 3600, end=now + 7200, since=now, until=now + 600,
                attempts=0, confirmed=False)
    saved.update(status='pending', activity='one', holds={'one': hold},
                 pending_start=now + 3600, pending_end=now + 7200,
                 pending_since=now, pending_checks=2)
    path.write_text(json.dumps(saved))

    school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约'},
                   {**lecture(), 'HD_WID': 'one', 'YYZT': '已取消'}]
    asked = []
    def prompt(question):
        asked.append(question)
        return 'confirm'
    assert resolve_pending(Settings(state_dir=tmp_path), session=school,
                           environ={'SEU_USERNAME': 'test-user'}, prompt=prompt) == 1

    saved = json.loads(path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    assert saved['holds']['one'] == hold
    assert asked == []
    events = (tmp_path / 'events.log').read_text()
    assert 'event=pending_confirmed' not in events
    assert 'event=pending_check_failed' in events


def test_conflicting_history_does_not_confirm_an_unconfirmed_hold(tmp_path):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    now = moment('2026-10-18 12:00:00').timestamp()
    hold = dict(start=now + 3600, end=now + 7200, since=now, until=now + 600,
                attempts=1, confirmed=False)
    saved['holds'] = {'one': hold}
    path.write_text(json.dumps(saved))

    school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约'},
                   {**lecture(), 'HD_WID': 'one', 'YYZT': '已取消'}]
    assert execute(school, tmp_path) == 130

    saved = json.loads(path.read_text())
    assert saved['holds']['one'] == hold
    assert 'event=reservation_succeeded' not in (tmp_path / 'events.log').read_text()


def test_conflicting_activity_list_does_not_confirm_an_unconfirmed_hold(tmp_path):
    from test_activities import activity
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    now = moment('2026-10-18 12:00:00').timestamp()
    hold = dict(start=now + 3600, end=now + 7200, since=now, until=now + 600,
                attempts=1, confirmed=False)
    saved['holds'] = {'one': hold}
    path.write_text(json.dumps(saved))

    # The list shows the held activity as both reserved and open: neither row is evidence.
    school.mine = []
    window = dict(YYKSSJ='2026-10-18 12:00:00', YYJSSJ='2026-10-18 13:00:00')
    school.activities = [activity('one', YY_WID='reservation-id', **window),
                         activity('one', **window)]
    assert execute(school, tmp_path) == 130

    saved = json.loads(path.read_text())
    assert saved['holds']['one'] == hold
    events = (tmp_path / 'events.log').read_text()
    assert 'event=reservation_succeeded' not in events
    assert 'event=reservation_hold_confirmed' not in events


def cas_login_payloads(school):
    return [kwargs['json'] for method, url, kwargs in school.requests
            if method == 'POST' and url.endswith('casLogin')]


class TrustedCampusSchool(School):
    """Campus-direct CAS SPA: persisted TGT enables the explicit verifyTgt silent login."""

    service = 'http://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do'
    entry = 'https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do'

    def __init__(self):
        super().__init__()
        self.cookies = requests.cookies.RequestsCookieJar()
        self.verify_tgt_calls = 0

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        tail = path.rsplit('/', 1)[-1]
        has_tgt = any(cookie.name == 'TGT' for cookie in self.cookies)
        if tail == 'index.do' and method == 'GET':
            self.requests.append((method, url, kwargs))
            response = requests.Response()
            response.url = url
            if self.authenticated:
                response.status_code = 200
                response._content = b'<script src="indexjsp.js"></script>'
            else:
                # The entry GET alone never completes a trusted login (the SPA route is served).
                response.status_code = 302
                response.headers['Location'] = ('https://auth.seu.edu.cn/dist/#/login?'
                                                + urlencode({'service': self.service}))
            return response
        if path.startswith('/dist/'):
            self.requests.append((method, url, kwargs))
            response = requests.Response()
            response.url = url
            response.status_code = 200
            response._content = b'<html>CAS</html>'
            return response
        if tail == 'verifyTgt' and method == 'POST':
            self.verify_tgt_calls += 1
            self.requests.append((method, url, kwargs))
            payload = ({'success': 1, 'code': 201,
                        'redirectUrl': quote(self.service, safe='')}
                       if has_tgt else {'success': 0, 'code': 500})
            response = requests.Response()
            response.url = url
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response
        if tail == 'loginRedirect':
            self.requests.append((method, url, kwargs))
            self.authenticated = True
            response = requests.Response()
            response.url = url
            response.status_code = 302
            response.headers['Location'] = self.entry + '?ticket=SYNTHETIC'
            return response
        if tail == 'casLogin' and method == 'POST':
            self.requests.append((method, url, kwargs))
            self.cookies.set('TGT', 'tgt-value', domain='auth.seu.edu.cn', path='/', secure=True)
            self.cookies.set('JSESSIONID', 'jsession-value', domain='ehall.seu.edu.cn', path='/',
                             expires=moment('2030-01-01 10:00:00').timestamp())
            response = requests.Response()
            response.url = url
            response.status_code = 200
            response._content = json.dumps(
                {'code': 200, 'redirectUrl': quote(self.service, safe='')}).encode()
            return response
        return super().request(method, url, **kwargs)


def test_check_reuses_persisted_cas_cookies_without_a_second_sms(tmp_path):
    from seu_reserve.state import TRUST_COOKIES_FILE, load_trust_cookies
    settings = Settings(state_dir=tmp_path, trust_device=True)
    first = TrustedCampusSchool()
    assert execute(first, tmp_path, settings=settings, check_only=True) == 0
    assert first.verify_tgt_calls == 0          # no cookie was persisted yet
    assert cas_login_payloads(first)
    stored = load_trust_cookies(tmp_path, account='test-user')
    assert [item['name'] for item in stored] == ['TGT', 'JSESSIONID']
    assert stored[0]['domain'] == 'auth.seu.edu.cn' and stored[0]['secure'] is True
    assert stored[1]['domain'] == 'ehall.seu.edu.cn'
    assert (tmp_path / TRUST_COOKIES_FILE).exists()

    second = TrustedCampusSchool()
    assert execute(second, tmp_path, settings=settings, check_only=True) == 0
    assert second.verify_tgt_calls == 1
    assert cas_login_payloads(second) == []     # no password/SMS second factor on the second run
    verify = [kwargs['json'] for method, url, kwargs in second.requests
              if method == 'POST' and url.endswith('verifyTgt')]
    assert verify == [{'service': TrustedCampusSchool.service, 'loginType': 'account'}]
    assert 'agentId' not in verify[0]

    # Opt-in only: with the flag off a fresh run ignores the persisted cookies and logs in again.
    third = TrustedCampusSchool()
    assert execute(third, tmp_path,
                   settings=Settings(state_dir=tmp_path, trust_device=False),
                   check_only=True) == 0
    assert third.verify_tgt_calls == 0
    assert cas_login_payloads(third)


def test_withdrawn_record_does_not_block_a_conflicting_candidate(tmp_path):
    school = School()
    school.activities = [lecture('new')]
    # Same time slot, but the school marks the old record as withdrawn/unpublished. Progress,
    # display and confirmation already ignore it; the candidate conflict check must too.
    school.mine = [{**lecture('old'), 'HD_WID': 'old', 'YYZT': '已预约', 'FBZT': '-1'}]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['new']


def test_dead_history_reader_never_falls_back_to_a_main_thread_query(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')

    class DeadHistory(FakeHistoryPoller):
        """Claims to start, but the worker is already gone, like a recycled reader."""

        def start(self):
            self.running = False

        def snapshot(self):
            from seu_reserve.poller import Snapshot
            now = self.clock()
            return Snapshot(rows=[], at=now - 60.0, error='RemoteError',
                            reason='network_timeout', failed_at=now)

    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', DeadHistory)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_error = None
    FakePoller.next_wake_once = False
    FakePoller.next_rows = []
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    school = School()
    school.activities = []
    now = [moment('2026-10-18 12:00:00').timestamp()]
    rounds = []

    def clock():
        return now[0]

    def wait(seconds):
        rounds.append(seconds)
        now[0] += max(seconds, 0.0)
        if len(rounds) == 1:
            # Let the history cache age out so the next scans need the background reader.
            now[0] += 10.1
        if len(rounds) >= 10:
            raise KeyboardInterrupt

    FakePoller.on_timeout = wait
    try:
        assert execute(school, tmp_path, clock=clock, sleep=wait,
                       background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    lines = diagnostics_rows(tmp_path) if (tmp_path / 'diagnostics.jsonl').exists() else []
    # A history reader that disappeared after having started is diagnosed as stopped per reader;
    # the shutdown no longer also reports it as a missing periodic reader.
    stopped = [line for line in lines if line['event'] == 'background_reader_stopped']
    assert stopped and all(line['reader'] == 'history' for line in stopped)
    assert not any(line['event'] == 'reader_snapshot_missing' for line in lines)
    # The unified architecture never pays for a synchronous main-thread history query.
    queries = [url for _, url, _ in school.requests if url.endswith('queryMyActivityList.do')]
    assert queries == []


def run_constructed_opening(school, directory, monkeypatch, *, opening='2026-10-18 12:00:10',
                            clock_start=None, stop_after=None):
    """Run one deterministic opening scan with the constructed pollers installed."""
    import seu_reserve.app as app_module
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    seed_opening_selection(directory, start=moment(opening).isoformat(timespec='seconds'))
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_error = None
    FakePoller.next_wake_once = False
    FakePoller.error_retry_after = 0.0
    FakePoller.error_server_directed = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    rows = [native_activity(opens=opening, closes='2030-01-01 09:00:00')]
    FakePoller.next_rows = list(rows)
    school.activities = list(rows)
    now = [clock_start if clock_start is not None else moment(opening).timestamp() - 10]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if stop_after is not None and len(waits) >= stop_after:
            raise KeyboardInterrupt
        if school.submitted:
            raise KeyboardInterrupt
    FakePoller.on_timeout = wait
    try:
        result = execute(school, directory, clock=lambda: now[0], sleep=wait,
                         background_poller=True, settings=Settings(state_dir=directory))
    finally:
        FakePoller.on_timeout = None
        FakePoller.next_rows = []
        FakePoller.rows_override = None
    return result, waits


def test_captcha_prefetch_uses_the_fixed_lead_and_its_own_budget(tmp_path, monkeypatch):
    timeouts = []
    class TimedSchool(School):
        def request(self, method, url, **kwargs):
            if url.endswith('vcode.do'):
                timeouts.append(kwargs.get('timeout'))
            return super().request(method, url, **kwargs)
    school = TimedSchool()
    assert run_constructed_opening(school, tmp_path, monkeypatch)[0] == 130
    lines = diagnostics_rows(tmp_path)
    started = [line for line in lines if line['event'] == 'captcha_prefetch_started']
    assert len(started) == 1 and started[0]['attempt'] == 1
    assert started[0]['lead_seconds'] == pytest.approx(2.7, abs=0.05)
    assert timeouts and timeouts[0] == pytest.approx(8.0)
    prefetched = [line for line in lines if line['event'] == 'captcha_prefetched']
    assert prefetched and prefetched[0]['lead_seconds'] == pytest.approx(2.7, abs=0.05)
    submitted = [line for line in lines if line['event'] == 'submission_started']
    assert submitted and submitted[0]['captcha_source'] == 'prefetch'
    assert school.submitted == ['one']


def test_fast_opening_warms_the_submission_session_before_the_prefetch(tmp_path, monkeypatch):
    school = School()
    assert run_constructed_opening(school, tmp_path, monkeypatch)[0] == 130
    start = moment('2026-10-18 12:00:10').timestamp()
    lines = diagnostics_rows(tmp_path)
    warmed = [line for line in lines if line['event'] == 'main_session_warmed']
    assert len(warmed) == 1
    assert warmed[0]['at'] - start == pytest.approx(-5.0, abs=.1)
    prefetch = next(line for line in lines if line['event'] == 'captcha_prefetch_started')
    assert prefetch['at'] - warmed[0]['at'] == pytest.approx(2.3, abs=.1)
    # The constructed readers never send school requests, so the only list read is the warm-up.
    list_calls = [url for _, url, _ in school.requests if url.endswith('queryActivityList.do')]
    assert len(list_calls) == 1
    submitted = next(line for line in lines if line['event'] == 'submission_started')
    assert submitted['captcha_source'] == 'prefetch'


@pytest.mark.parametrize('reason', ['network_timeout', 'service_unavailable'])
def test_prefetch_uses_known_opening_while_list_reader_fails(tmp_path, monkeypatch, reason):
    from dataclasses import replace

    opening = '2026-10-18 12:00:10'
    start = moment(opening).timestamp()
    snapshot = FakePoller.snapshot

    def timing_out(poller):
        result = snapshot(poller)
        if start - 3 <= poller.clock() < start + 0.1:
            return replace(result, error='RemoteError', reason=reason)
        return result

    monkeypatch.setattr(FakePoller, 'snapshot', timing_out)
    school = School()
    assert run_constructed_opening(school, tmp_path, monkeypatch, opening=opening)[0] == 130
    events = diagnostics_rows(tmp_path)
    started = next(row for row in events if row['event'] == 'captcha_prefetch_started')
    assert started['at'] - start == pytest.approx(-2.7, abs=.05)
    submitted = next(row for row in events if row['event'] == 'submission_started')
    assert submitted['at'] >= start + 0.1 and submitted['captcha_source'] == 'prefetch'


@pytest.mark.parametrize('failure', ['timeout', 'http_503'])
def test_failed_captcha_prefetch_is_retried_once_before_the_opening(tmp_path, monkeypatch, failure):
    attempts = {'vcode': 0}
    class FlakyCaptcha(School):
        def request(self, method, url, **kwargs):
            if url.endswith('vcode.do'):
                attempts['vcode'] += 1
                if attempts['vcode'] == 1:
                    if failure == 'timeout':
                        raise requests.Timeout('constructed prefetch failure')
                    response = super().request(method, url, **kwargs)
                    response.status_code = 503
                    return response
            return super().request(method, url, **kwargs)
    school = FlakyCaptcha()
    assert run_constructed_opening(school, tmp_path, monkeypatch)[0] == 130
    lines = diagnostics_rows(tmp_path)
    started = [line['attempt'] for line in lines if line['event'] == 'captcha_prefetch_started']
    failed = [line['attempt'] for line in lines if line['event'] == 'captcha_prefetch_failed']
    assert started == [1, 2]
    assert failed == [1]
    assert any(line['event'] == 'captcha_prefetched' for line in lines)
    submitted = [line for line in lines if line['event'] == 'submission_started']
    assert submitted and submitted[0]['captcha_source'] == 'prefetch'
    assert school.submitted == ['one']


def test_captcha_prefetch_is_limited_to_two_attempts_per_opening(tmp_path, monkeypatch):
    attempts = {'vcode': 0}
    class BrokenCaptcha(School):
        def request(self, method, url, **kwargs):
            if url.endswith('vcode.do'):
                attempts['vcode'] += 1
                if attempts['vcode'] <= 2:
                    raise requests.Timeout('constructed prefetch failure')
            return super().request(method, url, **kwargs)
    school = BrokenCaptcha()
    assert run_constructed_opening(school, tmp_path, monkeypatch)[0] == 130
    lines = diagnostics_rows(tmp_path)
    started = [line['attempt'] for line in lines if line['event'] == 'captcha_prefetch_started']
    failed = [line['attempt'] for line in lines if line['event'] == 'captcha_prefetch_failed']
    assert started == [1, 2] and failed == [1, 2]
    assert not any(line['event'] == 'captcha_prefetched' for line in lines)
    # After the two failed attempts the submit path falls back to a fresh fetch.
    submitted = [line for line in lines if line['event'] == 'submission_started']
    assert submitted and submitted[0]['captcha_source'] == 'fresh'
    assert attempts['vcode'] == 3 and school.submitted == ['one']


def test_captcha_prefetch_is_skipped_when_the_opening_is_too_close(tmp_path, monkeypatch):
    opening = '2026-10-18 12:00:05'
    school = School()
    assert run_constructed_opening(school, tmp_path, monkeypatch, opening=opening,
                                   clock_start=moment(opening).timestamp() - 0.3)[0] == 130
    lines = diagnostics_rows(tmp_path)
    skipped = [line for line in lines if line['event'] == 'captcha_prefetch_skipped']
    assert skipped and skipped[0]['reason'] == 'insufficient_remaining'
    assert not any(line['event'] == 'captcha_prefetch_started' for line in lines)
    submitted = [line for line in lines if line['event'] == 'submission_started']
    assert submitted and submitted[0]['captcha_source'] == 'fresh'
    assert school.submitted == ['one']


def test_history_error_during_a_fresh_captcha_wait_keeps_submitting(tmp_path, monkeypatch):
    """A failed history refresh must not turn into a submission backoff: the old rows stay."""
    import seu_reserve.app as app_module
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_error = None
    FakePoller.next_wake_once = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    school = School()
    rows = [native_activity()]
    school.activities = list(rows)
    FakePoller.next_rows = list(rows)
    original_request = School.request
    def request_and_break_history(self, method, url, **kwargs):
        if url.endswith('vcode.do'):
            FakeHistoryPoller.error = 'RemoteError'
            FakeHistoryPoller.reason = 'network_timeout'
        return original_request(self, method, url, **kwargs)
    school.request = request_and_break_history.__get__(school, School)
    try:
        assert execute(school, tmp_path, background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False),
                       sleep=interrupted) == 130
    finally:
        FakeHistoryPoller.error = None
        FakeHistoryPoller.reason = None
    lines = diagnostics_rows(tmp_path)
    assert not any(line['event'] == 'captcha_error' for line in lines)
    submitted = [line for line in lines if line['event'] == 'submission_started']
    assert submitted and submitted[0]['captcha_source'] == 'fresh'
    assert school.submitted == ['one']


@pytest.mark.parametrize('starts_in, hold_for', [(-5, 0), (56, 56), (900, 600)])
def test_hold_deadline_uses_host_clock(starts_in, hold_for):
    now = moment('2026-10-18 12:02:00').timestamp()
    assert pending_hold_until(now + starts_in, now) == now + hold_for


@pytest.mark.parametrize('old_duration', [30, 600])
def test_legacy_hold_deadline_is_recomputed_from_host_time(old_duration):
    now = moment('2026-10-18 12:02:00').timestamp()
    start = now + 60
    entry = dict(start=start, end=start + 3600, since=now,
                 until=now + old_duration, attempts=0, confirmed=False)
    holds = {'one': entry}
    assert normalize_hold_deadlines(holds) is True
    assert entry['until'] == start
    assert normalize_hold_deadlines(holds) is False


def test_phase_change_does_not_add_a_history_query(tmp_path, monkeypatch):
    """Entering the opening phase updates budgets but is not a history refresh event."""
    import seu_reserve.app as app_module
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', RecordingHistory)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.next_error = None
    FakePoller.at_offset = 0.0
    FakePoller.next_wake_once = False
    FakeHistoryPoller.rows = []
    RecordingHistory.refreshes = []
    RecordingHistory.starts = 0
    school = School()
    start = moment('2026-10-18 12:00:00').timestamp()
    # The opening phase begins 30 s before the reservation start, i.e. at 12:00:05.
    school.activities = [native_activity(opens='2026-10-18 12:00:35')]
    FakePoller.next_rows = school.activities
    now, waits = [start], []
    def stop(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if len(waits) >= 6:
            raise KeyboardInterrupt
    FakePoller.on_timeout = stop
    try:
        assert execute(school, tmp_path, clock=lambda: now[0], sleep=stop,
                       background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    assert now[0] > start + 5
    # Authentication sent the only baseline refresh; crossing into the opening phase added none.
    assert len(RecordingHistory.refreshes) == 1
    lines = diagnostics_rows(tmp_path)
    assert sum(line['event'] == 'history_refreshed' for line in lines) == 1


def test_pending_check_recovers_after_a_transient_history_outage(tmp_path, monkeypatch):
    """A completed failed history poll must be acknowledged so the next check queries again.

    Without that acknowledgement the pending path keeps re-reading the same failed snapshot while
    the bounded-retry reader waits for a refresh event that never arrives; confirmations stop
    advancing even after the school endpoint recovers.
    """
    import time
    import seu_reserve.app as app_module
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'
    saved['retry_at'] = 0
    for field in ('step_failure_since', 'query_failure_since'):
        saved.pop(field, None)
    state_path.write_text(json.dumps(saved))
    # Keep the constructed run short: normal scans and retries are exercised elsewhere.
    monkeypatch.setattr(app_module, 'SCAN_INTERVAL', .5)
    monkeypatch.setattr(app_module, 'PENDING_MISS_PAUSE', .2)
    monkeypatch.setattr(app_module, 'PENDING_AUTO_RELEASE_CHECKS', 1)
    monkeypatch.setattr(app_module, 'short_backoff', lambda failures: .05)

    flaky = School()
    flaky.activities = []
    flaky.mine = []
    history_calls = []
    original = flaky.request
    def request(method, url, **kwargs):
        if urlsplit(url).path.endswith('queryMyActivityList.do'):
            history_calls.append(time.monotonic())
            # The authentication baseline succeeds; the next pending refresh fails three times,
            # then the endpoint recovers.
            if len(history_calls) in (2, 3, 4):
                raise requests.Timeout('transient history outage')
        return original(method, url, **kwargs)
    flaky.request = request
    # Both reader threads use clones of the connected Client, so route their sessions through the
    # same constructed school; otherwise they would open real external connections.
    from seu_reserve.client import Client
    class ReaderSession:
        session = None
        def request(self, method, url, **kwargs):
            return flaky.request(method, url, **kwargs)
        def close(self):
            pass
    def clone(client):
        return Client(ReaderSession(), client.environ, client.prompt, client.notify,
                      diagnostic=client.diagnostic)
    monkeypatch.setattr(Client, 'clone_readonly', clone)
    started = time.monotonic()
    result = run(Settings(state_dir=tmp_path, prewarm=False, captcha_prefetch=False),
                 session=flaky,
                 environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
                 recognize=lambda _: '1234',
                 stop=lambda: time.monotonic() - started > 4.0)
    assert result == 130
    assert len(history_calls) >= 5
    assert any(line['event'] == 'reservation_checked' for line in diagnostics_rows(tmp_path))
