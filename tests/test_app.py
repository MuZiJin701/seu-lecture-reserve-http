import base64
import json
import socket
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from seu_reserve.app import Settings, run, scan_delay
from seu_reserve.model import moment


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
    school.query_statuses = [500] * 30
    events, delays = [], []
    now = 1000.0
    def wait(seconds):
        nonlocal now
        delays.append(seconds)
        now += seconds
        if len(delays) == 6:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, sleep=wait, clock=lambda: now) == 130
    assert events == ['query_failure']
    assert delays[:4] == [20, 40, 80, 160]
    assert sum(delays[:4]) == 300
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
        f'event=task_failed stage={stage} reason=http_failure http_status=502')
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
    delays = []
    def stop(seconds):
        delays.append(seconds)
        raise KeyboardInterrupt
    execute(school, tmp_path, sleep=stop)
    assert delays == [600]
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
    assert delays == [20, 40, 80]
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


def test_pending_miss_twice_releases_and_rescans(tmp_path):
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
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=wait, notify=events.append, clock=lambda: later) == 130
    assert waits == [1, 10]
    assert school.submitted == ['one', 'one']
    assert events == []
    events_log = (tmp_path / 'events.log').read_text()
    assert all(event in events_log for event in (
        'event=reservation_unconfirmed', 'event=reservation_unconfirmed_released',
        'event=reservation_succeeded'))
