import datetime
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from urllib3.util import connection

from seu_reserve.client import Client, GATEWAY, RemoteError


@pytest.fixture
def gateway_server(tmp_path, monkeypatch, request):
    hostname = request.param
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
        if address == ('127.0.0.1', 443):
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
def test_gateway_ip_bypasses_dns_preserving_tls_origin_and_cookies(gateway_server):
    received, sni, destinations = gateway_server
    with requests.Session() as session:
        client = Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        for origin in (GATEWAY, GATEWAY + ':443'):
            response = client.request('GET', origin + '/public')
            assert response.status_code == 200
            assert response.url == origin + '/public'
        assert all(cookie.domain == 'vpn.seu.edu.cn' for cookie in session.cookies)
    assert destinations == [('127.0.0.1', 443)] * 2
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
        with pytest.raises(RemoteError) as error:
            client.request('GET', GATEWAY + '/public')
    assert error.value.tls_reason == 'certificate_verify_failed'
    assert received == []
    assert sni == ['vpn.seu.edu.cn']
    assert destinations == [('127.0.0.1', 443)]


def test_gateway_ip_only_changes_the_gateway_adapter():
    with requests.Session() as session:
        other_adapter = session.get_adapter('https://auth.seu.edu.cn/')
        Client(session, {'SEU_VPN_IP': '127.0.0.1'}, input, lambda _: None)
        assert session.get_adapter('https://auth.seu.edu.cn/') is other_adapter
        assert session.get_adapter('https://ehall.seu.edu.cn/') is other_adapter
        assert session.get_adapter(GATEWAY + '/') is not other_adapter


def test_empty_gateway_ip_keeps_the_default_adapter():
    with requests.Session() as session:
        adapter = session.get_adapter(GATEWAY + '/')
        Client(session, {'SEU_VPN_IP': ''}, input, lambda _: None)
        assert session.get_adapter(GATEWAY + '/') is adapter
