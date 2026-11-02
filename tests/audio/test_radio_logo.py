"""O logo da estação no painel (item 8, Alto): logo via proxy do próprio painel, formatos raster, re-codificado. O ``favicon`` é uma URL cadastrada por qualquer
pessoa: aqui se prova que o servidor só a busca nas condições certas (memória, uma por vez, limitada), só com endereços públicos fixados, sem redirecionamento, com
corpo contado, e que o decodificador roda num processo filho com limites e devolve um PNG pequeno e limpo. O transporte roda contra um servidor local de verdade."""

import asyncio
import io
import socket
import struct
import zlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.radio import logo as logo_module
from empiremuse.utils.radio.directory import Directory
from empiremuse.utils.radio.logo import (
    LogoError,
    LogoFetcher,
    LogoResult,
    LogoService,
    decode_in_worker,
    detect_format,
    logo_url,
)

pytestmark = pytest.mark.critical

UUID = "960e57c5-0601-11e8-ae97-52543be04c81"
UUID_2 = "11111111-2222-3333-4444-555555555555"
LOGO_URL = "https://cdn.example/logo.png"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_bytes(width=2, height=2, *, extra_chunk=b"") -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))
    header = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + header + extra_chunk + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


def text_chunk(text=b"segredo=tok") -> bytes:
    return _chunk(b"tEXt", b"Comment\x00" + text)


def bomb_png(width: int, height: int) -> bytes:
    """Um PNG minúsculo que declara ``width``x``height`` e comprime todo o resto em zeros."""
    compressor = zlib.compressobj(9)
    row = b"\x00" * (1 + width * 4)
    data = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    header = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + header + _chunk(b"IDAT", data) + _chunk(b"IEND", b"")


def ico_with_inner_png(inner: bytes, directory_side=16) -> bytes:
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", directory_side, directory_side, 0, 0, 1, 32, len(inner), 22)
    return header + entry + inner


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Source:
    """O diretório de mentira: lembra a URL de alguns uuids e responde ``learn`` como mandarem."""

    def __init__(self, known=None, *, learned=None, fail=False):
        self.known = dict(known or {})
        self.learned = learned
        self.fail = fail
        self.learn_calls = 0

    def favicon_of(self, uuid):
        return self.known.get(uuid)

    async def learn(self, uuid):
        self.learn_calls += 1
        if self.fail:
            raise RuntimeError("diretório fora")
        if self.learned:
            self.known[uuid] = self.learned


class Fetch:
    def __init__(self, body=b"", error=None, gate=None):
        self.body, self.error, self.gate = body, error, gate
        self.urls: list[str] = []

    async def __call__(self, url):
        self.urls.append(url)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.body


class Decode:
    def __init__(self, out=None, error=None):
        self.out, self.error = out if out is not None else png_bytes(), error
        self.calls: list[tuple[str, int]] = []
        self.running = 0
        self.max_running = 0

    async def __call__(self, fmt, data):
        self.calls.append((fmt, len(data)))
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        await asyncio.sleep(0)
        self.running -= 1
        if self.error is not None:
            raise self.error
        return self.out


def service(source=None, fetch=None, decode=None, clock=None):
    clock = clock or Clock()
    svc = LogoService(
        source or Source({UUID: LOGO_URL}), fetch=fetch or Fetch(png_bytes()), decode=decode or Decode(), clock=clock
    )
    return svc, clock


def many(count):
    uuids = [f"00000000-0000-0000-0000-{n:012d}" for n in range(count)]
    return uuids, Source({u: f"https://cdn{n}.example/l.png" for n, u in enumerate(uuids)})


class TestFormat:
    @pytest.mark.parametrize(
        ("head", "expected"),
        [
            (b"\x89PNG\r\n\x1a\n....", "PNG"),
            (b"\xff\xd8\xff\xe0....", "JPEG"),
            (b"GIF89a....", "GIF"),
            (b"GIF87a....", "GIF"),
            (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "WEBP"),
            (b"\x00\x00\x01\x00\x01\x00\x10\x10", "ICO"),
        ],
    )
    def test_the_five_raster_formats_by_signature(self, head, expected):
        assert detect_format(head) == expected

    @pytest.mark.parametrize(
        "head",
        [
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            b"<?xml version='1.0'?><svg/>",
            b"%!PS-Adobe-3.0 EPSF-3.0",
            b"8BPS\x00\x01",
            b"<!doctype html><script>alert(1)</script>",
            b"\x00\x00\x01\x00\x00\x00",  # ICO sem imagem
            b"RIFF\x10\x00\x00\x00WAVEfmt ",
            b"",
            b"\x89PNG",
        ],
    )
    def test_svg_eps_psd_html_and_anything_else_are_refused(self, head):
        assert detect_format(head) is None


class TestUrl:
    def test_only_https_on_port_443_without_credentials(self):
        assert logo_url("https://cdn.example/logo.png") is not None
        assert logo_url("https://cdn.example:443/logo.png") is not None
        for bad in (
            "http://cdn.example/logo.png",
            "https://cdn.example:8443/logo.png",
            "https://user:pass@cdn.example/logo.png",
            "https://cdn.example/" + "a" * 3000,
            "ftp://cdn.example/x",
            "https://",
            "",
            None,
            5,
        ):
            assert logo_url(bad) is None


# ---------------------------------------------------------------------- o transporte (servidor local de verdade)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def http_server(raw: bytes):
    port = free_port()
    seen: list[bytes] = []

    async def handle(reader, writer):
        try:
            seen.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(raw)
            await writer.drain()
        except ConnectionError, asyncio.IncompleteReadError:
            pass  # o cliente largou no meio (corpo grande demais): é o que se espera
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        yield port, seen
    finally:
        server.close()
        await server.wait_closed()


def response(body: bytes, *, status="200 OK", extra="", length=True) -> bytes:
    head = f"HTTP/1.1 {status}\r\nContent-Type: image/png\r\n{extra}"
    if length:
        head += f"Content-Length: {len(body)}\r\n"
    return head.encode() + b"Connection: close\r\n\r\n" + body


async def _loopback(host, port):
    return ["127.0.0.1"]


def fetcher_for(port, **kwargs):
    kwargs.setdefault("resolve", _loopback)
    kwargs.setdefault("public", lambda a: True)
    return LogoFetcher(scheme="http", port=port, **kwargs)


class TestTransport:
    @pytest.mark.asyncio
    async def test_it_returns_the_body_and_sends_no_referer_cookie_or_compression(self):
        async with http_server(response(png_bytes())) as (port, seen):
            body = await fetcher_for(port)(LOGO_URL)

        request = seen[0].decode().lower()
        assert body == png_bytes()
        assert "accept-encoding: identity" in request and "referer" not in request and "cookie" not in request
        assert "user-agent: empiremuse/" in request

    @pytest.mark.asyncio
    async def test_a_redirect_is_refused_after_one_request(self):
        redirect = response(b"", status="302 Found", extra="Location: http://127.0.0.1:2333/x\r\n")
        async with http_server(redirect) as (port, seen):
            with pytest.raises(LogoError) as raised:
                await fetcher_for(port)(LOGO_URL)

        assert raised.value.code == "redirect" and len(seen) == 1

    @pytest.mark.asyncio
    async def test_not_200_gzip_and_a_declared_size_over_256_kib_are_refused(self):
        cases = {
            "status": response(b"nada", status="404 Not Found"),
            "encoding": response(b"x", extra="Content-Encoding: gzip\r\n"),
            "size": response(b"x" * 10).replace(b"Content-Length: 10", b"Content-Length: 999999"),
        }
        for code, raw in cases.items():
            async with http_server(raw) as (port, _):
                with pytest.raises(LogoError) as raised:
                    await fetcher_for(port)(LOGO_URL)
            assert raised.value.code == code

    @pytest.mark.asyncio
    async def test_a_body_over_the_limit_is_cut_without_a_content_length_or_chunked(self):
        huge = b"\x89PNG\r\n\x1a\n" + b"0" * (300 * 1024)
        chunked = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            + f"{len(huge):x}".encode()
            + b"\r\n"
            + huge
            + b"\r\n0\r\n\r\n"
        )
        for raw in (response(huge, length=False), chunked):
            async with http_server(raw) as (port, _):
                with pytest.raises(LogoError) as raised:
                    await fetcher_for(port)(LOGO_URL)
            assert raised.value.code == "size"

    @pytest.mark.asyncio
    async def test_any_private_loopback_or_mixed_address_is_refused_before_connecting(self):
        async with http_server(response(png_bytes())) as (port, seen):
            for answer in (["127.0.0.1"], ["10.0.0.5"], ["8.8.8.8", "127.0.0.1"], ["::1"], ["169.254.169.254"], []):

                async def resolve(host, p, answer=answer):
                    return answer

                with pytest.raises(LogoError) as raised:
                    await LogoFetcher(resolve=resolve, scheme="http", port=port)(LOGO_URL)
                assert raised.value.code == "address"
        assert seen == []  # nenhuma conexão

    @pytest.mark.asyncio
    async def test_an_ip_literal_in_the_url_never_connects(self):
        for literal in (
            "https://127.0.0.1/logo.png",
            "https://[::ffff:127.0.0.1]/logo.png",
            "https://10.1.2.3/logo.png",
        ):
            with pytest.raises(LogoError) as raised:
                await LogoFetcher()(literal)
            assert raised.value.code in ("address", "url")

    @pytest.mark.asyncio
    async def test_a_dns_failure_is_an_address_error(self):
        async def broken(host, port):
            raise OSError("sem DNS")

        with pytest.raises(LogoError) as raised:
            await LogoFetcher(resolve=broken)(LOGO_URL)

        assert raised.value.code == "address"

    @pytest.mark.asyncio
    async def test_a_url_that_does_not_pass_the_rule_is_refused_without_resolving(self):
        calls = []

        async def resolve(host, port):
            calls.append(host)
            return ["8.8.8.8"]

        for bad in ("http://cdn.example/x.png", "https://cdn.example:8443/x.png", "https://u@cdn.example/x"):
            with pytest.raises(LogoError) as raised:
                await LogoFetcher(resolve=resolve)(bad)
            assert raised.value.code == "url"
        assert calls == []


# ---------------------------------------------------------------------- o serviço


class TestServiceMemoryAndLimits:
    @pytest.mark.asyncio
    async def test_a_known_station_returns_the_png_and_is_cached(self):
        fetch, decode = Fetch(png_bytes()), Decode()
        svc, _ = service(fetch=fetch, decode=decode)

        first = await svc.png(UUID)
        second = await svc.png(UUID)

        assert first.png == decode.out and second == first and first.transient is False
        assert fetch.urls == [LOGO_URL] and len(decode.calls) == 1 and decode.calls[0][0] == "PNG"

    @pytest.mark.asyncio
    async def test_an_unknown_or_invalid_uuid_costs_no_network_at_all(self):
        source, fetch = Source(), Fetch(png_bytes())
        svc, _ = service(source=source, fetch=fetch)

        for uuid in (UUID_2, "nao-e-uuid", "", None, 5, "../x", UUID + "x", UUID.upper()):
            assert await svc.png(uuid) == LogoResult(None, False)

        assert fetch.urls == [] and source.learn_calls == 0

    @pytest.mark.asyncio
    async def test_a_configured_station_after_a_restart_does_one_lookup_then_the_logo(self):
        source = Source(learned=LOGO_URL)
        svc, _ = service(source=source)

        result = await svc.png(UUID, configured=True)

        assert result.png is not None and source.learn_calls == 1

    @pytest.mark.asyncio
    async def test_a_lookup_that_fails_is_transient_and_not_remembered_as_a_bad_logo(self):
        source = Source(fail=True)
        svc, clock = service(source=source)

        first = await svc.png(UUID, configured=True)
        source.fail, source.learned = False, LOGO_URL
        clock.now += 2.0  # uma ficha do limite de saída
        second = await svc.png(UUID, configured=True)

        assert first == LogoResult(None, True) and second.png is not None

    @pytest.mark.asyncio
    async def test_a_station_without_a_logo_is_remembered_for_an_hour(self):
        source = Source()  # o diretório responde, mas a estação não tem favicon
        svc, clock = service(source=source)

        assert await svc.png(UUID, configured=True) == LogoResult(None, False)
        await svc.png(UUID, configured=True)
        assert source.learn_calls == 1
        clock.now += 3601
        await svc.png(UUID, configured=True)
        assert source.learn_calls == 2

    @pytest.mark.asyncio
    async def test_25_requests_for_the_same_uuid_make_one_fetch(self):
        gate = asyncio.Event()
        fetch = Fetch(png_bytes(), gate=gate)
        svc, _ = service(fetch=fetch)

        tasks = [asyncio.ensure_future(svc.png(UUID)) for _ in range(25)]
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks)

        assert len(fetch.urls) == 1 and all(r.png == results[0].png and r.png is not None for r in results)

    @pytest.mark.asyncio
    async def test_at_most_two_fetches_at_once_the_third_answers_default_at_once_without_queueing(self):
        gate = asyncio.Event()
        uuids, source = many(3)
        fetch = Fetch(png_bytes(), gate=gate)
        svc, _ = service(source=source, fetch=fetch)

        running = [asyncio.ensure_future(svc.png(u)) for u in uuids[:2]]
        for _ in range(5):
            await asyncio.sleep(0)
        third = await svc.png(uuids[2])
        gate.set()
        await asyncio.gather(*running)

        assert third == LogoResult(None, True) and len(fetch.urls) == 2

    @pytest.mark.asyncio
    async def test_about_one_fetch_per_second_after_the_burst(self):
        uuids, source = many(4)
        svc, clock = service(source=source, fetch=Fetch(png_bytes()))

        first = [await svc.png(u) for u in uuids[:3]]  # a rajada é 2: a 3ª não tem ficha
        clock.now += 1.0
        later = await svc.png(uuids[2])

        assert [r.transient for r in first] == [False, False, True]
        assert later.png is not None and later.transient is False

    @pytest.mark.asyncio
    async def test_the_cache_has_a_byte_ceiling_and_an_entry_ceiling(self, monkeypatch):
        monkeypatch.setattr(logo_module, "CACHE_ENTRIES", 3)
        uuids, source = many(6)
        svc, clock = service(source=source)
        for u in uuids:
            await svc.png(u)
            clock.now += 2.0

        assert len(svc._hits) <= 3
        assert svc._hits.maxsize == logo_module.CACHE_BYTES


class TestServiceFailures:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10",
            b"8BPS\x00\x01" + b"\x00" * 100,
            b"<!doctype html><html></html>",
            b"",
        ],
    )
    async def test_svg_eps_psd_html_or_empty_become_the_default_icon_and_never_reach_the_decoder(self, body):
        decode = Decode()
        svc, _ = service(fetch=Fetch(body), decode=decode)

        result = await svc.png(UUID)

        assert result == LogoResult(None, False) and decode.calls == []

    @pytest.mark.asyncio
    async def test_a_failure_is_cached_for_an_hour_then_tried_again(self):
        fetch = Fetch(png_bytes(), error=LogoError("status"))
        svc, clock = service(fetch=fetch)

        assert await svc.png(UUID) == LogoResult(None, False)
        await svc.png(UUID)
        assert len(fetch.urls) == 1
        clock.now += 3601
        fetch.error = None
        assert (await svc.png(UUID)).png is not None and len(fetch.urls) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("where", ["fetch", "decode"])
    @pytest.mark.parametrize("error", [LogoError("decode"), LogoError("deadline"), TimeoutError(), OSError("rede")])
    async def test_a_dead_worker_a_deadline_or_a_network_error_are_the_default_icon_plus_a_negative_cache(
        self, where, error
    ):
        fetch = Fetch(png_bytes(), error=error if where == "fetch" else None)
        decode = Decode(error=error if where == "decode" else None)
        svc, _ = service(fetch=fetch, decode=decode)

        assert await svc.png(UUID) == LogoResult(None, False)
        await svc.png(UUID)

        assert len(fetch.urls) == 1

    @pytest.mark.asyncio
    async def test_an_unexpected_exception_never_reaches_the_caller(self):
        class Boom(Source):
            armed = False

            def favicon_of(self, uuid):
                if self.armed:
                    raise RuntimeError("segredo")
                self.armed = True
                return LOGO_URL

        svc, _ = service(source=Boom())

        assert await svc.png(UUID) == LogoResult(None, False)

    @pytest.mark.asyncio
    async def test_the_log_carries_only_the_reason_code_never_the_url(self, caplog):
        secret = "https://cdn.example/logo.png?token=SEGREDO123"
        svc, _ = service(source=Source({UUID: secret}), fetch=Fetch(error=LogoError("redirect")))

        with caplog.at_level("DEBUG"):
            await svc.png(UUID)

        assert "redirect" in caplog.text and "SEGREDO123" not in caplog.text and "cdn.example" not in caplog.text

    @pytest.mark.asyncio
    async def test_the_decoder_runs_one_at_a_time(self):
        uuids, source = many(2)
        decode = Decode()
        svc, _ = service(source=source, decode=decode)

        await asyncio.gather(*(svc.png(u) for u in uuids))

        assert decode.max_running == 1 and len(decode.calls) == 2


# ---------------------------------------------------------------------- o diretório lembra só o que passou pelas regras


class TestDirectoryMemory:
    @staticmethod
    def directory(payload):
        class Fetcher:
            async def __call__(self, mirror, path, query):
                return payload

        async def mirrors():
            return ["de1.api.radio-browser.info"]

        return Directory(fetcher=Fetcher(), mirrors=mirrors)

    @staticmethod
    def station(uuid, favicon):
        return {
            "stationuuid": uuid,
            "name": "Boa FM",
            "url_resolved": "http://stream.example:8000/live",
            "codec": "MP3",
            "bitrate": 128,
            "hls": 0,
            "country": "Brasil",
            "favicon": favicon,
        }

    @pytest.mark.asyncio
    async def test_a_search_remembers_only_https_clean_logos(self):
        third = "33333333-3333-3333-3333-333333333333"
        d = self.directory(
            [
                self.station(UUID, "https://cdn.example/logo.png"),
                self.station(UUID_2, "http://cdn.example/logo.png"),
                self.station(third, "javascript:alert(1)"),
            ]
        )

        await d.search("rock")

        assert d.favicon_of(UUID) == "https://cdn.example/logo.png"
        assert d.favicon_of(UUID_2) is None and d.favicon_of(third) is None

    @pytest.mark.asyncio
    async def test_by_uuid_remembers_the_logo_and_logo_png_never_raises_for_the_route(self):
        d = self.directory([self.station(UUID, "https://cdn.example/logo.png")])
        await d.by_uuid(UUID)
        d.logos._fetch = Fetch(png_bytes())
        d.logos._decode = Decode()

        known = await d.logo_png(UUID)
        unknown = await d.logo_png(UUID_2)
        invalid = await d.logo_png("../etc/passwd")

        assert known.png is not None and unknown == LogoResult() and invalid == LogoResult()


# ---------------------------------------------------------------------- o processo filho de verdade (Pillow)


class TestWorker:
    @pytest.mark.asyncio
    async def test_a_big_png_comes_back_as_a_png_of_at_most_128_pixels_without_text(self):
        from PIL import Image

        out = await decode_in_worker("PNG", png_bytes(500, 300, extra_chunk=text_chunk()))

        assert out.startswith(b"\x89PNG\r\n\x1a\n") and b"tEXt" not in out and b"segredo" not in out
        with Image.open(io.BytesIO(out), formats=["PNG"]) as image:
            assert image.format == "PNG" and image.mode == "RGBA" and max(image.size) == 128
            assert not image.info.get("Comment") and "icc_profile" not in image.info

    @pytest.mark.asyncio
    async def test_a_small_png_keeps_its_size(self):
        out = await decode_in_worker("PNG", png_bytes(16, 16))

        assert struct.unpack(">II", out[16:24]) == (16, 16)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("side", [9000, 1100])  # 81 MP e um logo acima do teto de 1 MP
    async def test_a_decompression_bomb_is_refused(self, side):
        with pytest.raises(LogoError):
            await decode_in_worker("PNG", bomb_png(side, side))

    @pytest.mark.asyncio
    async def test_an_ico_with_a_huge_inner_png_is_refused(self):
        with pytest.raises(LogoError):
            await decode_in_worker("ICO", ico_with_inner_png(bomb_png(4000, 4000)))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fmt", "body"),
        [
            ("JPEG", png_bytes()),
            ("GIF", png_bytes()),
            ("PNG", b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10"),
            ("PNG", b"8BPS" + b"\x00" * 200),
            ("PNG", b"\x89PNG\r\n\x1a\n" + b"\x00" * 50),
        ],
    )
    async def test_the_worker_opens_only_the_detected_format_and_garbage_fails(self, fmt, body):
        with pytest.raises(LogoError):
            await decode_in_worker(fmt, body)

    @pytest.mark.asyncio
    async def test_an_unknown_format_and_an_empty_input_are_refused(self):
        for fmt, body in (("SVG", b"<svg/>"), ("PNG", b"")):
            with pytest.raises(LogoError):
                await decode_in_worker(fmt, body)

    @pytest.mark.asyncio
    async def test_a_worker_that_overruns_the_deadline_is_killed(self, monkeypatch, tmp_path):
        slow = tmp_path / "lento.py"
        slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        monkeypatch.setattr(logo_module, "WORKER_TIMEOUT_S", 0.3)
        monkeypatch.setattr(logo_module, "_WORKER", slow)

        with pytest.raises(LogoError) as raised:
            await decode_in_worker("PNG", png_bytes())

        assert raised.value.code == "deadline"

    @pytest.mark.asyncio
    async def test_the_worker_starts_isolated_and_without_the_parents_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TOKEN_SECRETO", "SEGREDO123")
        probe = tmp_path / "sonda.py"
        # só devolve um PNG válido se estiver em modo isolado (-I) e sem o segredo do pai no ambiente; senão sai com erro
        probe.write_text(
            "import os, sys\n"
            "sys.exit(9) if ('TOKEN_SECRETO' in os.environ or not sys.flags.isolated) else None\n"
            f"sys.stdout.buffer.write(bytes.fromhex({png_bytes().hex()!r}))\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logo_module, "_WORKER", probe)

        out = await decode_in_worker("PNG", png_bytes())

        assert out == png_bytes()

    @pytest.mark.asyncio
    async def test_output_that_is_not_a_small_png_is_refused_by_the_parent(self, monkeypatch, tmp_path):
        for name, payload in (
            ("grande.py", png_bytes(500, 500)),
            ("texto.py", b"nao sou png"),
            ("enorme.py", b"\x89PNG\r\n\x1a\n" + b"0" * 200_000),
        ):
            script = tmp_path / name
            script.write_text(
                f"import sys\nsys.stdout.buffer.write(bytes.fromhex({payload.hex()!r}))\n", encoding="utf-8"
            )
            monkeypatch.setattr(logo_module, "_WORKER", script)

            with pytest.raises(LogoError):
                await decode_in_worker("PNG", png_bytes())


class TestSharedFlightCancel:
    """Revisão do R4: o cancelamento de quem busca o logo não pode virar cancelamento de quem só esperava o mesmo voo."""

    @pytest.mark.asyncio
    async def test_a_waiter_gets_no_logo_when_the_owner_of_the_flight_is_cancelled(self):
        gate = asyncio.Event()
        source = SimpleNamespace(favicon_of=lambda uuid: LOGO_URL, learn=AsyncMock())
        service = LogoService(source)

        async def hang(uuid, configured):
            await gate.wait()
            return LogoResult(b"x")

        service._load = hang
        owner = asyncio.ensure_future(service.png(UUID))
        await asyncio.sleep(0)
        waiter = asyncio.ensure_future(service.png(UUID))
        await asyncio.sleep(0)

        owner.cancel()
        result = await asyncio.wait_for(waiter, 1)

        assert result == LogoResult()
