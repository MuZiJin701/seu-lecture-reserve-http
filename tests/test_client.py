import base64
import datetime
import gzip
import json
import re
import socket
import ssl
import threading
import time
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import quote, urlencode, urlsplit

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from urllib3.util import connection
from urllib3.exceptions import ProtocolError

from seu_reserve.client import (Client, DEADLINE_POOLS, ENTRY, GATEWAY, GatewayIPAdapter,
                                RemoteError, install_deadline_pools)
from seu_reserve.state import (FINGERPRINT_FILE, TRUST_COOKIES_FILE, load_trust_cookies,
                               save_trust_cookies)


@pytest.fixture
def gateway_server(tmp_path, monkeypatch, request):
    hostname, dns_works = request.param if isinstance(request.param, tuple) else (request.param, False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path = tmp_path / 'certificate.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path = tmp_path / 'key.pem'
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    received = []
    sni = []
    destinations = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.headers['Host'], self.headers.get('Cookie')))
            if self.path.endswith('/slow-gzip'):
                payload = gzip.compress(b'{"code": 0}')
                self.send_response(200)
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload[:1])
                    self.wfile.flush()
                    time.sleep(0.3)
                    self.wfile.write(payload[1:])
                    self.wfile.flush()
                except OSError:
                    pass
                return
            if self.path.endswith('/slow-headers'):
                try:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n')
                    self.wfile.flush()
                    for index in range(10):
                        time.sleep(0.1)
                        self.wfile.write(f'X-Slow-{index}: value\r\n'.encode())
                        self.wfile.flush()
                    self.wfile.write(b'Content-Length: 2\r\n\r\nok')
                    self.wfile.flush()
                except OSError:
                    pass
                return
            self.send_response(200)
            self.send_header('Set-Cookie', 'gateway=test; Secure; Path=/')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    context.set_servername_callback(lambda sock, name, ctx: sni.append(name))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    create_connection = connection.create_connection
    getaddrinfo = socket.getaddrinfo

    def connect(address, *args, **kwargs):
        destinations.append(address)
        if address == ('127.0.0.1', 443) or dns_works and address == ('vpn.seu.edu.cn', 443):
            address = server.server_address
        return create_connection(address, *args, **kwargs)

    def resolve(host, *args, **kwargs):
        if host != '127.0.0.1':
            raise socket.gaierror('simulated DNS failure; offline tests cannot resolve remote hosts')
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(connection, 'create_connection', connect)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setenv('REQUESTS_CA_BUNDLE', str(cert_path))
    for variable in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(variable, raising=False)
    try:
        yield received, sni, destinations
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('gateway_server', ['vpn.seu.edu.cn'], indirect=True)
def test_gateway_ip_follows_dns_failure_preserving_tls_origin_and_cookies(gateway_server, monkeypatch):
    received, sni, destinations = gateway_server
    with requests.Session() as session:
        client = Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(GATEWAY + '/public'))
        client.authenticate()
        response = client.request('GET', GATEWAY + ':443/public')
        assert response.status_code == 200
        assert response.url == GATEWAY + ':443/public'
        assert all(cookie.domain == 'vpn.seu.edu.cn' for cookie in session.cookies)
    assert destinations == [('vpn.seu.edu.cn', 443)] + [('127.0.0.1', 443)] * 2
    assert sni == ['vpn.seu.edu.cn'] * 2
    assert received == [('vpn.seu.edu.cn', None), ('vpn.seu.edu.cn', 'gateway=test')]


@pytest.mark.parametrize('gateway_server, trusted', [
    ('other.example', True), ('vpn.seu.edu.cn', False),
], indirect=['gateway_server'])
def test_gateway_ip_rejects_wrong_domain_or_untrusted_certificates(gateway_server, trusted, monkeypatch):
    received, sni, destinations = gateway_server
    if not trusted:
        monkeypatch.delenv('REQUESTS_CA_BUNDLE')
    with requests.Session() as session:
        client = Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(GATEWAY + '/public'))
        with pytest.raises(RemoteError) as error:
            client.authenticate()
    assert error.value.tls_reason == 'certificate_verify_failed'
    assert received == []
    assert sni == ['vpn.seu.edu.cn']
    assert destinations == [('vpn.seu.edu.cn', 443), ('127.0.0.1', 443)]


@pytest.mark.parametrize('gateway_server', [('vpn.seu.edu.cn', True)], indirect=True)
def test_configured_ip_is_unused_when_dns_works(gateway_server, monkeypatch):
    received, sni, destinations = gateway_server
    with requests.Session() as session:
        adapter = session.get_adapter(GATEWAY + '/')
        client = Client(session, {'SEU_VPN_IP': '192.0.2.1'}, input, lambda _: None)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(GATEWAY + '/public'))
        client.authenticate()
        assert session.get_adapter(GATEWAY + '/') is adapter
    assert destinations == [('vpn.seu.edu.cn', 443)]
    assert sni == ['vpn.seu.edu.cn']
    assert received == [('vpn.seu.edu.cn', None)]


def test_empty_gateway_ip_keeps_the_default_adapter():
    with requests.Session() as session:
        adapter = session.get_adapter(GATEWAY + '/')
        Client(session, {'SEU_VPN_IP': ''}, input, lambda _: None)
        assert session.get_adapter(GATEWAY + '/') is adapter


@pytest.mark.parametrize('failure', [
    requests.ConnectionError(socket.gaierror(socket.EAI_NONAME, 'SECRET DNS failure')),
    requests.ConnectionError(ProtocolError('closed', RemoteDisconnected('SECRET closed'))),
    requests.ConnectionError(ProtocolError('reset', ConnectionResetError('SECRET reset'))),
    requests.ConnectionError(ConnectionAbortedError('SECRET aborted')),
    requests.ConnectionError(ProtocolError('closed', BrokenPipeError('SECRET pipe closed'))),
    requests.exceptions.SSLError('[SSL: UNEXPECTED_EOF_WHILE_READING] SECRET'),
    requests.exceptions.SSLError(ssl.SSLEOFError('SECRET EOF')),
])
def test_initial_gateway_connection_failure_switches_only_gateway_once(monkeypatch, failure):
    with requests.Session() as session:
        session.trust_env = False
        original = session.get_adapter(GATEWAY + '/')
        adapters = []
        def send(request, **kwargs):
            assert kwargs['verify'] is True
            adapters.append(session.get_adapter(request.url))
            raise failure
        monkeypatch.setattr(session, 'send', send)
        client = Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(GATEWAY + '/public'))
        with pytest.raises(RemoteError):
            client.authenticate()
        assert len(adapters) == 2
        assert adapters[0] is original
        assert isinstance(adapters[1], GatewayIPAdapter)
        assert session.get_adapter(GATEWAY + ':443/') is adapters[1]
        assert session.get_adapter('https://auth.seu.edu.cn/') is original
        assert session.get_adapter('https://ehall.seu.edu.cn/') is original
        with pytest.raises(RemoteError):
            client.authenticate()
        assert adapters == [original, adapters[1], adapters[1]]


def test_total_deadline_interrupts_a_slow_drip_response():
    class SlowResponse:
        status_code = 200
        headers = {}
        raw = object()
        closed = False
        def iter_content(self, chunk_size):
            for _ in range(100):
                time.sleep(0.02)
                yield b'x'
        def close(self):
            self.closed = True
    class SlowSession:
        response = SlowResponse()
        def request(self, method, url, **kwargs):
            return self.response
    client = Client(SlowSession(), {}, input, lambda _: None)
    client.timeout = 0.05
    with pytest.raises(RemoteError, match='network_timeout'):
        client.request('GET', 'https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do')
    assert SlowSession.response.closed


class UnreadableBody:
    """A response body that fails the test when anyone asks for its bytes."""

    def __init__(self):
        self.read = False
        self.closed = False

    def read1(self, amt, decode_content=None):
        self.read = True
        raise AssertionError('error and redirect bodies must not be read')

    def stream(self, chunk_size, decode_content=True):
        self.read = True
        raise AssertionError('error and redirect bodies must not be streamed')

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


def response_with(status, body, headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response.raw = body
    return response


def client_returning(response):
    class Session:
        def request(self, method, url, **kwargs):
            return response
    return Client(Session(), {}, input, lambda _: None)


@pytest.mark.parametrize('status, reason, header, retry_after', [
    (429, 'server_backoff', '120', 120.0),
    (502, 'service_unavailable', '2', 2.0),
    (503, 'service_unavailable', '3', 3.0),
    (504, 'service_unavailable', None, 0.0),
])
def test_error_status_and_retry_after_are_decided_before_the_body(status, reason, header, retry_after):
    body = UnreadableBody()
    client = client_returning(response_with(status, body, {'Retry-After': header} if header else {}))
    client.timeout = 5
    with pytest.raises(RemoteError) as error:
        client.request('GET', 'https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do')
    assert str(error.value) == reason
    assert error.value.retry_after == retry_after
    assert error.value.http_status == status
    assert not body.read


@pytest.mark.parametrize('status', [301, 403, 500])
def test_non_200_body_is_never_consumed(status):
    body = UnreadableBody()
    client = client_returning(response_with(status, body))
    result = client.request('GET', 'https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do')
    assert result.status_code == status
    assert not body.read


class ChunkedBody:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.calls = []
        self.released = False

    def read1(self, amt, decode_content=None):
        self.calls.append((amt, decode_content))
        return self.chunks.pop(0) if self.chunks else b''

    def release_conn(self):
        self.released = True


def test_body_is_read_in_large_chunks_and_preserves_content():
    body = ChunkedBody([b'a' * 65536, b'b' * 65536, b'c' * 1000])
    client = client_returning(response_with(200, body))
    result = client.request('GET', 'https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do')
    assert result.content == b'a' * 65536 + b'b' * 65536 + b'c' * 1000
    # Compressed responses must be decoded like Requests' own body reads.
    assert body.calls == [(65536, True)] * 4
    assert body.released


class DeadlineSock:
    def __init__(self):
        self.timeouts = []

    def gettimeout(self):
        return 10.0

    def settimeout(self, value):
        self.timeouts.append(value)


class BlockingBody:
    def __init__(self, sock):
        self._fp = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=sock)))
        self.reads = 0
        self.closed = False

    def read1(self, amt, decode_content=None):
        self.reads += 1
        if self.reads == 1:
            return b'x'
        raise TimeoutError

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


def test_remaining_budget_bounds_a_blocking_body_read():
    sock = DeadlineSock()
    body = BlockingBody(sock)
    client = client_returning(response_with(200, body))
    client.timeout = 0.5
    with pytest.raises(RemoteError, match='network_timeout'):
        client.request('GET', 'https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do')
    assert body.reads == 2
    assert body.closed
    assert 0 < sock.timeouts[0] <= 0.5
    assert sock.timeouts[-1] == 10.0


@pytest.mark.parametrize('failure', [
    requests.ConnectTimeout('SECRET timeout'),
    requests.ReadTimeout('SECRET timeout'),
    requests.exceptions.ProxyError(socket.gaierror('SECRET proxy DNS')),
    requests.ConnectionError(ConnectionRefusedError('SECRET refused')),
    requests.ConnectionError('SECRET unknown failure'),
    requests.exceptions.SSLError('[SSL: CERTIFICATE_VERIFY_FAILED] SECRET'),
    requests.exceptions.SSLError('[SSL: WRONG_VERSION_NUMBER] SECRET'),
    requests.exceptions.SSLError('SECRET unknown TLS failure'),
    403, 429, 500,
])
def test_unrelated_failure_does_not_switch_gateway(monkeypatch, failure):
    with requests.Session() as session:
        session.trust_env = False
        original = session.get_adapter(GATEWAY + '/')
        calls = []
        def send(request, **kwargs):
            calls.append(request.url)
            if isinstance(failure, Exception):
                raise failure
            response = requests.Response()
            response.status_code = failure
            return response
        monkeypatch.setattr(session, 'send', send)
        client = Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(GATEWAY + '/public'))
        with pytest.raises(RemoteError):
            client.authenticate()
        assert len(calls) == 1
        assert session.get_adapter(GATEWAY + '/') is original


@pytest.mark.parametrize('origin, address, logged_in', [
    ('https://auth.seu.edu.cn', '127.0.0.1', False),
    ('https://ehall.seu.edu.cn', '127.0.0.1', False),
    (GATEWAY, '', False),
    (GATEWAY, '127.0.0.1', True),
])
def test_fallback_requires_gateway_backup_and_initial_authentication(monkeypatch, origin, address, logged_in):
    with requests.Session() as session:
        session.trust_env = False
        original = session.get_adapter(GATEWAY + '/')
        client = Client(session, {'SEU_VPN_IP': address}, input, lambda _: None)
        if logged_in:
            monkeypatch.setattr(client, '_authenticate', lambda: None)
            client.authenticate()
        calls = []
        def fail(request, **kwargs):
            calls.append(request.url)
            raise requests.ConnectionError(socket.gaierror('SECRET DNS failure'))
        monkeypatch.setattr(session, 'send', fail)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(origin + '/public'))
        with pytest.raises(RemoteError):
            client.authenticate()
        assert len(calls) == 1
        assert session.get_adapter(GATEWAY + '/') is original


class StubSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = requests.Response()
        response.url = url
        response.status_code = 200
        response._content = json.dumps(self.payload).encode()
        return response


def stub_client(payload):
    session = StubSession(payload)
    return Client(session, {}, input, lambda _: None), session


class CasSession:
    """Fake CAS responses for a direct Client.login() call without an SMS challenge."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = requests.Response()
        response.url = url
        response.status_code = 200
        if url.endswith('needCaptcha'):
            payload = {'code': 200}
        elif url.endswith('getChiperKey'):
            public = self.key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            payload = {'success': True, 'publicKey': base64.urlsafe_b64encode(public).decode()}
        else:
            payload = self.reply
        response._content = json.dumps(payload).encode()
        return response


def cas_client(**options):
    session = CasSession({'code': 200,
                          'redirectUrl': 'https%3A%2F%2Fehall.seu.edu.cn%2Fcallback'})
    client = Client(session, {'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
                    input, lambda _: None, **options)
    return client, session


def cas_login_data(session):
    return next(kwargs['json'] for method, url, kwargs in session.calls
                if url.endswith('casLogin'))


def test_login_payload_has_no_trust_fields_by_default(tmp_path):
    client, session = cas_client(fingerprint_dir=tmp_path)
    client.login('https://ehall.seu.edu.cn/service')
    data = cas_login_data(session)
    assert data['fingerPrint'] is None
    assert data['rememberTrustDevice'] is False
    assert data['rememberMe'] is False
    assert not (tmp_path / FINGERPRINT_FILE).exists()


def test_login_with_trusted_device_sends_and_reuses_the_persisted_fingerprint(tmp_path):
    client, session = cas_client(trust_device=True, fingerprint_dir=tmp_path)
    client.login('https://ehall.seu.edu.cn/service')
    data = cas_login_data(session)
    stored = (tmp_path / FINGERPRINT_FILE).read_text()
    assert re.fullmatch(r'[A-Za-z0-9_-]{8,128}', stored)
    assert data['rememberTrustDevice'] is True
    assert data['fingerPrint'] == stored
    assert data['rememberMe'] is False
    # A brand-new client (e.g. the next process start) must send the same device identity.
    other, other_session = cas_client(trust_device=True, fingerprint_dir=tmp_path)
    other.login('https://ehall.seu.edu.cn/service')
    assert cas_login_data(other_session)['fingerPrint'] == stored


def test_login_regenerates_a_corrupt_trusted_device_file(tmp_path):
    path = tmp_path / FINGERPRINT_FILE
    path.write_text('PRIVATE corrupt value')
    client, session = cas_client(trust_device=True, fingerprint_dir=tmp_path)
    client.login('https://ehall.seu.edu.cn/service')
    data = cas_login_data(session)
    assert re.fullmatch(r'[A-Za-z0-9_-]{8,128}', data['fingerPrint'])
    assert data['fingerPrint'] != 'PRIVATE corrupt value'
    assert path.read_text() == data['fingerPrint']


def test_device_fingerprint_is_none_when_disabled_or_unconfigured(tmp_path):
    session = StubSession({'code': 0})
    disabled = Client(session, {}, input, lambda _: None, fingerprint_dir=tmp_path)
    assert disabled.device_fingerprint() is None
    unconfigured = Client(session, {}, input, lambda _: None, trust_device=True)
    assert unconfigured.device_fingerprint() is None
    assert not (tmp_path / FINGERPRINT_FILE).exists()


def test_activities_joins_reservation_counts_and_server_clock():
    payload = {'code': 0, 'datas': {
        'hdlbList': [{'WID': 'one', 'HDZRS': '30'}, {'WID': 'two', 'HDZRS': 30}, {'WID': 'three'}],
        'hdrsList': [{'HD_WID': 'one', 'YYRS': 30}, {'HD_WID': 'two', 'YYRS': '29'},
                     {'HD_WID': 42, 'YYRS': 1}],
        'xtdqsj': '2030-01-01 08:00:00'}}
    client, session = stub_client(payload)
    rows = client.activities()
    assert [row.get('YYRS') for row in rows] == [30, '29', None]
    assert all(row['_SERVER_NOW'] == '2030-01-01 08:00:00' for row in rows)
    assert rows[0]['HDZRS'] == '30'
    method, url, kwargs = session.calls[0]
    assert method == 'POST' and kwargs['data'] == {}
    assert url.endswith('/modules/hdyy/queryActivityList.do')


@pytest.mark.parametrize('payload, reason', [
    ({'code': 0, 'datas': {'hdlbList': ['private row']}}, 'business_rows_invalid'),
    ({'code': 1, 'datas': {'hdlbList': []}}, 'business_response_invalid'),
    ({'code': 0, 'datas': {'hdlbList': [{'WID': 'one'}], 'total': 2}}, 'business_rows_incomplete'),
])
def test_activities_rejects_unusable_payloads(payload, reason):
    client, _ = stub_client(payload)
    with pytest.raises(RemoteError) as error:
        client.activities()
    assert str(error.value) == reason


def test_cancel_posts_activity_id_and_returns_raw_business_reply():
    client, session = stub_client({'code': 1, 'msg': 'PRIVATE server message'})
    assert client.cancel('activity-one') == {'code': 1, 'msg': 'PRIVATE server message'}
    method, url, kwargs = session.calls[0]
    assert method == 'POST' and kwargs['data'] == {'wid': 'activity-one'}
    assert url.endswith('/modules/hdyy/deleteReservation.do')


def test_request_uses_configurable_timeout():
    client, session = stub_client({'code': 0})
    assert client.timeout == 10.0
    client.request('GET', 'https://ehall.seu.edu.cn/modules/hdyy/vcode.do')
    client.timeout = 3
    client.request('GET', 'https://ehall.seu.edu.cn/modules/hdyy/vcode.do')
    assert [call[2]['timeout'] for call in session.calls] == [10.0, 3]
    assert all(call[2]['verify'] is True for call in session.calls)


@pytest.fixture
def body_server():
    gzip_body = gzip.compress(b'{"code":0,"data":"' + b'a' * 200 + b'"}')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path == '/gzip-slow':
                    self.send_response(200)
                    self.send_header('Content-Encoding', 'gzip')
                    self.send_header('Content-Length', str(len(gzip_body)))
                    self.end_headers()
                    for byte in gzip_body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.1)
                elif self.path == '/chunked-slow':
                    self.send_response(200)
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    for byte in b'10\r\n':
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.2)
                    self.wfile.write(b'0123456789abcdef\r\n0\r\n\r\n')
                    self.wfile.flush()
                else:
                    payload = b'{"code": 0}'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            except OSError:
                pass
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d' % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def read_body_with_budget(base, path, budget):
    with requests.Session() as session:
        client = Client(session, {}, input, lambda _: None)
        response = session.get(base + path, stream=True, timeout=5)
        started = time.monotonic()
        try:
            body = client._read_body(response, time.monotonic() + budget)
            return body, time.monotonic() - started, None
        except RemoteError as exc:
            return None, time.monotonic() - started, str(exc)


@pytest.mark.parametrize('path', ['/gzip-slow', '/chunked-slow'])
def test_total_budget_interrupts_internal_body_read_loops(body_server, path):
    body, elapsed, reason = read_body_with_budget(body_server, path, 0.2)
    assert reason == 'network_timeout'
    assert elapsed < 0.6


def test_fast_body_read_keeps_the_connection_reusable(body_server):
    with requests.Session() as session:
        client = Client(session, {}, input, lambda _: None)
        for _ in range(2):
            response = session.get(body_server + '/fast', stream=True, timeout=5)
            body = client._read_body(response, time.monotonic() + 5)
            assert body == b'{"code": 0}'
            response._content = body
            response._content_consumed = True
            response.close()


@pytest.mark.parametrize('gateway_server', ['vpn.seu.edu.cn'], indirect=True)
def test_total_budget_interrupts_a_slow_gzip_body_over_tls(gateway_server, monkeypatch):
    with requests.Session() as session:
        client = Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(GATEWAY + '/public'))
        client.authenticate()
        client.timeout = 0.2
        started = time.monotonic()
        with pytest.raises(RemoteError) as error:
            client.request('GET', GATEWAY + '/slow-gzip')
        elapsed = time.monotonic() - started
    assert str(error.value) == 'network_timeout'
    assert elapsed < 1.0


@pytest.mark.parametrize('gateway_server', ['vpn.seu.edu.cn'], indirect=True)
def test_total_budget_interrupts_slow_response_headers(gateway_server, monkeypatch):
    with requests.Session() as session:
        client = Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        assert client.gateway_adapter is not None
        session.mount(GATEWAY + '/', client.gateway_adapter)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(GATEWAY + '/public'))
        client.authenticate()
        client.timeout = 0.2
        started = time.monotonic()
        with pytest.raises(RemoteError) as error:
            client.request('GET', GATEWAY + '/slow-headers')
        elapsed = time.monotonic() - started
    assert str(error.value) == 'network_timeout'
    assert elapsed < 0.8


def test_install_deadline_pools_covers_proxy_managers():
    with requests.Session() as session:
        install_deadline_pools(session)
        adapter = session.get_adapter('https://ehall.seu.edu.cn/')
        manager = adapter.proxy_manager_for('http://127.0.0.1:9')
        assert manager.pool_classes_by_scheme == DEADLINE_POOLS


def test_deadline_socket_tightens_the_blocking_recv_timeout(monkeypatch):
    import seu_reserve.client as client_module
    class Raw:
        def __init__(self):
            self.timeouts = []
        def settimeout(self, value):
            self.timeouts.append(value)
        def recv_into(self, buffer, nbytes=0, flags=0):
            return 0
    raw = Raw()
    sock = client_module._DeadlineSocket(raw, time.monotonic() + 0.2)
    monkeypatch.setattr(client_module.select, 'select', lambda r, w, x, timeout: (r, [], []))
    sock.recv_into(bytearray(8))
    assert raw.timeouts
    assert 0 < raw.timeouts[-1] <= 0.21


@pytest.fixture
def slow_header_proxy():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                self.wfile.write(b'HTTP/1.1 200 OK\r\n')
                self.wfile.flush()
                for index in range(10):
                    time.sleep(0.1)
                    self.wfile.write(f'X-Slow-{index}: value\r\n'.encode())
                    self.wfile.flush()
                self.wfile.write(b'Content-Length: 2\r\n\r\nok')
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d' % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_proxy_header_deadline_is_enforced(slow_header_proxy):
    with requests.Session() as session:
        session.trust_env = False
        install_deadline_pools(session)
        started = time.monotonic()
        with pytest.raises(requests.RequestException):
            session.get('http://example.invalid/slow', proxies={'http': slow_header_proxy},
                        timeout=0.2)
        assert time.monotonic() - started < 0.8


@pytest.fixture(autouse=True)
def reset_request_connect():
    """Keep the connection-observation thread-local from leaking between tests."""
    import seu_reserve.client as client_module
    client_module.REQUEST_CONNECT.value = None
    yield
    client_module.REQUEST_CONNECT.value = None


def test_fake_session_reports_no_connection_and_counts_attempts():
    client, session = stub_client({'code': 0})
    assert client.requests_made == 0
    assert client.last_connect_seconds is None
    client.request('GET', 'https://ehall.seu.edu.cn/modules/hdyy/vcode.do')
    assert client.last_connect_seconds is None
    assert client.requests_made == 1
    # A connection cost measured for an earlier request must not survive a session that does
    # not go through urllib3 at all.
    client.last_connect_seconds = 1.5
    client.request('GET', 'https://ehall.seu.edu.cn/modules/hdyy/vcode.do')
    assert client.last_connect_seconds is None
    assert client.requests_made == 2
    assert len(session.calls) == 2


def test_request_reads_the_connect_cost_and_restores_the_thread_local():
    import seu_reserve.client as client_module

    class Session:
        def request(self, method, url, **kwargs):
            client_module.REQUEST_CONNECT.value = 0.25
            response = requests.Response()
            response.status_code = 200
            response._content = b'{}'
            return response

    client = Client(Session(), {}, input, lambda _: None)
    client_module.REQUEST_CONNECT.value = 'outer'
    client.request('GET', 'https://ehall.seu.edu.cn/modules/hdyy/vcode.do')
    assert client.last_connect_seconds == 0.25
    assert client.requests_made == 1
    assert getattr(client_module.REQUEST_CONNECT, 'value', None) == 'outer'


def test_failed_request_counts_the_attempt_and_restores_the_thread_local():
    import seu_reserve.client as client_module

    class Session:
        def request(self, method, url, **kwargs):
            raise requests.ConnectionError(socket.gaierror(socket.EAI_NONAME, 'SECRET DNS failure'))

    client = Client(Session(), {}, input, lambda _: None)
    client_module.REQUEST_CONNECT.value = 'outer'
    with pytest.raises(RemoteError, match='connection_failure'):
        client.request('GET', 'https://ehall.seu.edu.cn/modules/hdyy/vcode.do')
    assert client.requests_made == 1
    assert client.last_connect_seconds is None
    assert getattr(client_module.REQUEST_CONNECT, 'value', None) == 'outer'


def test_clone_readonly_has_independent_observability_counters():
    client, _ = stub_client({'code': 0})
    client.request('GET', 'https://ehall.seu.edu.cn/modules/hdyy/vcode.do')
    client.last_connect_seconds = 2.0
    clone = client.clone_readonly()
    assert client.requests_made == 1
    assert client.last_connect_seconds == 2.0
    assert clone.requests_made == 0
    assert clone.last_connect_seconds is None


def test_new_connection_duration_is_measured_for_http_and_https(monkeypatch):
    import seu_reserve.client as client_module

    def fake_connect(connection):
        # time.monotonic() is coarse on Windows (~15 ms), so a sleep shorter than that can
        # measure as 0.0.
        time.sleep(0.05)

    monkeypatch.setattr(client_module.Urllib3HTTPConnection, 'connect', fake_connect)
    monkeypatch.setattr(client_module.Urllib3HTTPSConnection, 'connect', fake_connect)
    for connection_cls in (client_module._DeadlineHTTPConnection,
                           client_module._DeadlineHTTPSConnection):
        connection = connection_cls('example.invalid', 443)
        client_module.REQUEST_CONNECT.value = None
        connection_cls.connect(connection)
        seconds = getattr(client_module.REQUEST_CONNECT, 'value', None)
        assert isinstance(seconds, float)
        assert seconds >= 0.02


def test_failed_new_connection_still_records_its_duration(monkeypatch):
    import seu_reserve.client as client_module

    def fail(connection):
        raise socket.gaierror(socket.EAI_NONAME, 'simulated DNS failure')

    monkeypatch.setattr(client_module.Urllib3HTTPConnection, 'connect', fail)
    connection = client_module._DeadlineHTTPConnection('example.invalid', 80)
    with pytest.raises(socket.gaierror):
        client_module._DeadlineHTTPConnection.connect(connection)
    seconds = getattr(client_module.REQUEST_CONNECT, 'value', None)
    assert isinstance(seconds, float)
    assert seconds >= 0


@pytest.mark.parametrize('gateway_server', ['vpn.seu.edu.cn'], indirect=True)
def test_real_tls_handshake_reaches_last_connect_seconds(gateway_server, monkeypatch):
    with requests.Session() as session:
        client = Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        monkeypatch.setattr(client, '_authenticate', lambda: client.get(GATEWAY + '/public'))
        client.authenticate()
        assert client.requests_made == 2
        assert isinstance(client.last_connect_seconds, float)
        assert 0 <= client.last_connect_seconds < 5
        client.request('GET', GATEWAY + ':443/public')
        assert client.requests_made == 3
        assert isinstance(client.last_connect_seconds, float)


TRUST_CREDENTIALS = {'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'}


class CampusTrustSession(requests.Session):
    """Campus-direct CAS/SPA flow with school cookies and a configurable verifyTgt reply.

    The entry page always redirects to the CAS SPA until the session is authenticated, so a
    persisted cookie alone is never treated as a completed login: only the explicit verifyTgt
    attempt (or the normal password/SMS login) can finish authentication.
    """

    service = 'http://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do'

    def __init__(self, *, verify=None, broken_trust_redirect=False):
        super().__init__()
        self.calls = []
        self.authenticated = False
        self.sso_active = False
        self.verify_reply = verify
        self.broken_trust_redirect = broken_trust_redirect
        self.verify_calls = 0
        self.cas_login_calls = 0
        self.login_redirects = 0
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = requests.Response()
        response.url = url
        response.status_code = 200
        response._content = b'{"code": 0}'
        path = urlsplit(url).path
        tail = path.rsplit('/', 1)[-1]
        if tail == 'index.do' and method == 'GET':
            if self.authenticated or self.sso_active:
                response._content = b'<script src="indexjsp.js"></script>'
            else:
                response.status_code = 302
                response.headers['Location'] = ('https://auth.seu.edu.cn/dist/#/login?'
                                                + urlencode({'service': self.service}))
        elif path.startswith('/dist/'):
            response._content = b'<html>CAS</html>'
        elif tail == 'verifyTgt' and method == 'POST':
            self.verify_calls += 1
            if isinstance(self.verify_reply, BaseException):
                raise self.verify_reply
            if self.verify_reply.get('code') == 200:
                self.sso_active = True
            response._content = json.dumps(self.verify_reply).encode()
        elif tail == 'needCaptcha':
            response._content = json.dumps({'code': 200}).encode()
        elif tail == 'getChiperKey':
            public = self.key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            response._content = json.dumps(
                {'success': True, 'publicKey': base64.urlsafe_b64encode(public).decode()}).encode()
        elif tail == 'casLogin' and method == 'POST':
            self.cas_login_calls += 1
            self.cookies.set('TGT', 'tgt-value', domain='auth.seu.edu.cn', path='/', secure=True)
            self.cookies.set('JSESSIONID', 'jsession-value', domain='ehall.seu.edu.cn', path='/',
                             expires=time.time() + 3600)
            self.cookies.set('unrelated', 'other-value', domain='example.com', path='/')
            response._content = json.dumps(
                {'code': 200, 'redirectUrl': quote(self.service, safe='')}).encode()
        elif tail == 'loginRedirect':
            self.login_redirects += 1
            if self.broken_trust_redirect and self.cas_login_calls == 0:
                # The trust attempt's redirect must not be accepted: it is not the app page.
                response._content = b'<html>not the application</html>'
            else:
                self.authenticated = True
                response.status_code = 302
                response.headers['Location'] = ENTRY + '?ticket=SYNTHETIC'
        return response


VERIFY_REDIRECT = {'success': 1, 'code': 201,
                   'redirectUrl': quote(CampusTrustSession.service, safe='')}
VERIFY_ALREADY = {'success': 1, 'code': 200}
VERIFY_REJECTED = {'success': 0, 'code': 500}


def school_trust_cookies():
    return [
        dict(name='TGT', value='tgt-value', domain='auth.seu.edu.cn', path='/',
             secure=True, expires=None),
        dict(name='JSESSIONID', value='jsession-value', domain='ehall.seu.edu.cn', path='/',
             secure=False, expires=time.time() + 3600),
    ]


def campus_client(directory, *, trust_device=True, verify=None, **session_options):
    session = CampusTrustSession(verify=verify, **session_options)
    client = Client(session, TRUST_CREDENTIALS, input, lambda _: None,
                    trust_device=trust_device, fingerprint_dir=directory)
    return client, session


def test_trusted_authenticate_persists_only_school_cookies(tmp_path):
    client, session = campus_client(tmp_path)
    client.authenticate()
    assert session.authenticated
    assert session.verify_calls == 0          # nothing persisted yet: normal login only
    assert session.cas_login_calls == 1
    stored = load_trust_cookies(tmp_path)
    assert [item['name'] for item in stored] == ['TGT', 'JSESSIONID']
    assert stored[0]['domain'] == 'auth.seu.edu.cn' and stored[0]['secure'] is True
    assert stored[0]['expires'] is None
    assert stored[1]['domain'] == 'ehall.seu.edu.cn' and stored[1]['secure'] is False
    assert stored[1]['expires'] > time.time()
    assert (tmp_path / TRUST_COOKIES_FILE).exists()


def test_trust_device_disabled_persists_and_loads_nothing(tmp_path):
    save_trust_cookies(tmp_path, school_trust_cookies())
    client, session = campus_client(tmp_path, trust_device=False)
    client.authenticate()
    assert session.authenticated
    assert session.verify_calls == 0
    # The file was not used and not rewritten with the fake session's unrelated cookie.
    assert [item['name'] for item in load_trust_cookies(tmp_path)] == ['TGT', 'JSESSIONID']


def test_persisted_cookies_are_loaded_into_a_fresh_session_and_would_be_sent(tmp_path):
    save_trust_cookies(tmp_path, school_trust_cookies())
    session = requests.Session()
    client = Client(session, TRUST_CREDENTIALS, input, lambda _: None,
                    trust_device=True, fingerprint_dir=tmp_path)
    seen = {}
    def capture() -> None:
        seen['cookies'] = [(cookie.name, cookie.value) for cookie in session.cookies]
    client._authenticate = capture
    client.authenticate()
    assert ('TGT', 'tgt-value') in seen['cookies']
    assert ('JSESSIONID', 'jsession-value') in seen['cookies']
    # The initial ENTRY request would carry the persisted CAS cookie header.
    prepared = session.prepare_request(
        requests.Request('GET', 'https://auth.seu.edu.cn/dist/'))
    assert 'TGT=tgt-value' in prepared.headers.get('Cookie', '')


def test_expired_persisted_cookie_is_not_loaded(tmp_path):
    save_trust_cookies(tmp_path, [dict(name='TGT', value='stale', domain='auth.seu.edu.cn',
                                       path='/', secure=True, expires=time.time() - 60)])
    client, session = campus_client(tmp_path, verify=VERIFY_REDIRECT)
    client.authenticate()
    assert session.verify_calls == 0          # an expired cookie must not trigger verifyTgt
    assert session.cas_login_calls == 1
    assert session.authenticated


def test_corrupt_cookie_file_falls_back_to_password_login(tmp_path):
    (tmp_path / TRUST_COOKIES_FILE).write_text('PRIVATE not json')
    client, session = campus_client(tmp_path, verify=VERIFY_REDIRECT)
    client.authenticate()
    assert session.verify_calls == 0
    assert session.cas_login_calls == 1
    assert session.authenticated


def test_unwritable_trust_state_directory_does_not_break_login(tmp_path):
    directory = tmp_path / 'not-a-directory'
    directory.write_text('file')
    client, session = campus_client(directory, verify=VERIFY_REDIRECT)
    client.authenticate()
    assert session.cas_login_calls == 1 and session.authenticated
    assert load_trust_cookies(directory) == []


def test_verify_tgt_201_redirect_completes_login_without_password_or_sms(tmp_path):
    save_trust_cookies(tmp_path, school_trust_cookies())
    client, session = campus_client(tmp_path, verify=VERIFY_REDIRECT)
    client.authenticate()
    assert session.verify_calls == 1
    assert session.cas_login_calls == 0
    assert session.authenticated
    verify = next(kwargs['json'] for method, url, kwargs in session.calls
                  if url.endswith('verifyTgt'))
    assert verify == {'service': CampusTrustSession.service, 'loginType': 'account'}


def test_verify_tgt_200_rechecks_the_entry_page(tmp_path):
    save_trust_cookies(tmp_path, school_trust_cookies())
    client, session = campus_client(tmp_path, verify=VERIFY_ALREADY)
    client.authenticate()
    assert session.verify_calls == 1
    assert session.cas_login_calls == 0
    assert session.sso_active


@pytest.mark.parametrize('verify', [VERIFY_REJECTED, RemoteError('service_unavailable')])
def test_verify_tgt_failure_or_error_falls_back_to_password_login(tmp_path, verify):
    save_trust_cookies(tmp_path, school_trust_cookies())
    client, session = campus_client(tmp_path, verify=verify)
    client.authenticate()
    assert session.verify_calls == 1
    assert session.cas_login_calls == 1
    assert session.authenticated


def test_verify_tgt_non_app_redirect_falls_back_to_password_login(tmp_path):
    save_trust_cookies(tmp_path, school_trust_cookies())
    client, session = campus_client(tmp_path, verify=VERIFY_REDIRECT,
                                    broken_trust_redirect=True)
    client.authenticate()
    assert session.verify_calls == 1
    assert session.login_redirects >= 2       # trust redirect rejected, normal login redirect used
    assert session.cas_login_calls == 1
    assert session.authenticated


@pytest.mark.parametrize('trust_device, cookie_file', [
    (False, 'valid'), (True, 'missing'), (True, 'expired'),
])
def test_verify_tgt_runs_only_when_persisted_cookies_were_loaded(tmp_path, trust_device,
                                                                 cookie_file):
    if cookie_file == 'valid':
        save_trust_cookies(tmp_path, school_trust_cookies())
    elif cookie_file == 'expired':
        save_trust_cookies(tmp_path, [dict(name='TGT', value='stale', domain='auth.seu.edu.cn',
                                           path='/', secure=True, expires=time.time() - 60)])
    client, session = campus_client(tmp_path, trust_device=trust_device, verify=VERIFY_REDIRECT)
    client.authenticate()
    assert session.verify_calls == 0
    assert session.cas_login_calls == 1
    assert session.authenticated
