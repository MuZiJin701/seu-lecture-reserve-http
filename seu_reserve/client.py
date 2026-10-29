"""Validated HTTPS boundary for CAS, the gateway and lecture APIs."""

import base64
import io
import json
import math
import re
import select
import socket
import ssl
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from http.client import RemoteDisconnected
from ipaddress import ip_address
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key
from urllib3.connection import HTTPConnection as Urllib3HTTPConnection
from urllib3.connection import HTTPSConnection as Urllib3HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool as Urllib3HTTPConnectionPool
from urllib3.connectionpool import HTTPSConnectionPool as Urllib3HTTPSConnectionPool
from urllib3.exceptions import ReadTimeoutError

BASE = 'https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu'
ENTRY = BASE + '/*default/index.do'
CAS = 'https://auth.seu.edu.cn/auth/casback/'
GATEWAY = 'https://vpn.seu.edu.cn'
API_PARAMS = {'clientType': 'SDPBrowserClient', 'platform': 'Linux', 'lang': 'zh-CN'}
HOSTS = {'auth.seu.edu.cn', 'vpn.seu.edu.cn', 'ehall.seu.edu.cn'}
# Read bodies in large chunks and bound each blocking read by the remaining request budget:
# one-byte iteration made every response byte pass through Python code and timing checks.
RESPONSE_CHUNK_SIZE = 65536
# The school rejects too-frequent SMS sends; after a failure, block local resends for this long.
SMS_RESEND_DELAY = 60.0


class _DeadlineSocket:
    """Make every socket read wait no longer than the total response budget.

    A Requests/urllib3 read may loop internally (gzip decoding, chunked framing) and a socket
    timeout only limits a single recv. Routing each recv through select() with the remaining
    budget lets the deadline interrupt those loops; the timeout surfaces as a normal read
    timeout and is reported as network_timeout.
    """

    def __init__(self, sock: Any, deadline: float):
        self._sock = sock
        self._deadline = deadline

    def recv_into(self, buffer: Any, nbytes: int = 0, flags: int = 0) -> int:
        if not self._wait_for_data():
            raise socket.timeout('response deadline exceeded')
        if not self._tighten_timeout():
            raise socket.timeout('response deadline exceeded')
        return self._sock.recv_into(buffer, nbytes, flags)

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        if not self._wait_for_data():
            raise socket.timeout('response deadline exceeded')
        if not self._tighten_timeout():
            raise socket.timeout('response deadline exceeded')
        return self._sock.recv(bufsize, flags)

    def _tighten_timeout(self) -> bool:
        # select() only says the raw socket is readable; TLS may still block waiting for the
        # rest of a record. Bound that inner blocking read by the same deadline.
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            self._sock.settimeout(max(remaining, 0.001))
        except OSError:
            pass
        return True

    def _wait_for_data(self) -> bool:
        # TLS may already hold decrypted bytes that select() cannot see.
        pending = getattr(self._sock, 'pending', None)
        if pending is not None and pending():
            return True
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            return False
        return bool(select.select([self._sock], [], [], remaining)[0])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)

    def makefile(self, mode: str = 'rb', buffering: int = -1) -> Any:
        # Used while http.client reads the status line and response headers; the reader then
        # stops blocking once the request deadline is reached. Mirror socket.makefile()'s
        # reference counting so http.client closing the connection does not close the fd early.
        self._sock._io_refs += 1
        return io.BufferedReader(socket.SocketIO(self, mode))  # type: ignore[arg-type]

    def _decref_socketios(self) -> None:
        # socket.SocketIO.close() calls this on its owner; keep the real socket's refcounts.
        self._sock._decref_socketios()


class _DeadlineHTTPConnection(Urllib3HTTPConnection):
    _response_deadline: float | None = None

    def request(self, *args: Any, **kwargs: Any) -> None:
        timeout = self.timeout
        fallback = (time.monotonic() + float(timeout)) if timeout is not None else None
        self._response_deadline = _current_request_deadline() or fallback
        Urllib3HTTPConnection.request(self, *args, **kwargs)

    def connect(self) -> None:
        deadline = _current_request_deadline()
        if deadline is None:
            _connect_and_measure(Urllib3HTTPConnection.connect, self)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout('response deadline exceeded')
        original = self.timeout
        self.timeout = min(float(original), remaining) if original is not None else remaining
        try:
            _connect_and_measure(Urllib3HTTPConnection.connect, self)
        finally:
            if original is not None:
                self.timeout = original
        if time.monotonic() > deadline:
            self.close()
            raise socket.timeout('response deadline exceeded')

    def getresponse(self) -> Any:
        deadline = self._response_deadline
        if deadline is None or self.sock is None:
            return Urllib3HTTPConnection.getresponse(self)
        sock = self.sock
        wrapped = _DeadlineSocket(sock, deadline)
        setattr(self, 'sock', wrapped)
        try:
            response = Urllib3HTTPConnection.getresponse(self)
        finally:
            if getattr(self, 'sock', None) is wrapped:
                setattr(self, 'sock', sock)
        return response


class _DeadlineHTTPSConnection(Urllib3HTTPSConnection):
    _response_deadline: float | None = None

    def request(self, *args: Any, **kwargs: Any) -> None:
        timeout = self.timeout
        fallback = (time.monotonic() + float(timeout)) if timeout is not None else None
        self._response_deadline = _current_request_deadline() or fallback
        Urllib3HTTPSConnection.request(self, *args, **kwargs)

    def connect(self) -> None:
        deadline = _current_request_deadline()
        if deadline is None:
            _connect_and_measure(Urllib3HTTPSConnection.connect, self)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout('response deadline exceeded')
        original = self.timeout
        self.timeout = min(float(original), remaining) if original is not None else remaining
        try:
            _connect_and_measure(Urllib3HTTPSConnection.connect, self)
        finally:
            if original is not None:
                self.timeout = original
        if time.monotonic() > deadline:
            self.close()
            raise socket.timeout('response deadline exceeded')

    def getresponse(self) -> Any:
        deadline = self._response_deadline
        if deadline is None or self.sock is None:
            return Urllib3HTTPSConnection.getresponse(self)
        sock = self.sock
        wrapped = _DeadlineSocket(sock, deadline)
        setattr(self, 'sock', wrapped)
        try:
            response = Urllib3HTTPSConnection.getresponse(self)
        finally:
            if getattr(self, 'sock', None) is wrapped:
                setattr(self, 'sock', sock)
        return response


class _DeadlineHTTPConnectionPool(Urllib3HTTPConnectionPool):
    ConnectionCls = _DeadlineHTTPConnection


class _DeadlineHTTPSConnectionPool(Urllib3HTTPSConnectionPool):
    ConnectionCls = _DeadlineHTTPSConnection


DEADLINE_POOLS = {'http': _DeadlineHTTPConnectionPool, 'https': _DeadlineHTTPSConnectionPool}
# Set by Client.request for the duration of session.request(); the deadline-aware connection
# classes read it so connect + headers + body share one wall-clock budget.
REQUEST_DEADLINE = threading.local()


def _current_request_deadline() -> float | None:
    return getattr(REQUEST_DEADLINE, 'value', None)


# Written by the deadline-aware connection classes only when a request opens a new connection,
# and saved/cleared by Client.request so each request reports its own connection setup cost.
REQUEST_CONNECT = threading.local()


def _connect_and_measure(connect: Callable[[Any], None], connection: Any) -> None:
    """Run one new-connection attempt and record its TCP/TLS handshake duration.

    urllib3 calls connect() only for a connection that is not reusable, so writing here keeps
    reused connections from reporting a connection cost.
    """
    started = time.monotonic()
    try:
        connect(connection)
    finally:
        REQUEST_CONNECT.value = time.monotonic() - started


def install_deadline_pools(session: Any) -> None:
    """Make new connections (direct and proxy) under a session deadline-aware."""
    adapters = getattr(session, 'adapters', None)
    if not isinstance(adapters, dict):
        return
    for adapter in adapters.values():
        manager = getattr(adapter, 'poolmanager', None)
        if manager is not None:
            manager.pool_classes_by_scheme = dict(DEADLINE_POOLS)
        for proxy_manager in getattr(adapter, 'proxy_manager', {}).values():
            proxy_manager.pool_classes_by_scheme = dict(DEADLINE_POOLS)
        if isinstance(adapter, DeadlineAdapter) or getattr(adapter, '_deadline_proxy', False):
            continue
        original = adapter.proxy_manager_for
        def proxy_manager_for(proxy: str, _original: Any = original, **kwargs: Any) -> Any:
            proxy_manager = _original(proxy, **kwargs)
            proxy_manager.pool_classes_by_scheme = dict(DEADLINE_POOLS)
            return proxy_manager
        adapter.proxy_manager_for = proxy_manager_for  # type: ignore[method-assign]
        adapter._deadline_proxy = True


class DeadlineAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(DEADLINE_POOLS)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        manager: Any = super().proxy_manager_for(proxy, **proxy_kwargs)
        manager.pool_classes_by_scheme = dict(DEADLINE_POOLS)
        return manager


class GatewayIPAdapter(DeadlineAdapter):
    def __init__(self, address: str):
        # shortcut: a configured gateway IP can change; update it when the university moves the gateway.
        self.address = str(ip_address(address))
        super().__init__()

    def build_connection_pool_key_attributes(self, request: requests.PreparedRequest,
            verify: Any, cert: Any = None) -> Any:
        attributes: Any = super().build_connection_pool_key_attributes(request, verify, cert)
        host, tls = attributes
        tls['server_hostname'] = host['host']
        host['host'] = self.address
        return host, tls

    def add_headers(self, request: requests.PreparedRequest, **kwargs: Any) -> None:
        request.headers['Host'] = 'vpn.seu.edu.cn'


def has_code(payload: dict[str, Any], expected: int) -> bool:
    return type(payload.get('code')) is int and payload['code'] == expected


def retry_after_seconds(response: Any) -> float:
    """Return the server's Retry-After in seconds, or 0 when unusable/missing."""
    from email.utils import parsedate_to_datetime
    retry = response.headers.get('Retry-After', '')
    try:
        seconds = float(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp() - time.time()
    except (ValueError, TypeError, OverflowError, OSError):
        seconds = 0
    if not math.isfinite(seconds) or seconds > 253402214400 - time.time():
        seconds = 0
    return max(0, seconds)


def gateway_connection_failure(error: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (socket.gaierror, ConnectionResetError, ConnectionAbortedError,
                                BrokenPipeError, RemoteDisconnected, ssl.SSLEOFError)):
            return True
        pending.extend(value for value in (current.__cause__, current.__context__,
                       getattr(current, 'reason', None), *current.args)
                       if isinstance(value, BaseException))
    return False


class RemoteError(Exception):
    """Only fixed, non-sensitive reason codes cross this boundary."""

    def __init__(self, reason: str, retry_after: float = 0, *, http_status: int | None = None):
        super().__init__(reason)
        self.retry_after = retry_after
        self.http_status = http_status
        self.host: str | None = None
        self.tls_reason: str | None = None
        self.proxy_used: bool | None = None
        self.gateway_fallback_allowed = False


class AuthenticationExpired(RemoteError):
    pass


class AuthenticationRejected(RemoteError):
    pass


class Client:
    def __init__(self, session: Any, environ: Mapping[str, str],
                 prompt: Callable[[str], str], notify: Callable[[str], None]):
        self.session = session
        install_deadline_pools(session)
        self.environ = environ
        self.prompt = prompt
        self.notify = notify
        self.timeout = 10.0
        # Observability for callers (app.py): one counter per request attempt and the handshake
        # cost of the connection that attempt opened, if any.
        self.requests_made = 0
        self.last_connect_seconds: float | None = None
        address = environ.get('SEU_VPN_IP', '').strip()
        self.gateway_adapter = GatewayIPAdapter(address) if address else None
        self.initial_authentication = True
        # The school rejects too-frequent SMS sends. The deadline lives on the instance: a
        # campus -> gateway fallback calls login() a second time and must not reset it.
        self.sms_retry_at = 0.0

    def clone_readonly(self) -> 'Client':
        """Independent client with a copied-cookie session for read-only background queries.

        A requests.Session is not safe to share between threads, so the opening poller gets
        its own session; the server treats the copied cookies as the same logged-in session.
        """
        session = requests.Session()
        cookies = getattr(self.session, 'cookies', None)
        if cookies is not None:
            session.cookies.update(cookies)
        clone = Client(session, self.environ, self.prompt, self.notify)
        clone.timeout = self.timeout
        return clone

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        # Keep the bookkeeping around every attempt, including local rejections and failures:
        # the connection classes report a handshake in REQUEST_CONNECT on this thread only.
        previous_connect = getattr(REQUEST_CONNECT, 'value', None)
        REQUEST_CONNECT.value = None
        self.requests_made += 1
        try:
            return self._request(method, url, **kwargs)
        finally:
            self.last_connect_seconds = getattr(REQUEST_CONNECT, 'value', None)
            REQUEST_CONNECT.value = previous_connect

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
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
        deadline = time.monotonic() + float(self.timeout)
        try:
            previous_deadline = getattr(REQUEST_DEADLINE, 'value', None)
            REQUEST_DEADLINE.value = deadline
            try:
                response = self.session.request(method, url, timeout=self.timeout,
                                                allow_redirects=False, verify=True, headers=headers,
                                                stream=True, **kwargs)
            finally:
                REQUEST_DEADLINE.value = previous_deadline
            if response.status_code in (429, 502, 503, 504):
                # Decide from the status line and headers first: a slow or malformed error body
                # must not turn an explicit rate-limit/overload answer into a generic network
                # timeout that loses the wait the school asked for.
                status = response.status_code
                retry_after = retry_after_seconds(response)
                self._discard(response)
                if status == 429:
                    raise RemoteError('server_backoff', retry_after, http_status=429)
                raise RemoteError('service_unavailable', retry_after, http_status=status)
            if response.status_code != 200:
                # Only 200 bodies are consumed later (JSON payload or captcha image); redirect
                # and error bodies are never needed.
                self._discard(response)
                return response
            if getattr(response, 'raw', None) is not None:
                # A single Requests/urllib3 timeout is reset between reads; read the body with
                # the remaining wall-clock budget as the per-read socket timeout so a slow-drip
                # response cannot overshoot the total response deadline.
                response._content = self._read_body(response, deadline)
                response._content_consumed = True
                response.close()
        except ReadTimeoutError:
            # Header/status read hit the total response budget.
            raise RemoteError('network_timeout') from None
        except requests.exceptions.SSLError as exc:
            failure = RemoteError('tls_failure')
            failure.host = parsed.hostname
            message = str(exc)
            failure.tls_reason = next((reason.lower() for reason in (
                'CERTIFICATE_VERIFY_FAILED', 'UNSAFE_LEGACY_RENEGOTIATION_DISABLED',
                'UNEXPECTED_EOF_WHILE_READING', 'DH_KEY_TOO_SMALL',
                'SSLV3_ALERT_HANDSHAKE_FAILURE', 'TLSV1_ALERT_PROTOCOL_VERSION', 'WRONG_VERSION_NUMBER'
            ) if reason in message), 'unknown')
            failure.gateway_fallback_allowed = parsed.hostname == 'vpn.seu.edu.cn' and (
                failure.tls_reason == 'unexpected_eof_while_reading'
                or failure.tls_reason == 'unknown' and gateway_connection_failure(exc))
            if hasattr(self.session, 'merge_environment_settings'):
                settings = self.session.merge_environment_settings(
                    url, dict(kwargs.get('proxies') or {}), None, True, None)
                failure.proxy_used = bool(requests.utils.select_proxy(url, settings['proxies']))
            raise failure from None
        except requests.exceptions.ProxyError:
            raise RemoteError('proxy_failure') from None
        except requests.exceptions.ConnectTimeout:
            # The TCP connection was never established, so the request was not sent; this is
            # safe to retry and must not be treated as an unknown submission outcome.
            raise RemoteError('connect_timeout') from None
        except requests.Timeout:
            raise RemoteError('network_timeout') from None
        except requests.ConnectionError as exc:
            failure = RemoteError('connection_failure')
            failure.gateway_fallback_allowed = (parsed.hostname == 'vpn.seu.edu.cn'
                                                and gateway_connection_failure(exc))
            raise failure from None
        except requests.RequestException:
            raise RemoteError('network_failure') from None
        return response

    @staticmethod
    def _discard(response: Any) -> None:
        if getattr(response, 'raw', None) is not None:
            response.close()

    @staticmethod
    def _response_socket(raw: Any) -> Any:
        """Best-effort access to the socket behind a streamed urllib3 response."""
        sock = getattr(getattr(raw, '_connection', None), 'sock', None)
        if sock is not None:
            return sock
        buffered = getattr(getattr(raw, '_fp', None), 'fp', None)
        return getattr(getattr(buffered, 'raw', None), '_sock', None)

    @staticmethod
    def _socket_holder(raw: Any) -> Any:
        """The SocketIO wrapper whose ``_sock`` the transport reads from, if reachable."""
        return getattr(getattr(getattr(raw, '_fp', None), 'fp', None), 'raw', None)

    def _read_body(self, response: Any, deadline: float) -> bytes:
        raw = response.raw
        read1 = getattr(raw, 'read1', None)
        if read1 is None:
            return self._read_body_chunks(response, deadline)
        sock = self._response_socket(raw)
        holder = self._socket_holder(raw)
        header_socket = getattr(holder, '_sock', None) if holder is not None else None
        if holder is not None and isinstance(header_socket, _DeadlineSocket):
            # Headers were read through a temporary deadline wrapper; hand the body reader the
            # real socket so normal keep-alive and close semantics stay intact.
            real = header_socket._sock
            holder._sock = real
            sock = real
        original_timeout = None
        if sock is not None:
            try:
                original_timeout = sock.gettimeout()
            except OSError:
                sock = None
        # One read1() call may loop over many recvs (gzip decoding, chunked framing); route each
        # recv through a select() bounded by the deadline so those loops cannot outlive it.
        wrapped = None
        if sock is not None and holder is not None and getattr(holder, '_sock', None) is sock:
            wrapped = _DeadlineSocket(sock, deadline)
            holder._sock = wrapped
        body = bytearray()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RemoteError('network_timeout')
                if sock is not None:
                    try:
                        # Bound a single blocking recv as well; the wrapper enforces the total
                        # budget across the transport's internal read loop.
                        sock.settimeout(max(remaining, 0.001))
                    except OSError:
                        sock = None
                try:
                    # decode_content matches Requests' own body reads; the raw flag defaults to
                    # False and would otherwise return compressed bytes to JSON parsing.
                    chunk = read1(RESPONSE_CHUNK_SIZE, decode_content=True)
                except (ReadTimeoutError, TimeoutError):
                    raise RemoteError('network_timeout') from None
                except Exception:
                    # Body failures happen after the request was sent, so the outcome is
                    # unknown; a fixed reason code keeps submissions pending, never retried.
                    raise RemoteError('connection_failure') from None
                if not chunk:
                    break
                body.extend(chunk)
        except RemoteError:
            self._discard(response)
            raise
        finally:
            if wrapped is not None:
                holder._sock = wrapped._sock
            if sock is not None and original_timeout is not None:
                try:
                    sock.settimeout(original_timeout)
                except OSError:
                    pass
        return bytes(body)

    def _read_body_chunks(self, response: Any, deadline: float) -> bytes:
        """Fallback for response objects without read1(); bounded per received chunk."""
        chunks: list[bytes] = []
        try:
            for chunk in response.iter_content(chunk_size=RESPONSE_CHUNK_SIZE):
                if time.monotonic() > deadline:
                    raise RemoteError('network_timeout')
                if chunk:
                    chunks.append(chunk)
        except (ReadTimeoutError, TimeoutError):
            self._discard(response)
            raise RemoteError('network_timeout') from None
        except RemoteError:
            self._discard(response)
            raise
        return b''.join(chunks)

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
            sms_hint = ''
            while True:
                instruction = ('需要短信认证；输入 send 发送/重发短信，或输入六位验证码'
                               '（Ctrl+C 停止）：')
                if sms_hint:
                    instruction = sms_hint + '\n' + instruction
                answer = self.prompt(instruction).strip()
                sms_hint = ''
                if answer == 'send':
                    now = time.monotonic()
                    if now < self.sms_retry_at:
                        wait = max(1, math.ceil(self.sms_retry_at - now))
                        sms_hint = f'短信发送被学校拒绝或过于频繁，请等待约 {wait} 秒后再输入 send。'
                        continue
                    try:
                        sent = self.cas('sendStage2Code', {'userId': username})
                    except RemoteError as exc:
                        # A network, TLS or 429 failure while sending the SMS is not fatal: report
                        # it, cool down for at least the local interval (or the school's
                        # Retry-After) and keep the prompt loop so the next input is not lost.
                        self.notify('sms_send_failed')
                        self.sms_retry_at = time.monotonic() + max(SMS_RESEND_DELAY, exc.retry_after)
                        sms_hint = '短信发送失败，可能请求过于频繁；请稍后重试，或直接输入六位验证码。'
                        continue
                    if not has_code(sent, 200):
                        self.notify('sms_send_failed')
                        self.sms_retry_at = time.monotonic() + SMS_RESEND_DELAY
                        sms_hint = '短信发送失败，可能请求过于频繁；请稍后重试，或直接输入六位验证码。'
                elif re.fullmatch(r'\d{6}', answer):
                    code = answer
                    break

    def authenticate(self) -> None:
        try:
            self._authenticate()
        except RemoteError as exc:
            if not (self.initial_authentication and self.gateway_adapter is not None
                    and exc.gateway_fallback_allowed):
                raise
            adapter, self.gateway_adapter = self.gateway_adapter, None
            self.session.mount(GATEWAY + '/', adapter)
            self.session.mount(GATEWAY + ':443/', adapter)
            # Restart authentication, never replay a failed request or reservation submission.
            self._authenticate()
        self.initial_authentication = False

    def _authenticate(self) -> None:
        app = self.get(ENTRY)
        if urlsplit(app.url).hostname == 'ehall.seu.edu.cn' and 'indexjsp.js' in app.text:
            return
        if urlsplit(app.url).hostname == 'auth.seu.edu.cn':
            service: str | None = None
            try:
                service = self.service(app.url)
            except RemoteError as exc:
                if str(exc) != 'cas_service_missing':
                    raise
            if service is not None and urlsplit(service).hostname == 'ehall.seu.edu.cn':
                # Campus access goes straight to the application's CAS service, without VPN.
                # Login errors (credentials, challenges, network failures inside login()) keep
                # their existing behavior: only the application-entry step below may fall back.
                result = self.login(service)
                try:
                    app = self.get(CAS + 'loginRedirect?redirectUrl=' + result['redirectUrl'])
                except (AuthenticationRejected, AuthenticationExpired):
                    # Authentication failures are never routing failures.
                    raise
                except RemoteError:
                    # After a successful campus CAS login any HTTP/TLS/network failure while
                    # entering the application (for example after a network change) means the
                    # campus route is unusable: fall back once to the existing gateway flow.
                    self.notify('campus_login_fallback')
                else:
                    if (urlsplit(app.url).hostname == 'ehall.seu.edu.cn'
                            and 'indexjsp.js' in app.text):
                        return
                    # A 200 response that is not the application is the same routing failure.
                    self.notify('campus_login_fallback')
            elif service is None:
                self.notify('campus_login_fallback')
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

    def dataset(self, payload: dict[str, Any], field: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
        return datas, rows

    def rows(self, endpoint: str, field: str, data: dict[str, str]) -> list[dict[str, Any]]:
        payload = self.business(endpoint, data=data)
        _, rows = self.dataset(payload, field)
        return rows

    def activities(self) -> list[dict[str, Any]]:
        payload = self.business('queryActivityList.do', data={})
        datas, rows = self.dataset(payload, 'hdlbList')
        # The school UI joins these per-activity reservation counts into the activity list.
        counts: dict[str, Any] = {}
        hdrs = datas.get('hdrsList')
        if isinstance(hdrs, list):
            for item in hdrs:
                if isinstance(item, dict) and isinstance(item.get('HD_WID'), str):
                    counts[item['HD_WID']] = item.get('YYRS')
        server_now = datas.get('xtdqsj')
        merged: list[dict[str, Any]] = []
        for row in rows:
            activity = dict(row, _SERVER_NOW=server_now)
            for key in ('HD_WID', 'WID'):
                identifier = activity.get(key)
                if isinstance(identifier, str) and identifier in counts:
                    activity['YYRS'] = counts[identifier]
                    break
            merged.append(activity)
        return merged

    def mine(self) -> list[dict[str, Any]]:
        return self.rows('queryMyActivityList.do', 'wdhdList', {'param': '{}'})

    def cancel(self, wid: str) -> dict[str, Any]:
        return self.business('deleteReservation.do', data={'wid': wid})

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
