"""Offline tests may use local servers, never an external school connection."""
import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def local_network_only(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            assert host == 'localhost' or ipaddress.ip_address(host).is_loopback, 'offline test blocked external connection'
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
