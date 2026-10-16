"""Validated HTTPS boundary for CAS, the gateway and lecture APIs."""

import base64
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

BASE = 'https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu'
ENTRY = BASE + '/*default/index.do'
CAS = 'https://auth.seu.edu.cn/auth/casback/'
GATEWAY = 'https://vpn.seu.edu.cn'
API_PARAMS = {'clientType': 'SDPBrowserClient', 'platform': 'Linux', 'lang': 'zh-CN'}
HOSTS = {'auth.seu.edu.cn', 'vpn.seu.edu.cn', 'ehall.seu.edu.cn'}


def has_code(payload: dict[str, Any], expected: int) -> bool:
    return type(payload.get('code')) is int and payload['code'] == expected


class RemoteError(Exception):
    """Only fixed, non-sensitive reason codes cross this boundary."""

    def __init__(self, reason: str, retry_after: float = 0, *, http_status: int | None = None):
        super().__init__(reason)
        self.retry_after = retry_after
        self.http_status = http_status


class AuthenticationExpired(RemoteError):
    pass


class AuthenticationRejected(RemoteError):
    pass


class Client:
    def __init__(self, session: Any, environ: Mapping[str, str],
                 prompt: Callable[[str], str], notify: Callable[[str], None]):
        self.session = session
        self.environ = environ
        self.prompt = prompt
        self.notify = notify

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        parsed = urlsplit(url)
        if parsed.scheme == 'http' and parsed.hostname == 'ehall.seu.edu.cn' and parsed.port in (None, 80):
            url = urlunsplit(('https', 'ehall.seu.edu.cn', parsed.path, parsed.query, parsed.fragment))
            parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in HOSTS
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or any(ord(char) < 32 for char in url)):
            raise RemoteError('unexpected_origin')
        # CAS rejects the default python-requests user agent with HTTP 403.
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)', **kwargs.pop('headers', {})}
        try:
            response = self.session.request(method, url, timeout=25, allow_redirects=False,
                                            verify=True, headers=headers, **kwargs)
        except requests.exceptions.SSLError:
            raise RemoteError('tls_failure') from None
        except requests.exceptions.ProxyError:
            raise RemoteError('proxy_failure') from None
        except requests.Timeout:
            raise RemoteError('network_timeout') from None
        except requests.ConnectionError:
            raise RemoteError('connection_failure') from None
        except requests.RequestException:
            raise RemoteError('network_failure') from None
        if response.status_code in (429, 503):
            from email.utils import parsedate_to_datetime
            retry = response.headers.get('Retry-After', '')
            try:
                seconds = float(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
            raise RemoteError('server_backoff', max(0, seconds), http_status=response.status_code)
        return response

    def get(self, url: str) -> Any:
        for _ in range(12):
            response = self.request('GET', url)
            if response.status_code not in (301, 302, 303, 307, 308):
                if response.status_code != 200:
                    raise RemoteError('http_failure', http_status=response.status_code)
                return response
            location = response.headers.get('Location')
            if not location:
                raise RemoteError('missing_redirect')
            url = urljoin(url, location)
        raise RemoteError('redirect_loop')

    def payload(self, response: Any) -> dict[str, Any]:
        if response.status_code != 200:
            raise RemoteError('http_failure', http_status=response.status_code)
        try:
            result = response.json()
        except ValueError:
            raise RemoteError('invalid_json') from None
        if not isinstance(result, dict):
            raise RemoteError('invalid_response')
        return result

    def cas(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        return self.payload(self.request('POST', CAS + name, json=data))

    @staticmethod
    def service(url: str) -> str:
        try:
            service = parse_qs(urlsplit(url).fragment.split('?', 1)[1])['service'][0]
        except (IndexError, KeyError):
            raise RemoteError('cas_service_missing') from None
        if not service:
            raise RemoteError('cas_service_missing')
        return service

    def login(self, service: str) -> dict[str, Any]:
        username = self.environ.get('SEU_USERNAME', '')
        password = self.environ.get('SEU_PASSWORD', '')
        if not username or not password:
            raise AuthenticationRejected('credentials_missing')
        code = ''
        while True:
            captcha = self.payload(self.request('GET', CAS + 'needCaptcha'))
            if not has_code(captcha, 200):
                self.notify('authentication_required')
                self.prompt('需要尚不支持的登录图形验证码或认证挑战；处理后按回车重试（Ctrl+C 停止）：')
                continue
            key_data = self.cas('getChiperKey', {})
            try:
                encoded = key_data['publicKey']
                key = load_der_public_key(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
                if not isinstance(key, rsa.RSAPublicKey):
                    raise ValueError
                encrypt = lambda value: base64.b64encode(key.encrypt(value.encode(), padding.PKCS1v15())).decode()
                data = {'service': service, 'username': username, 'password': encrypt(password),
                        'captcha': '', 'rememberMe': False, 'loginType': 'account', 'wxBinded': False,
                        'mobilePhoneNum': '', 'fingerPrint': None, 'mobileVerifyCode': encrypt(code) if code else ''}
            except (KeyError, TypeError, ValueError):
                raise RemoteError('cas_public_key_invalid') from None
            result = self.cas('casLogin', data)
            if has_code(result, 200) and isinstance(result.get('redirectUrl'), str):
                return result
            if not has_code(result, 502):
                info = result.get('info', '')
                rejected = (isinstance(info, str) and any(word in info for word in
                            ('密码错误', '密码不正确', '用户名或密码', '账号锁定', '账户锁定', '账号受限', '账号禁用')))
                if result.get('code') in (400, 401, 403, 423) or rejected:
                    self.notify('authentication_rejected')
                    raise AuthenticationRejected('authentication_rejected')
                self.notify('authentication_required')
                self.prompt('需要尚不支持的认证操作；处理后按回车重试（Ctrl+C 停止）：')
                continue
            self.notify('authentication_required')
            while True:
                answer = self.prompt('需要短信认证；输入 send 发送/重发短信，或输入六位验证码（Ctrl+C 停止）：').strip()
                if answer == 'send':
                    sent = self.cas('sendStage2Code', {'userId': username})
                    if not has_code(sent, 200):
                        self.notify('sms_send_failed')
                elif re.fullmatch(r'\d{6}', answer):
                    code = answer
                    break

    def authenticate(self) -> None:
        app = self.get(ENTRY)
        if urlsplit(app.url).hostname == 'ehall.seu.edu.cn' and 'indexjsp.js' in app.text:
            return
        config_url = GATEWAY + '/passport/v1/public/authConfig'
        self.payload(self.request('GET', config_url, params={**API_PARAMS, 'mod': 1, 'needTicket': 1}))
        auth_page = self.get(GATEWAY + '/passport/v1/public/casLogin?sfDomain=CAS-auth')
        service = self.service(auth_page.url)
        result = self.login(service)
        callback = self.get(CAS + 'loginRedirect?redirectUrl=' + result['redirectUrl'])
        query = parse_qs(urlsplit(callback.url).query)
        try:
            data = json.loads(query.get('data', ['{}'])[0])
            config = self.payload(self.request('GET', config_url, params={**API_PARAMS, 'mod': 1, 'needTicket': 1}))
            headers = {'x-csrf-token': config['data']['security']['csrfToken'],
                       'x-sdp-traceid': uuid.uuid4().hex, 'Referer': callback.url}
        except (KeyError, TypeError, ValueError):
            raise RemoteError('gateway_config_invalid') from None
        if data.get('env', {}).get('need') and data.get('ticket'):
            device = uuid.uuid4().hex
            report = self.payload(self.request('POST', GATEWAY + '/controller/v1/public/reportEnv',
                params=API_PARAMS, headers=headers, json={'ticket': data['ticket'], 'deviceId': device,
                'env': {'endpoint': {'device_id': device, 'device': {'type': 'browser'}}}}))
            if not has_code(report, 0):
                raise RemoteError('gateway_environment_rejected')
        next_service = query.get('nextService', [''])[0]
        for _ in range(4):
            if not next_service:
                break
            if next_service not in ('auth/authCheck', 'auth/accessCheck'):
                self.notify('authentication_required')
                self.prompt('网关需要尚不支持的认证操作；处理后按回车重试（Ctrl+C 停止）：')
                raise AuthenticationExpired('gateway_challenge')
            params = dict(API_PARAMS)
            if query.get('taskId'):
                params['taskId'] = query['taskId'][0]
            method = 'GET' if next_service.endswith('authCheck') else 'POST'
            checked = self.payload(self.request(method, GATEWAY + '/passport/v1/' + next_service,
                                                params=params, headers=headers))
            if not has_code(checked, 0):
                raise RemoteError('gateway_authentication_rejected')
            next_service = checked.get('data', {}).get('nextService', '')
        if next_service:
            raise RemoteError('gateway_steps_exhausted')
        config = self.payload(self.request('GET', config_url, params={**API_PARAMS, 'mod': 1}, headers=headers))
        if config.get('data', {}).get('isLogin') != 1:
            raise RemoteError('gateway_not_authenticated')
        app = self.get(ENTRY)
        if urlsplit(app.url).hostname == 'auth.seu.edu.cn':
            verified = self.cas('verifyTgt', {'service': self.service(app.url), 'loginType': 'account'})
            if not has_code(verified, 201) or not isinstance(verified.get('redirectUrl'), str):
                raise AuthenticationExpired('application_authentication_required')
            app = self.get(verified['redirectUrl'])
        if urlsplit(app.url).hostname != 'ehall.seu.edu.cn' or 'indexjsp.js' not in app.text:
            raise RemoteError('application_unavailable')

    def business(self, endpoint: str, *, method: str = 'POST', **kwargs: Any) -> dict[str, Any]:
        response = self.request(method, BASE + '/modules/hdyy/' + endpoint, **kwargs)
        if response.status_code in (301, 302, 303, 307, 308, 401, 403):
            raise AuthenticationExpired('business_authentication_expired', http_status=response.status_code)
        return self.payload(response)

    def rows(self, endpoint: str, field: str, data: dict[str, str]) -> list[dict[str, Any]]:
        payload = self.business(endpoint, data=data)
        datas = payload.get('datas')
        if not has_code(payload, 0) or not isinstance(datas, dict) or not isinstance(datas.get(field), list):
            raise RemoteError('business_response_invalid')
        rows = datas[field]
        if not all(isinstance(row, dict) for row in rows):
            raise RemoteError('business_rows_invalid')
        for source in (payload, datas):
            for key in ('total', 'totalSize', 'totalCount'):
                if key in source and (type(source[key]) is not int or source[key] != len(rows)):
                    raise RemoteError('business_rows_incomplete')
        return rows

    def activities(self) -> list[dict[str, Any]]:
        return self.rows('queryActivityList.do', 'hdlbList', {})

    def mine(self) -> list[dict[str, Any]]:
        return self.rows('queryMyActivityList.do', 'wdhdList', {'param': '{}'})

    def preflight(self, wid: str) -> bool:
        return has_code(self.business('appiontCheck.do', method='GET', params={'wid': wid}), 0)

    def captcha(self) -> bytes:
        from io import BytesIO
        from PIL import Image, UnidentifiedImageError
        payload = self.business('vcode.do', method='GET', params={'_': int(time.time() * 1000)})
        if not has_code(payload, 0):
            raise RemoteError('captcha_rejected')
        def image_value(value: Any) -> str | None:
            if isinstance(value, str) and value.startswith('data:image/'):
                return value
            if isinstance(value, dict):
                for child in value.values():
                    found = image_value(child)
                    if found:
                        return found
            return None
        source = image_value(payload)
        try:
            if not source or len(source) > 2_000_000:
                raise ValueError
            binary = base64.b64decode(source.split(',', 1)[1], validate=True)
            with Image.open(BytesIO(binary)) as image:
                if image.width * image.height > 1_000_000:
                    raise ValueError
                image.verify()
            return binary
        except (ValueError, IndexError, OSError, UnidentifiedImageError):
            raise RemoteError('captcha_invalid') from None

    def submit(self, wid: str, code: str) -> dict[str, Any]:
        return self.business('addReservation.do', data={'wid': wid, 'vcode': code})
