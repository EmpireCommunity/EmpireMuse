"""T-407-F1: decisão de confiança sem consultar DNS e URLs válidas para literais IPv6."""

import socket

import pytest

pytestmark = pytest.mark.critical


@pytest.mark.parametrize(
    "host,allowed",
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("[::1]", True),
        ("::ffff:127.0.0.1", True),
        ("LOCALHOST", True),
        ("localhost.", False),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("203.0.113.1", False),
        ("lavalink", False),
        ("localhost.example.com", False),
        ("alias.local", False),
        ("[localhost]", False),
        ("[127.0.0.1]", False),
        (8090, False),
    ],
)
def test_loopback_gate_never_uses_dns(monkeypatch, host, allowed):
    from empiremuse.rules.lavalink import is_loopback_host

    def forbidden_resolver(*args, **kwargs):
        pytest.fail("a decisão de confiança não pode consultar DNS")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_resolver)

    assert is_loopback_host(host) is allowed


@pytest.mark.parametrize(
    "host,secure,expected",
    [
        ("::1", False, "http://[::1]:8090"),
        ("[::1]", True, "https://[::1]:8090"),
        ("::ffff:127.0.0.1", False, "http://[::ffff:127.0.0.1]:8090"),
        ("127.0.0.2", False, "http://127.0.0.2:8090"),
        ("lavalink", True, "https://lavalink:8090"),
    ],
)
def test_node_rest_uri(host, secure, expected):
    from empiremuse.rules.lavalink import node_rest_uri

    assert node_rest_uri(host, 8090, secure) == expected
