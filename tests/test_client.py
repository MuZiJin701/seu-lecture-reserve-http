import datetime
import json
import socket
import ssl
import threading
import time
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from urllib3.util import connection
from urllib3.exceptions import ProtocolError

from seu_reserve.client import Client, GATEWAY, GatewayIPAdapter, RemoteError


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
