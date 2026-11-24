"""Regra pura da lista de hosts das capas (``COVER_HOSTS``): host exato, ``*.sufixo`` de um rótulo e ``*`` = qualquer host."""

import pytest

from empiremuse.rules.cover import DEFAULT_COVER_HOSTS, host_allowed, normalize_cover_hosts, parse_cover_hosts

pytestmark = pytest.mark.critical

DEFAULT = parse_cover_hosts(DEFAULT_COVER_HOSTS)


@pytest.mark.parametrize(
    "host",
    ["i.ytimg.com", "img.youtube.com", "i1.sndcdn.com", "i.scdn.co", "e-cdns-images.dzcdn.net", "is1-ssl.mzstatic.com",
     "f4.bcbits.com", "static-cdn.jtvnw.net", "i.vimeocdn.com", "I.SCDN.CO", "i.scdn.co."],
)  # fmt: skip
def test_the_default_list_accepts_the_known_cdns(host):
    assert host_allowed(DEFAULT, host)


@pytest.mark.parametrize(
    "host",
    ["evil.example", "sndcdn.com", "a.b.sndcdn.com", "i.scdn.co.evil.example", "xi.scdn.co", "127.0.0.1", "::1", "",
     "ytimg.com"],
)  # fmt: skip
def test_the_default_list_refuses_everything_else(host):
    assert not host_allowed(DEFAULT, host)


def test_any_host_mode_accepts_public_names_and_literals_the_ip_filter_decides():
    rule = parse_cover_hosts("*")

    assert host_allowed(rule, "evil.example") and host_allowed(rule, "1.1.1.1") and not host_allowed(rule, "")


@pytest.mark.parametrize(
    "value",
    ["", "   ", "*.com", "* i.ytimg.com", "**.x.com", "*x.com", "i.ytimg.com/x", "https://i.ytimg.com", "1.2.3.4",
     "a..b.com", "-a.com", "a_b.com", "localhost", "i.ytimg.com:443", "x." + "a" * 64 + ".com", None, 5],
)  # fmt: skip
def test_invalid_values_raise(value):
    with pytest.raises(ValueError):
        parse_cover_hosts(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, normalize_cover_hosts(DEFAULT_COVER_HOSTS)), ("  ", normalize_cover_hosts(DEFAULT_COVER_HOSTS)),
     (" * ", "*"), ("I.SCDN.CO  *.DZCDN.NET", "i.scdn.co *.dzcdn.net")],
)  # fmt: skip
def test_normalize_uses_the_default_when_empty_and_lowercases(value, expected):
    assert normalize_cover_hosts(value) == expected
