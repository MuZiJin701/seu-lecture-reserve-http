import base64
import json
import socket
import sys
import threading
import types
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from seu_reserve.app import (Settings, earliest_opening_local, progress_rows,
                             resolve_pending, run, scan_delay, school_clock_offset,
                             school_clock_uncertainty)
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
    result = run(Settings(state_dir=tmp_path), session=school,
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
    options = dict(session=school, environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password',
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
    settings = options.pop('settings', Settings(state_dir=directory))
    return run(settings, session=school,
               environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
               **options)


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


def test_windows_state_and_event_log_without_unix_file_apis(tmp_path, monkeypatch):
    import os
    import sys
    from types import SimpleNamespace
    from seu_reserve import state

    locks = []
    def locking(fd, mode, size):
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        locks.append((mode, size))
    monkeypatch.setattr(state, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=2, locking=locking))
    monkeypatch.delattr(os, 'O_NOFOLLOW', raising=False)
    monkeypatch.delattr(os, 'O_DIRECTORY', raising=False)
    school = School()
    school.activities = [lecture()]
    assert execute(school, tmp_path) == 130
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']
    assert locks == [(2, 1), (2, 1)]
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'scanning'
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


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
    school.query_statuses = [500] * 400
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


def test_four_explicit_captcha_rejections_cool_down_first_candidate_and_try_next(tmp_path):
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
    assert run(Settings(state_dir=tmp_path), session=school, environ=env) == 1
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
    assert run(Settings(state_dir=tmp_path), session=school, environ=env,
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
    assert run(Settings(state_dir=tmp_path), session=school, environ=env,
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
        assert run(Settings(state_dir=directory), session=session, environ={}, check_only=True) == 1
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
    assert events == []
    assert 'event=authentication_rejected' in (tmp_path / 'events.log').read_text()
    assert len([url for method, url, _ in school.requests if method == 'POST' and url.endswith('casLogin')]) == 1


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


def test_large_recoverable_state_does_not_block_restart(tmp_path):
    school = School()
    assert execute(school, tmp_path, sleep=interrupted) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['cooldowns'] = {str(n).zfill(32): 1 for n in range(2000)}
    path.write_text(json.dumps(saved))
    school.activities = [lecture()]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']


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
                response.status_code = 500
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


def test_scan_delay_uses_quarter_second_window_and_next_opening_boundary():
    start = moment('2030-01-01 10:00:00').timestamp()
    details = [dict(title='讲座', reason='reservation_not_started',
                    reservation_start='2030-01-01T10:00:00+08:00',
                    reservation_end='2030-01-01T12:00:00+08:00')]
    assert scan_delay(details, start - 100, 0, 10) == (10, False)
    assert scan_delay(details, start - 31, 0.2, 10) == (1, False)
    assert scan_delay(details, start - 30, 0, 10) == (0.25, True)
    assert scan_delay(details, start + 10, 0.4, 10) == (0, True)
    assert scan_delay(details, start + 599, 2, 10) == (0, True)
    assert scan_delay(details, start + 601, 0, 10) == (10, False)
    assert scan_delay([], start, 0, 10) == (10, False)
    known = [(start, start + 600)]
    assert scan_delay([], start - 30, 0, 10, known) == (0.25, True)
    assert scan_delay([], start + 601, 0, 10, known) == (10, False)


def test_school_clock_offset_calibrates_opening_window():
    now = moment('2026-10-18 12:00:00').timestamp()
    assert school_clock_offset([], now) == 0
    assert school_clock_offset([{'_SERVER_NOW': '2026-10-18 12:00:05'}], now) == 5
    # Day-scale divergence is a fixture or a broken clock, never useful scheduling input.
    assert school_clock_offset([{'_SERVER_NOW': '2026-10-19 12:00:00'}], now) == 0
    assert school_clock_offset([{'_SERVER_NOW': 'invalid'}], now) == 0


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


def test_resolve_pending_rejects_a_non_pending_task(tmp_path):
    school = School()
    assert resolve_pending(Settings(state_dir=tmp_path), session=school,
                           environ={'SEU_USERNAME': 'test-user'},
                           prompt=lambda _: 'confirm') == 1
    assert 'event=pending_not_found' in (tmp_path / 'events.log').read_text()


def test_pending_misses_auto_release_and_resume_scanning(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']
    school.submit_timeout = False
    waits, events = [], []
    later = moment('2026-10-18 12:05:00').timestamp()
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=wait, notify=events.append, clock=lambda: later) == 130
    assert waits == [2, 2, 10]
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
    assert waits[:4] == [1, 2, 4, 5]
    assert max(waits) <= 5


def test_step_alert_survives_restart(tmp_path):
    class CaptchaTimeout(School):
        def request(self, method, url, **kwargs):
            if url.endswith('vcode.do'):
                raise requests.Timeout('PRIVATE captcha timeout')
            return super().request(method, url, **kwargs)
    school = CaptchaTimeout()
    school.activities = [lecture()]
    events = []
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def wait(seconds):
        now[0] += seconds
        if events:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=wait, clock=lambda: now[0],
                   notify=events.append) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['health']['last_mail_event'] == 'reservation_step_failure'
    assert execute(school, tmp_path, sleep=interrupted, clock=lambda: now[0]) == 130


def test_every_major_mail_event_has_a_message():
    from seu_reserve.app import MAJOR_MAIL_EVENTS
    from seu_reserve.mail import MESSAGES
    assert MAJOR_MAIL_EVENTS <= set(MESSAGES)


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
    assert execute(school, tmp_path) == 130
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
    assert execute(school, tmp_path) == 130
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
    assert execute(school, tmp_path) == 130
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
    assert execute(school, tmp_path) == 130
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
    assert execute(school, tmp_path) == 130
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


def test_pending_confirmation_logs_one_diagnostic(tmp_path):
    school = School()
    school.activities = [lecture(), lecture('two')]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    state_path.write_text(json.dumps(saved))

    school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约'}]
    assert execute(school, tmp_path) == 130

    diagnostics = [json.loads(line) for line in
                   (tmp_path / 'diagnostics.jsonl').read_text().splitlines()]
    confirmed = [row for row in diagnostics
                 if row.get('event') == 'reservation_confirmed' and row.get('wid') == 'one']
    assert len(confirmed) == 1


def test_health_write_failures_are_reported_once_per_minute(tmp_path, capsys, monkeypatch):
    import inspect
    import seu_reserve.app as app_module

    original_save = app_module.Task.save

    def fail_only_from_health(self):
        caller = inspect.currentframe()
        caller = caller.f_back if caller is not None else None
        if caller is not None and caller.f_code.co_name in ('health', 'write_health'):
            raise OSError('state file temporarily held by a reader')
        return original_save(self)

    monkeypatch.setattr(app_module.Task, 'save', fail_only_from_health)
    school = School()
    school.activities = [lecture()]
    now = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds
        if len(waits) >= 3:
            raise KeyboardInterrupt

    assert execute(school, tmp_path, sleep=wait, clock=lambda: now[0]) == 130
    output = capsys.readouterr().out
    assert output.count('event=health_write_failed') == 1


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


def test_confirmed_hold_counts_toward_progress_and_suppresses_candidates(tmp_path):
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
    planned = progress(progress_rows(history, holds, now), now)
    assert sum(planned.planned.values()) == 8
    assert not planned.missing
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
    assert school.submitted == []


def test_resolve_pending_clears_legacy_hold(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    state_path = tmp_path / 'task.json'
    saved = json.loads(state_path.read_text())
    saved['retry_at'] = 0
    saved['hold'] = {'activity': 'one', 'start': 1893463200.0, 'end': 1893466800.0,
                     'since': 1792296000.0, 'until': 1792296600.0, 'attempts': 0}
    saved.pop('holds', None)
    state_path.write_text(json.dumps(saved))
    school.submit_timeout = False
    school.mine = []
    assert resolve_pending(Settings(state_dir=tmp_path), session=school,
                           environ={'SEU_USERNAME': 'test-user'},
                           prompt=lambda _: 'confirm') == 0
    assert 'hold' not in json.loads(state_path.read_text())
    assert 'holds' not in json.loads(state_path.read_text())


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


@pytest.mark.parametrize('listed', [False, True])
def test_legacy_confirmed_hold_missing_category_cannot_allow_extra_booking(tmp_path, listed):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    now = moment('2026-10-18 12:00:00').timestamp()
    held = lecture('held', KSSJ='2030-02-01 10:00:00', JSSJ='2030-02-01 11:00:00')
    saved['holds'] = {'held': dict(start=moment(held['KSSJ']).timestamp(),
                                  end=moment(held['JSSJ']).timestamp(), since=now,
                                  until=now + 600, attempts=0, confirmed=True)}
    path.write_text(json.dumps(saved))
    categories = ['人文与科学素养系列讲座_心理健康', '人文与科学素养系列讲座_法律',
                  '人文与科学素养系列讲座-艺术类']
    school.mine = [lecture(f'old{i}', HDLX_DISPLAY=categories[i % 3], YYZT='已预约',
                           KSSJ=f'2030-01-{i + 1:02d} 10:00:00',
                           JSSJ=f'2030-01-{i + 1:02d} 11:00:00') for i in range(7)]
    school.activities = [lecture('extra', KSSJ='2030-03-01 10:00:00',
                                 JSSJ='2030-03-01 11:00:00')]
    if listed:
        school.activities.append(held)
    school.requests.clear()
    assert execute(school, tmp_path) == 130
    assert school.submitted == []
    current = json.loads(path.read_text())
    assert current['holds']['held']['confirmed'] is True
    if listed:
        assert current['holds']['held']['category'] == held['HDLX_DISPLAY']
        assert sum(url.endswith('queryActivityList.do') for _, url, _ in school.requests) == 1
        assert sum(url.endswith('queryMyActivityList.do') for _, url, _ in school.requests) == 1
    else:
        assert 'event=attendance_category_unknown' in (tmp_path / 'events.log').read_text()

def test_transient_failures_use_bounded_short_backoff_not_generic_twenty(tmp_path):
    class Flaky(School):
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                raise requests.ConnectionError('connection reset')
            return super().request(method, url, **kwargs)

    waits = []

    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 4:
            raise KeyboardInterrupt

    assert execute(Flaky(), tmp_path, sleep=stop) == 130
    assert waits == [1, 2, 4, 5]


def test_429_without_retry_after_uses_short_backoff_not_twenty_seconds(tmp_path):
    school = School()
    school.query_statuses = [429] * 4
    waits = []

    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt

    assert execute(school, tmp_path, sleep=stop) == 130
    assert waits == [1, 2, 4]


def test_restart_clamps_legacy_generic_wait_to_short_backoff(tmp_path):
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
    assert waits == [5]


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
        self.aligns = []
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

    def stop(self):
        self.running = False

    def align_at(self, when):
        self.aligns.append(when)

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


def test_opening_wait_wakes_on_poller_update_without_a_second_sleep(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    school = School()
    school.activities = [native_activity(opens='2026-10-18 12:00:10')]
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_rows = school.activities
    FakePoller.next_wake_once = True
    now = [moment('2026-10-18 12:00:00').timestamp()]
    stop_at = moment('2026-10-18 12:00:09').timestamp()
    def clock(): return now[0]
    def advance(seconds):
        now[0] += max(seconds, 0.0)
        if now[0] >= stop_at:
            raise KeyboardInterrupt
    FakePoller.on_timeout = advance
    slept = []
    def wait(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt
    try:
        assert execute(school, tmp_path, clock=clock, sleep=wait,
                       background_poller=True) == 130
    finally:
        FakePoller.on_timeout = None
    poller = next(entry for entry in FakePoller.instances if entry.versioned_reads)
    assert len(poller.waits) >= 2
    assert poller.versioned_reads >= 1
    # wait_update already consumed the interval; a timeout must not be followed by sleep again.
    assert slept == []
    timings = [line for line in diagnostics_rows(tmp_path) if line['event'] == 'scan_timing']
    assert timings
    assert {'rows_age', 'main_requests', 'poller_polls',
            'rows_fetch_seconds', 'rows_data_age'} <= set(timings[-1])


def test_opening_alignment_is_off_by_default_and_opt_in(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    start = moment('2026-10-18 12:00:10').timestamp()
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    school = School()
    school.activities = [native_activity(opens='2026-10-18 12:00:10')]
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_rows = school.activities
    FakePoller.next_wake_once = False
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock(): return now[0]
    def advance(seconds):
        now[0] += max(seconds, 0.0)
        raise KeyboardInterrupt
    FakePoller.on_timeout = advance
    try:
        assert execute(school, tmp_path, clock=clock, sleep=interrupted,
                       background_poller=True) == 130
        assert all(not entry.aligns for entry in FakePoller.instances)

        FakePoller.instances.clear()
        now[0] = moment('2026-10-18 12:00:00').timestamp()
        assert execute(school, tmp_path, clock=clock, sleep=interrupted,
                       background_poller=True,
                       settings=Settings(state_dir=tmp_path, poller_align=True)) == 130
        aligns = [when for entry in FakePoller.instances for when in entry.aligns]
        assert aligns and abs(aligns[-1] - start) < 1
    finally:
        FakePoller.on_timeout = None


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


def test_disabling_prefetch_and_prewarm_restores_the_fresh_captcha_path(tmp_path):
    school = School()
    school.activities = [native_activity()]
    now = [moment('2026-10-18 11:59:57').timestamp()]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if school.submitted:
            raise KeyboardInterrupt
    settings = Settings(state_dir=tmp_path, prewarm=False, captcha_prefetch=False)
    assert execute(school, tmp_path, clock=clock, sleep=wait, settings=settings) == 130
    lines = diagnostics_rows(tmp_path)
    assert not any(line['event'] in ('prewarm_history', 'captcha_prefetched') for line in lines)
    started = [line for line in lines if line['event'] == 'submission_started']
    assert started and started[0]['captcha_source'] == 'fresh'
    assert len([1 for _, url, _ in school.requests if url.endswith('vcode.do')]) == 1


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


def test_preparation_is_skipped_when_the_opening_is_too_close(tmp_path):
    school = School()
    start = moment('2026-10-18 12:00:01').timestamp()
    school.activities = [native_activity(opens='2026-10-18 12:00:01',
                                         closes='2026-10-18 13:00:00')]
    now = [start - 0.1]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if school.submitted:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait) == 130
    lines = diagnostics_rows(tmp_path)
    assert not any(line['event'] in ('prewarm_history', 'captcha_prefetched')
                   for line in lines)
    started = [line for line in lines if line['event'] == 'submission_started']
    assert started and started[0]['captcha_source'] == 'fresh'
    assert school.submitted == ['one']


def test_preparation_is_deduplicated_across_school_clock_jitter(tmp_path):
    from datetime import datetime
    now = [moment('2026-10-18 11:59:58').timestamp()]
    class JitterSchool(School):
        lists = 0
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if urlsplit(url).path.endswith('queryActivityList.do'):
                self.lists += 1
                # Second-truncated school clock: the estimated offset moves by up to a second
                # between snapshots, so a local-time key would repeat preparation.
                stamp = datetime.fromtimestamp(int(now[0]) + self.lists % 2)
                self.server_now = stamp.strftime('%Y-%m-%d %H:%M:%S')
            return response
    school = JitterSchool()
    school.activities = [native_activity(opens='2026-10-18 12:00:03',
                                         closes='2026-10-18 13:00:00')]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if school.submitted:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait) == 130
    lines = diagnostics_rows(tmp_path)
    assert sum(line['event'] == 'prewarm_history' for line in lines) == 1
    assert sum(line['event'] == 'captcha_prefetched' for line in lines) == 1
    assert school.submitted == ['one']


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
    assert any(line['event'] == 'prewarm_history' and line['at'] >= full_start
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
    seconds = 0.4
    retry_after = 0.0
    server_directed = False
    events = []
    starts = 0

    def __init__(self, query, *, interval, clock, **_kwargs):
        self.query = query
        self.clock = clock
        self.running = True

    def start(self):
        self.running = True
        FakeHistoryPoller.starts += 1

    def stop(self):
        self.running = False
        FakeHistoryPoller.events.append('history_stop')

    def snapshot(self):
        from seu_reserve.poller import Snapshot
        now = self.clock()
        has_error = FakeHistoryPoller.error is not None
        return Snapshot(rows=list(FakeHistoryPoller.rows), at=now,
                        error=FakeHistoryPoller.error,
                        retry_after=FakeHistoryPoller.retry_after,
                        failed_at=now if has_error else 0.0,
                        server_directed=FakeHistoryPoller.server_directed,
                        seconds=FakeHistoryPoller.seconds)


def seed_opening_selection(directory, *, start='2026-10-18T12:00:10+08:00'):
    from seu_reserve.state import Task
    task = Task(directory, 'test-user')
    task.data['selection'] = dict(updated_at=0, activities=[dict(
        title='测试讲座', reason='reservation_not_started',
        reservation_start=start, reservation_end='2026-10-18T13:00:00+08:00')])
    task.save()
    task.close()


def test_background_history_refresh_supplies_the_opening_cache(tmp_path, monkeypatch):
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
    FakePoller.next_wake_once = True
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock(): return now[0]
    def advance(seconds):
        now[0] += max(seconds, 0.0)
        raise KeyboardInterrupt
    FakePoller.on_timeout = advance
    try:
        assert execute(school, tmp_path, clock=clock, sleep=interrupted,
                       background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    lines = diagnostics_rows(tmp_path)
    assert any(line['event'] == 'history_background_used' for line in lines)
    hist_queries = [url for _, url, _ in school.requests if url.endswith('queryMyActivityList.do')]
    assert hist_queries == []


def test_background_history_failure_falls_back_to_the_sync_query(tmp_path, monkeypatch):
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
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock(): return now[0]
    def advance(seconds):
        now[0] += max(seconds, 0.0)
        raise KeyboardInterrupt
    FakePoller.on_timeout = advance
    try:
        assert execute(school, tmp_path, clock=clock, sleep=interrupted,
                       background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    lines = diagnostics_rows(tmp_path)
    assert not any(line['event'] == 'history_background_used' for line in lines)
    hist_queries = [url for _, url, _ in school.requests if url.endswith('queryMyActivityList.do')]
    assert hist_queries


def test_prefetch_uses_the_earliest_plausible_opening_time(tmp_path):
    from datetime import datetime
    start = moment('2026-10-18 12:00:03').timestamp()
    now = [moment('2026-10-18 11:59:58').timestamp() + 0.9]
    class SecondClockSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if urlsplit(url).path.endswith('queryActivityList.do'):
                stamp = datetime.fromtimestamp(int(now[0]))
                self.server_now = stamp.strftime('%Y-%m-%d %H:%M:%S')
            return response
    school = SecondClockSchool()
    school.activities = [native_activity(opens='2026-10-18 12:00:03')]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if school.submitted:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait) == 130
    lines = diagnostics_rows(tmp_path)
    prefetched = [line for line in lines if line['event'] == 'captcha_prefetched']
    assert prefetched
    # The second-truncated school clock makes the naive estimate 0.9s late; the margin must keep
    # the prefetch before the true opening instant, not after it.
    assert prefetched[0]['at'] <= start
    started = [line for line in lines if line['event'] == 'submission_started']
    assert started and started[0]['captcha_source'] == 'prefetch'


def test_submission_persists_once_before_sending(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    records = []
    original_save = app_module.Task.save
    def counting_save(self):
        health = self.data.get('health')
        records.append((self.data.get('status'),
                        health.get('phase') if isinstance(health, dict) else None))
        return original_save(self)
    monkeypatch.setattr(app_module.Task, 'save', counting_save)
    school = School()
    school.activities = [lecture('one')]
    assert execute(school, tmp_path) == 130
    pending_saves = [entry for entry in records if entry[0] == 'pending']
    assert ('pending', 'submitting') in pending_saves
    assert len([entry for entry in pending_saves if entry[1] == 'submitting']) == 1
    captcha_writes = [entry for entry in records if entry[1] == 'captcha']
    assert len(captcha_writes) == 1


class SpringPoller(FakePoller):
    """Serves a stale snapshot first, then a fresh one after the fallback wait wakes."""

    versioned_reads = 0
    wakes = 0

    def snapshot_with_version(self):
        from seu_reserve.poller import Snapshot
        SpringPoller.versioned_reads += 1
        at = self.clock() if SpringPoller.versioned_reads > 1 else self.clock() - 5.0
        return (Snapshot(rows=list(FakePoller.next_rows), at=at, started=at, seconds=0.1),
                SpringPoller.versioned_reads)

    def wait_update(self, timeout, since=None):
        self.waits.append(timeout)
        SpringPoller.wakes += 1
        if SpringPoller.wakes >= 3:
            raise KeyboardInterrupt
        return True


def test_stale_snapshot_waits_for_the_inflight_poll_instead_of_a_duplicate_query(
        tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path)
    monkeypatch.setattr(app_module, 'ActivityPoller', SpringPoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    SpringPoller.versioned_reads = 0
    SpringPoller.wakes = 0
    FakePoller.next_rows = [native_activity(opens='2026-10-18 12:00:10')]
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = 'RemoteError'
    school = School()
    school.activities = list(FakePoller.next_rows)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    clock = lambda: now[0]
    assert execute(school, tmp_path, clock=clock, sleep=interrupted,
                   background_poller=True,
                   settings=Settings(state_dir=tmp_path, prewarm=False,
                                     captcha_prefetch=False)) == 130
    list_queries = [url for _, url, _ in school.requests if url.endswith('queryActivityList.do')]
    # One initial synchronous query to bootstrap the poller; the stale snapshot is then resolved
    # by waiting for the in-flight poll instead of a second synchronous query.
    assert len(list_queries) == 1
    timings = [line for line in diagnostics_rows(tmp_path) if line['event'] == 'scan_timing']
    assert any(line['rows_source'] == 'poller' for line in timings)


def test_prewarm_and_captcha_prefetch_move_work_before_the_candidate(tmp_path):
    school = School()
    school.activities = [native_activity()]
    now = [moment('2026-10-18 11:59:57').timestamp()]
    waits = []
    def clock():
        return now[0]
    def wait(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if school.submitted or len(waits) > 400:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait) == 130
    assert school.submitted == ['one']
    lines = diagnostics_rows(tmp_path)
    assert any(line['event'] == 'prewarm_history' for line in lines)
    assert any(line['event'] == 'captcha_prefetched' for line in lines)
    started = [line for line in lines if line['event'] == 'submission_started']
    assert started and started[0]['captcha_source'] == 'prefetch'
    assert started[0]['prefetch_age'] is not None
    assert 'precommit_save_seconds' in started[0]
    assert started[0]['rows_data_age'] is not None
    prefetch_requests = [url for _, url, _ in school.requests if url.endswith('vcode.do')]
    assert len(prefetch_requests) == 1


def test_health_writes_are_coalesced_and_flushed_on_stop(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    saves = []
    original_save = app_module.Task.save
    def counting_save(self):
        health = self.data.get('health')
        if isinstance(health, dict):
            saves.append((health.get('phase'), health.get('next_check_at')))
        return original_save(self)
    monkeypatch.setattr(app_module.Task, 'save', counting_save)
    school = School()
    school.activities = [native_activity(opens='2026-10-18 12:00:10')]
    now = [moment('2026-10-18 12:00:00').timestamp()]
    stop_at = now[0] + 1.2
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if now[0] >= stop_at:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait) == 130
    # Over ~1.2s of fast scanning the wait-boundary heartbeat is throttled: without coalescing
    # each 0.25s scan wrote several snapshots (~5+ waiting/activity writes).
    in_scan = [entry for entry in saves if entry[0] in ('activities', 'waiting')]
    assert len(in_scan) <= 3
    assert any(phase == 'waiting' and next_check > 0 for phase, next_check in in_scan)
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['health']['phase'] == 'stopped'


def test_school_clock_uncertainty_includes_snapshot_age():
    assert school_clock_uncertainty(0.0) == pytest.approx(1.1)
    assert school_clock_uncertainty(0.7) == pytest.approx(1.8)
    # Request duration is part of the uncertainty too: a slow list response delays when the
    # school-clock reading takes effect even though the consumed snapshot is fresh.
    assert school_clock_uncertainty(0.0, 2.0) == pytest.approx(3.1)
    assert school_clock_uncertainty(0.7, 1.0) == pytest.approx(2.8)


def test_earliest_opening_estimate_never_moves_later():
    first = earliest_opening_local(1000.0, -0.9, 1.1)
    assert first == pytest.approx(999.8)
    # A later offset/margin recomputation must not push the preparation deadline forward.
    assert earliest_opening_local(1000.0, 0.5, 1.1, first) <= first
    assert earliest_opening_local(1000.0, -0.9, 1.8, first) <= first


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


def test_error_backoff_stops_both_query_threads_before_waiting(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.next_rows = [native_activity(opens='2026-10-18 12:00:10')]
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    events = []
    FakeHistoryPoller.events = events
    school = School()
    school.activities = list(FakePoller.next_rows)
    school.query_statuses = [503]
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock(): return now[0]
    def wait(seconds):
        events.append('wait')
        raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait,
                   background_poller=True,
                   settings=Settings(state_dir=tmp_path, prewarm=False,
                                     captcha_prefetch=False)) == 130
    assert 'history_stop' in events and 'wait' in events
    assert events.index('history_stop') < events.index('wait')


class VersionedStalePoller(FakePoller):
    """Reports a stale snapshot with a version newer than anything consumed."""

    since_calls = []

    def snapshot_with_version(self):
        from seu_reserve.poller import Snapshot
        at = self.clock() - 5.0
        return Snapshot(rows=list(FakePoller.next_rows), at=at, started=at - 0.1,
                        seconds=0.1), 5

    def wait_update(self, timeout, since=None):
        VersionedStalePoller.since_calls.append(since)
        # First call is the normal opening wait: let the scan proceed to the fallback path.
        if len(VersionedStalePoller.since_calls) == 1:
            return True
        if len(VersionedStalePoller.since_calls) >= 3:
            raise KeyboardInterrupt
        return False


def test_stale_fallback_waits_for_a_version_newer_than_the_stale_snapshot(
        tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', VersionedStalePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    VersionedStalePoller.since_calls = []
    FakePoller.next_rows = [native_activity(opens='2026-10-18 12:00:10')]
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    school = School()
    school.activities = list(FakePoller.next_rows)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    clock = lambda: now[0]
    assert execute(school, tmp_path, clock=clock, sleep=interrupted,
                   background_poller=True,
                   settings=Settings(state_dir=tmp_path, prewarm=False,
                                     captcha_prefetch=False)) == 130
    # The fallback wait must target the version of the stale snapshot (5), not the last consumed
    # version (0): otherwise wait_update returns immediately without waiting for a newer poll.
    assert 5 in VersionedStalePoller.since_calls


def test_query_threads_wait_for_the_longest_server_wait_before_restarting(
        tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.next_rows = [native_activity(opens='2026-10-18 12:00:10')]
    FakePoller.next_wake_once = True
    FakePoller.at_offset = -5.0
    FakePoller.next_error = None
    FakePoller.error_retry_after = 0.0
    FakePoller.error_server_directed = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    FakeHistoryPoller.events = []
    FakeHistoryPoller.starts = 0
    class LongWaitSchool(School):
        lists = 0
        def request(self, method, url, **kwargs):
            if urlsplit(url).path.endswith('queryActivityList.do'):
                self.lists += 1
                if self.lists >= 2:
                    # The list thread learns a 120s wait while the main loop gets a shorter 10s
                    # wait from the synchronous fallback response.
                    FakePoller.next_error = 'RemoteError'
                    FakePoller.error_retry_after = 120.0
                    FakePoller.error_server_directed = True
                    self.query_statuses = [429]
            response = super().request(method, url, **kwargs)
            if urlsplit(url).path.endswith('queryActivityList.do') and self.lists >= 2:
                response.headers['Retry-After'] = '10'
            return response
    school = LongWaitSchool()
    school.activities = list(FakePoller.next_rows)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def clock(): return now[0]
    def wait(seconds):
        waits.append(seconds)
        now[0] += max(seconds, 0.0)
        if len(waits) >= 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait,
                   background_poller=True,
                   settings=Settings(state_dir=tmp_path, prewarm=False,
                                     captcha_prefetch=False)) == 130
    # The first defer only lasts 10s; the history thread must not restart inside the remaining
    # 110s of the shared wait that the list thread reported.
    assert FakeHistoryPoller.starts == 1
    assert waits and waits[0] == pytest.approx(10.0, abs=0.5)
    FakePoller.next_error = None
    FakePoller.error_retry_after = 0.0
    FakePoller.error_server_directed = False


def test_slow_sync_list_skips_preparation_when_clock_uncertainty_is_large(
        tmp_path, monkeypatch):
    from datetime import datetime
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:08+08:00')
    now = [moment('2026-10-18 11:59:58').timestamp()]
    class SlowListSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if urlsplit(url).path.endswith('queryActivityList.do'):
                now[0] += 2.0  # list request takes two seconds
                stamp = datetime.fromtimestamp(int(now[0]))
                self.server_now = stamp.strftime('%Y-%m-%d %H:%M:%S')
            return response
    school = SlowListSchool()
    school.activities = [native_activity(opens='2026-10-18 12:00:08')]
    def clock(): return now[0]
    def wait(seconds):
        now[0] += max(seconds, 0.0)
        if school.submitted:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=clock, sleep=wait,
                   background_poller=False) == 130
    lines = diagnostics_rows(tmp_path)
    assert any(line['event'] == 'preparation_skipped' for line in lines)
    assert not any(line['event'] in ('prewarm_history', 'captcha_prefetched')
                   for line in lines)
    started = [line for line in lines if line['event'] == 'submission_started']
    assert started and started[0]['captcha_source'] == 'fresh'

def test_mid_scan_history_wait_blocks_the_sync_history_fallback(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller', FakeHistoryPoller)
    activities = [native_activity(opens='2026-10-18 12:00:10')]
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = -5.0
    FakePoller.next_error = None
    FakePoller.next_wake_once = True
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.server_directed = False
    FakeHistoryPoller.retry_after = 0.0
    class MidScanLimited(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('queryActivityList.do'):
                # The refresher learns the server-directed wait after the scan-start check.
                FakeHistoryPoller.error = 'RemoteError'
                FakeHistoryPoller.retry_after = 120.0
                FakeHistoryPoller.server_directed = True
            return response
    school = MidScanLimited()
    school.activities = list(activities)
    FakePoller.next_rows = list(activities)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock(): return now[0]
    def stop(seconds):
        now[0] += max(seconds, 0.0)
        raise KeyboardInterrupt
    FakePoller.on_timeout = stop
    try:
        assert execute(school, tmp_path, clock=clock, sleep=stop, background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    history_queries = [url for _, url, _ in school.requests
                       if url.endswith('queryMyActivityList.do')]
    assert history_queries == []
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved.get('retry_server') is True
    assert saved['retry_at'] >= moment('2026-10-18 12:00:00').timestamp() + 120
    assert 'event=server_backoff' in (tmp_path / 'events.log').read_text()


def test_activity_list_without_an_id_does_not_advance_pending_checks(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
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


def test_invalid_background_history_falls_back_to_sync_in_the_same_scan(tmp_path, monkeypatch):
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
    def stop(_seconds):
        raise KeyboardInterrupt
    FakePoller.on_timeout = stop
    try:
        assert execute(school, tmp_path, clock=clock, sleep=stop, background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    lines = diagnostics_rows(tmp_path)
    invalid = [line for line in lines if line['event'] == 'history_background_invalid']
    assert invalid and invalid[0].get('reason') == 'time_invalid'
    assert not any(line['event'] == 'history_background_used' for line in lines)
    history_queries = [url for _, url, _ in school.requests
                       if url.endswith('queryMyActivityList.do')]
    assert history_queries
    events = (tmp_path / 'events.log').read_text()
    assert 'event=invalid_activity_data' not in events
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['health'].get('failure_count', 0) == 0

def test_background_readers_use_the_opening_query_budget_and_share_the_gate(
        tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:10+08:00')
    seen = {}
    class RecordingPoller(FakePoller):
        def __init__(self, query, *, interval, clock, **kwargs):
            super().__init__(query, interval=interval, clock=clock, **kwargs)
            seen['list_timeout'] = getattr(query.__self__, 'timeout', None)
            seen['list_gate'] = kwargs.get('gate')
    class RecordingHistory(FakeHistoryPoller):
        def __init__(self, query, *, interval, clock, **kwargs):
            super().__init__(query, interval=interval, clock=clock, **kwargs)
            seen['history_timeout'] = getattr(query.__self__, 'timeout', None)
            seen['history_gate'] = kwargs.get('gate')
    monkeypatch.setattr(app_module, 'ActivityPoller', RecordingPoller)
    monkeypatch.setattr(app_module, 'new_history_poller', RecordingHistory)
    activities = [native_activity(opens='2026-10-18 12:00:10')]
    FakePoller.instances.clear()
    FakePoller.rows_override = None
    FakePoller.at_offset = 0.0
    FakePoller.next_error = None
    FakePoller.next_wake_once = False
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.server_directed = False
    FakeHistoryPoller.retry_after = 0.0
    school = School()
    school.activities = list(activities)
    FakePoller.next_rows = list(activities)
    now = [moment('2026-10-18 12:00:00').timestamp()]
    def clock(): return now[0]
    def stop(_seconds): raise KeyboardInterrupt
    FakePoller.on_timeout = stop
    try:
        assert execute(school, tmp_path, clock=clock, sleep=stop, background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    assert seen.get('list_timeout') == 6
    assert seen.get('history_timeout') == 6
    assert seen.get('list_gate') is not None
    assert seen.get('list_gate') is seen.get('history_gate')


def test_invalid_salon_history_falls_back_to_sync_in_the_same_scan(tmp_path, monkeypatch):
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
    FakeHistoryPoller.rows = [{'HD_WID': 'salon1', 'YYZT': '已预约',
                               'JZXL_DISPLAY': '“SEU咖啡间”系列沙龙活动',
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
    def stop(_seconds): raise KeyboardInterrupt
    FakePoller.on_timeout = stop
    try:
        assert execute(school, tmp_path, clock=clock, sleep=stop, background_poller=True,
                       settings=Settings(state_dir=tmp_path, prewarm=False,
                                         captcha_prefetch=False)) == 130
    finally:
        FakePoller.on_timeout = None
    lines = diagnostics_rows(tmp_path)
    invalid = [line for line in lines if line['event'] == 'history_background_invalid']
    assert invalid and invalid[0].get('reason') == 'time_invalid'
    assert not any(line['event'] == 'history_background_used' for line in lines)
    history_queries = [url for _, url, _ in school.requests
                       if url.endswith('queryMyActivityList.do')]
    assert history_queries
    events = (tmp_path / 'events.log').read_text()
    assert 'event=invalid_activity_data' not in events
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['health'].get('failure_count', 0) == 0
