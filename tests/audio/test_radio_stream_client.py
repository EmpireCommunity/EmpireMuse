"""O cliente de saída do relay (item 8, R3, R-8): ICY 200, limites de cabeçalho e prazo, e nunca um endereço que não seja público.

Os servidores são de verdade, em loopback, numa porta da faixa permitida (8000-8999); como o loopback não é público, os testes de caminho
feliz trocam o ``public`` por um que aceita. Os testes de SSRF usam o ``public`` PADRÃO e nunca chegam a conectar."""

import asyncio
import socket
import ssl
from contextlib import asynccontextmanager

import pytest

from empiremuse.utils.radio import stream_client as sc
from empiremuse.utils.radio.stream_client import StreamRefused, open_stream, redirect_target

pytestmark = pytest.mark.critical


def free_port() -> int:
    for port in range(8300, 8999):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("nenhuma porta livre na faixa 8000-8999")


@asynccontextmanager
async def server(handler):
    port = free_port()
    srv = await asyncio.start_server(handler, "127.0.0.1", port)
    try:
        yield port
    finally:
        srv.close()
        await srv.wait_closed()


def replying(data: bytes, *, then=b""):
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(data + then)
            await writer.drain()
        finally:
            writer.close()

    return handle


async def resolves(host, port):
    return ["127.0.0.1"]


async def _must_not_resolve(host, port):
    raise AssertionError("não deveria resolver nada")


async def _resolve_public(host, port):
    return ["93.184.216.34"]


ALLOW_LOOPBACK = {"resolve": resolves, "public": lambda address: True}


class TestOpening:
    @pytest.mark.asyncio
    async def test_a_shoutcast_icy_200_status_line_is_accepted_which_aiohttp_refuses(self):
        head = b"ICY 200 OK\r\nicy-name:Estacao\r\nicy-metaint:8192\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with server(replying(head, then=b"\xff\xfb\x90\x00")) as port:
            response = await open_stream(f"http://radio.example:{port}/live", **ALLOW_LOOPBACK)

            assert response.status == 200 and response.headers["icy-metaint"] == "8192"
            assert await response.reader.readexactly(4) == b"\xff\xfb\x90\x00"
            await response.close()

    @pytest.mark.asyncio
    async def test_http_1_0_and_1_1_status_lines(self):
        for version in (b"HTTP/1.0", b"HTTP/1.1"):
            async with server(replying(version + b" 200 OK\r\nContent-Type: audio/aac\r\n\r\n")) as port:
                response = await open_stream(f"http://radio.example:{port}/x", **ALLOW_LOOPBACK)
                assert response.status == 200 and response.headers["content-type"] == "audio/aac"
                await response.close()

    @pytest.mark.asyncio
    async def test_the_request_is_http_1_0_with_fixed_headers_and_the_icy_ask(self):
        seen = {}

        async def handle(reader, writer):
            seen["head"] = (await reader.readuntil(b"\r\n\r\n")).decode()
            writer.write(b"HTTP/1.0 200 OK\r\n\r\n")
            await writer.drain()
            writer.close()

        async with server(handle) as port:
            response = await open_stream(f"http://radio.example:{port}/a/b?x=1", **ALLOW_LOOPBACK)
            await response.close()

        head = seen["head"]
        assert head.startswith("GET /a/b?x=1 HTTP/1.0\r\n")
        assert f"Host: radio.example:{port}" in head and "Icy-MetaData: 1" in head
        assert "Accept-Encoding: identity" in head and "Connection: close" in head
        assert "Cookie" not in head and "Authorization" not in head

    @pytest.mark.asyncio
    async def test_a_redirect_is_returned_not_followed(self):
        async with server(replying(b"HTTP/1.0 302 Found\r\nLocation: http://other.example:8000/live\r\n\r\n")) as port:
            url = f"http://radio.example:{port}/x"
            response = await open_stream(url, **ALLOW_LOOPBACK)

            assert response.status == 302 and redirect_target(url, response) == "http://other.example:8000/live"
            await response.close()


class TestLimits:
    @pytest.mark.asyncio
    async def test_a_huge_header_block_is_refused(self):
        async with server(replying(b"HTTP/1.0 200 OK\r\nX-Big: " + b"a" * 40_000 + b"\r\n\r\n")) as port:
            with pytest.raises(StreamRefused):
                await open_stream(f"http://radio.example:{port}/x", **ALLOW_LOOPBACK)

    @pytest.mark.asyncio
    async def test_too_many_header_lines_are_refused(self):
        lines = b"".join(f"X-{n}: y\r\n".encode() for n in range(150))
        async with server(replying(b"HTTP/1.0 200 OK\r\n" + lines + b"\r\n")) as port:
            with pytest.raises(StreamRefused) as refusal:
                await open_stream(f"http://radio.example:{port}/x", **ALLOW_LOOPBACK)

            assert refusal.value.reason == "response"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_line", [b"SPAM 200 OK", b"HTTP/2 200", b"ICY OK", b"200", b""])
    async def test_a_bad_status_line_is_refused(self, status_line):
        async with server(replying(status_line + b"\r\n\r\n")) as port:
            with pytest.raises(StreamRefused):
                await open_stream(f"http://radio.example:{port}/x", **ALLOW_LOOPBACK)

    @pytest.mark.asyncio
    async def test_a_compressed_body_is_refused(self):
        async with server(replying(b"HTTP/1.0 200 OK\r\nContent-Encoding: gzip\r\n\r\n")) as port:
            with pytest.raises(StreamRefused):
                await open_stream(f"http://radio.example:{port}/x", **ALLOW_LOOPBACK)

    @pytest.mark.asyncio
    async def test_a_server_that_never_finishes_the_headers_times_out(self):
        async def slow(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.0 200 OK\r\n")  # nunca termina
            await writer.drain()
            await asyncio.sleep(5)
            writer.close()

        async with server(slow) as port:
            with pytest.raises(StreamRefused) as refusal:
                await open_stream(f"http://radio.example:{port}/x", header_timeout=0.2, **ALLOW_LOOPBACK)

            assert refusal.value.reason in ("timeout", "response")

    @pytest.mark.asyncio
    async def test_a_server_that_closes_without_answering_is_refused(self):
        async with server(replying(b"")) as port:
            with pytest.raises(StreamRefused):
                await open_stream(f"http://radio.example:{port}/x", **ALLOW_LOOPBACK)

    @pytest.mark.asyncio
    async def test_a_closed_port_is_refused_not_raised_raw(self):
        port = free_port()

        with pytest.raises(StreamRefused) as refusal:
            await open_stream(f"http://radio.example:{port}/x", **ALLOW_LOOPBACK)

        assert refusal.value.reason == "connect"


class TestSsrfDefenses:
    """R-8: com o ``public`` PADRÃO, nada que aponte para dentro chega a abrir socket."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "[::1]",
            "10.0.0.1",
            "192.168.1.1",
            "172.16.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "[::ffff:127.0.0.1]",
            "[64:ff9b::7f00:1]",
            "[::ffff:10.0.0.1]",
            "[2002:7f00:1::]",
            "[fd00::1]",
        ],
    )
    async def test_an_ip_literal_that_is_not_public_is_refused_before_any_connection(self, host):
        with pytest.raises(StreamRefused) as refusal:
            await open_stream(f"http://{host}:8000/live", resolve=_must_not_resolve)

        assert refusal.value.reason == "address"

    @pytest.mark.asyncio
    async def test_a_name_that_resolves_inside_is_refused(self):
        async def to_loopback(host, port):
            return ["127.0.0.1"]

        with pytest.raises(StreamRefused) as refusal:
            await open_stream("http://radio.example:8000/live", resolve=to_loopback)

        assert refusal.value.reason == "address"

    @pytest.mark.asyncio
    async def test_dns_rebinding_one_public_and_one_private_answer_refuses_the_whole_name(self):
        async def mixed(host, port):
            return ["93.184.216.34", "10.0.0.1"]

        with pytest.raises(StreamRefused) as refusal:
            await open_stream("http://radio.example:8000/live", resolve=mixed)

        assert refusal.value.reason == "address"

    @pytest.mark.asyncio
    async def test_a_name_that_resolves_to_nothing_is_refused(self):
        async def empty(host, port):
            return []

        with pytest.raises(StreamRefused):
            await open_stream("http://radio.example:8000/live", resolve=empty)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "http://user:pass@radio.example:8000/x",
            "http://radio.example:2333/x",
            "http://radio.example:27017/",
            "http://radio.example:22/",
            "ftp://radio.example/x",
            "file:///etc/passwd",
            "http://radio.example:8000/x y",
            "",
            None,
        ],
    )
    async def test_an_unsafe_url_is_refused_before_resolving_anything(self, url):
        with pytest.raises(StreamRefused) as refusal:
            await open_stream(url, resolve=_must_not_resolve)

        assert refusal.value.reason == "url"


class TestRedirectTarget:
    @staticmethod
    def fake(status, location):
        return sc.StreamResponse(status, {"location": location} if location else {}, None, None, "h")  # type: ignore[arg-type]

    def test_only_redirect_statuses_with_a_safe_destination(self):
        base = "http://a.example:8000/live"

        assert redirect_target(base, self.fake(301, "/other")) == "http://a.example:8000/other"
        assert redirect_target(base, self.fake(307, "http://b.example/s")) == "http://b.example/s"
        assert redirect_target(base, self.fake(200, "http://b.example/s")) is None
        assert redirect_target(base, self.fake(302, None)) is None

    @pytest.mark.parametrize(
        "location",
        [
            "http://radio.example:2333/",
            "http://radio.example:27017/",
            "file:///etc/passwd",
            "ftp://x.example/",
            "http://u:p@x.example/",
            "http://x.example:6379/",
            "x" * 3000,
        ],
    )
    def test_a_destination_that_fails_the_url_rules_is_dropped(self, location):
        assert redirect_target("http://a.example:8000/live", self.fake(302, location)) is None


class TestTlsIsChecked:
    @pytest.mark.asyncio
    async def test_https_connects_to_the_checked_ip_with_the_name_for_sni_and_a_verifying_context(self, monkeypatch):
        captured = {}

        async def fake_open(address, port, **kwargs):
            captured.update(address=address, port=port, **kwargs)
            raise ssl.SSLError("não precisa conectar")

        monkeypatch.setattr(sc.asyncio, "open_connection", fake_open)

        with pytest.raises(StreamRefused) as refusal:
            await open_stream("https://radio.example/live", resolve=_resolve_public)

        assert refusal.value.reason == "tls"
        assert captured["address"] == "93.184.216.34" and captured["server_hostname"] == "radio.example"
        context = captured["ssl"]
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
