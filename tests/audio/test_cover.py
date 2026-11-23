"""A capa da faixa nas skins de imagem (Alto): a URL é de terceiro. Prova que a busca só sai para host da lista e endereço
público fixado (uma resolução só), sem redirecionamento, com corpo contado; que a decodificação roda num filho isolado que
devolve só pixels de tamanho fixo; que o serviço limita a saída, divide voos, cacheia por bytes e nunca loga a URL.

Transporte contra um servidor local de verdade; resolvedor, relógio e processo filho falsos onde o tempo importaria (sem ``sleep``).
"""

import asyncio
import contextlib
import functools
import io
import logging

import pytest
import yarl
from PIL import Image

from empiremuse.rules.cover import COVER_RAW_BYTES, DEFAULT_COVER_HOSTS, parse_cover_hosts
from empiremuse.utils.music import cover_fetch as fetch_module
from empiremuse.utils.music import covers as covers_module
from empiremuse.utils.music.cover_fetch import CoverError, CoverFetcher, cover_url
from empiremuse.utils.music.covers import CoverResult, CoverService, decode_in_worker
from empiremuse.utils.server_playlists_fetch import PinnedResolver
from tests.audio.test_radio_logo import free_port, http_server, response

pytestmark = pytest.mark.critical

DEFAULT = parse_cover_hosts(DEFAULT_COVER_HOSTS)
ANY = parse_cover_hosts("*")
COVER = "https://i.scdn.co/image/ab67616d0000b273?token=segredo"
PIXELS = b"\x07" * COVER_RAW_BYTES


@functools.cache
def flat(fmt: str, size: tuple[int, int]) -> bytes:
    """Uma imagem VÁLIDA e compressível (poucos KB) de ``size``: sem o teto de pixels o filho a decodificaria."""
    buffer = io.BytesIO()
    Image.new("L", size, 128).save(buffer, format=fmt)
    return buffer.getvalue()


def encoded(fmt: str, size=(300, 200), *, palette_alpha=False) -> bytes:
    image = Image.new("RGB", size, (10, 20, 30))
    image.paste((200, 100, 50), (50, 0, 250, 200))  # o miolo quadrado tem outra cor que as bordas
    buffer = io.BytesIO()
    if palette_alpha:  # PNG de paleta com transparência em bytes (tRNS)
        image.convert("P", palette=Image.Palette.ADAPTIVE).save(buffer, format=fmt, transparency=b"\x00\x80")
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


class Resolver:
    def __init__(self, *answers, error=None, hang=False):
        self.answers, self.error, self.hang, self.calls = list(answers), error, hang, []

    async def __call__(self, host, port):
        self.calls.append(host)
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return self.answers[min(len(self.calls), len(self.answers)) - 1]


@pytest.fixture
def no_connection(monkeypatch):
    """Qualquer tentativa de abrir sessão HTTP falha o teste: prova que a recusa veio antes da rede."""

    def refuse(*args, **kwargs):
        raise AssertionError("abriu conexão")

    monkeypatch.setattr(fetch_module.aiohttp, "ClientSession", refuse)


def local(port, *, resolve=None, hosts=ANY, public=lambda a: True):
    return CoverFetcher(hosts, resolve=resolve or Resolver(["127.0.0.1"]), public=public, scheme="http", port=port)


# ---------------------------------------------------------------------- SK-1, SK-2, SK-9: URL, hosts e endereços


@pytest.mark.parametrize(
    "raw",
    [
        "http://i.scdn.co/a.jpg",
        "https://user:pw@i.scdn.co/a.jpg",
        "https://i.scdn.co:8080/a.jpg",
        "https://127.0.0.1:2333/r/tok",
        "ftp://i.scdn.co/a",
        "https:///a",
        "https://i.scdn.co/" + "a" * 2048,
        "https://i.scdn.co/" + "é" * 2000,  # cabe no texto, mas o ``yarl`` codifica para ~12 mil caracteres
        "",
        None,
        5,
    ],
)
def test_only_https_443_without_credentials_and_bounded_urls_are_fetchable(raw):
    assert cover_url(raw) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "https://127.0.0.1/a.jpg",
        "https://[::1]/a.jpg",
        "https://[::ffff:127.0.0.1]/a.jpg",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/a.jpg",
        "https://100.64.0.1/a.jpg",
        "https://127.0.0.1/r/token-do-relay",
    ],
)
async def test_internal_ip_literals_never_connect_even_with_any_host(raw, no_connection):
    resolver = Resolver(["8.8.8.8"])
    with pytest.raises(CoverError) as raised:
        await CoverFetcher(ANY, resolve=resolver)(cover_url(raw))

    assert raised.value.code == "address" and resolver.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolver",
    [
        Resolver(["8.8.8.8", "127.0.0.1"]),
        Resolver(["10.0.0.5"]),
        Resolver(["::1"]),
        Resolver([]),
        Resolver(error=OSError("sem DNS")),
        Resolver(hang=True),
    ],
)
async def test_a_name_that_is_not_only_public_or_does_not_resolve_is_refused_before_connecting(resolver, no_connection):
    fetcher = CoverFetcher(DEFAULT, resolve=resolver, resolve_timeout=0 if resolver.hang else 5)

    with pytest.raises(CoverError) as raised:
        await fetcher(cover_url(COVER))

    assert raised.value.code == "address" and resolver.calls == ([] if resolver.hang else ["i.scdn.co"])


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["evil.example", "scdn.co", "a.b.sndcdn.com", "i.scdn.co.evil.example", "sndcdn.com"])
async def test_a_host_outside_the_list_never_resolves_nor_connects(host, no_connection):
    resolver = Resolver(["8.8.8.8"])
    with pytest.raises(CoverError) as raised:
        await CoverFetcher(DEFAULT, resolve=resolver)(cover_url(f"https://{host}/a.jpg"))

    assert raised.value.code == "host" and resolver.calls == []


@pytest.mark.asyncio
async def test_any_host_mode_still_applies_the_ip_filter(no_connection):
    resolver = Resolver(["192.168.0.10"])
    with pytest.raises(CoverError) as raised:
        await CoverFetcher(ANY, resolve=resolver)(cover_url("https://evil.example/a.jpg"))

    assert raised.value.code == "address" and resolver.calls == ["evil.example"]


@pytest.mark.asyncio
async def test_one_resolution_only_and_the_connection_uses_the_checked_address():
    rebinding = Resolver(["127.0.0.1"], ["10.9.9.9"])  # a 2ª resposta nunca é pedida
    async with http_server(response(encoded("JPEG"))) as (port, seen):
        body = await local(port, resolve=rebinding)(cover_url(COVER))

    assert body == encoded("JPEG") and rebinding.calls == ["i.scdn.co"] and len(seen) == 1
    with pytest.raises(OSError):
        await PinnedResolver("i.scdn.co", ["127.0.0.1"]).resolve("outro.example", 443)


@pytest.mark.asyncio
async def test_a_redirect_to_loopback_is_not_followed():
    redirect = response(b"", status="302 Found", extra="Location: http://127.0.0.1:2333/x\r\n")
    async with http_server(redirect) as (port, seen):
        with pytest.raises(CoverError) as raised:
            await local(port)(cover_url(COVER))

    assert raised.value.code == "redirect" and len(seen) == 1


# ---------------------------------------------------------------------- SK-3: transporte


@pytest.mark.asyncio
async def test_the_session_ignores_proxies_and_cookies_never_decompresses_and_sends_no_credential(monkeypatch):
    import aiohttp

    options = {}
    real = aiohttp.ClientSession

    def spy(*args, **kwargs):
        options.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(fetch_module.aiohttp, "ClientSession", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    async with http_server(response(encoded("PNG"))) as (port, seen):
        await local(port)(cover_url(COVER))

    request = seen[0].decode().lower()
    assert options["trust_env"] is False and options["auto_decompress"] is False
    assert isinstance(options["cookie_jar"], aiohttp.DummyCookieJar)
    assert (options["timeout"].total, options["timeout"].connect) == (5.0, 3.0)
    assert "accept-encoding: identity" in request and "cookie" not in request and "authorization" not in request


BIG = b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024)
CHUNKED = (
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    + f"{len(BIG):x}".encode()
    + b"\r\n"
    + BIG
    + b"\r\n0\r\n\r\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (response(b"x", status="404 Not Found"), "status"),
        (response(b"x", status="500 Internal Server Error"), "status"),
        (response(b"x", extra="Content-Encoding: gzip\r\n"), "encoding"),
        (response(b"x" * 10).replace(b"Content-Length: 10", b"Content-Length: 99999999"), "size"),
        (response(BIG, length=False), "size"),
        (CHUNKED, "size"),
    ],
)
async def test_bad_status_compression_and_bodies_over_2_mib_are_refused(raw, code):
    async with http_server(raw) as (port, _):
        with pytest.raises(CoverError) as raised:
            await local(port)(cover_url(COVER))

    assert raised.value.code == code


# ---------------------------------------------------------------------- SK-4: decodificação isolada


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "body"),
    [
        ("JPEG", encoded("JPEG")),
        ("PNG", encoded("PNG")),
        ("WEBP", encoded("WEBP")),
        ("PNG", encoded("PNG", palette_alpha=True)),
    ],
    ids=["jpeg", "png", "webp", "png-palette-trns"],
)
async def test_the_worker_returns_fixed_size_raw_rgb_with_the_center_crop(fmt, body):
    out = await decode_in_worker(fmt, body)

    image = Image.frombytes("RGB", (240, 240), out)
    r, g, b = image.getpixel((120, 120))
    assert len(out) == COVER_RAW_BYTES and r > 150 and b < 100, "o miolo do quadrado é a cor do centro"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "body"),
    [
        ("PNG", flat("PNG", (2100, 2100))),  # entre 1× e 2× o teto: no Pillow é só aviso
        ("PNG", flat("PNG", (1, 4_500_000))),
        ("JPEG", flat("JPEG", (3000, 3000))),
        ("JPEG", b"\xff\xd8\xff" + b"\x00" * 300),
        ("JPEG", encoded("PNG")),
        ("GIF", encoded("GIF")),
        ("PNG", b""),
    ],
)
async def test_bombs_garbage_and_formats_outside_the_list_fail_in_the_worker(fmt, body):
    with pytest.raises(CoverError) as raised:
        await decode_in_worker(fmt, body)

    assert raised.value.code == "decode"


@pytest.mark.asyncio
async def test_a_cover_at_the_pixel_ceiling_still_decodes():
    assert len(await decode_in_worker("PNG", flat("PNG", (2048, 2048)))) == COVER_RAW_BYTES


class FakeProcess:
    def __init__(self, out=b"", code=0, hang=False):
        self._out, self.hang, self.returncode, self._code = out, hang, None, code
        self.pid = 4242
        self.killed = False
        self.stdin = self
        self.stdout = self

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    async def read(self, n):
        if self.hang:
            await asyncio.Event().wait()
        out, self._out = self._out[:n], self._out[n:]
        return out

    async def wait(self):
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._code
        return self.returncode

    def kill(self):
        self.killed = True


def spawner(process):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    return spawn, calls


@pytest.mark.asyncio
async def test_the_worker_starts_isolated_without_environment():
    process = FakeProcess(PIXELS)
    spawn, calls = spawner(process)

    assert await decode_in_worker("JPEG", b"\xff\xd8\xff", spawn=spawn) == PIXELS
    args, kwargs = calls[0]
    assert args[1] == "-I" and args[-1] == "JPEG" and args[2].endswith("cover_worker.py")
    assert kwargs["env"] == {} and kwargs["cwd"] == "/" and kwargs["close_fds"] is True
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_a_worker_past_the_deadline_is_killed_with_its_group_and_reaped(monkeypatch):
    process = FakeProcess(hang=True)
    spawn, _ = spawner(process)
    groups = []

    def killpg(pid, sig):
        groups.append(pid)
        process.kill()

    monkeypatch.setattr(covers_module.os, "killpg", killpg)

    with pytest.raises(CoverError) as raised:
        await decode_in_worker("PNG", b"x", spawn=spawn, timeout_s=0)

    assert raised.value.code == "deadline" and groups == [4242] and process.returncode == -9


@pytest.mark.asyncio
@pytest.mark.parametrize("process", [FakeProcess(PIXELS[:-1]), FakeProcess(PIXELS + b"x"), FakeProcess(PIXELS, code=1)])
async def test_worker_output_of_the_wrong_size_or_a_failed_exit_is_refused(process):
    spawn, _ = spawner(process)
    with pytest.raises(CoverError):
        await decode_in_worker("PNG", b"x", spawn=spawn)


# ---------------------------------------------------------------------- o serviço: SK-3, SK-5, SK-6, SK-8, SK-10


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Fetch:
    def __init__(self, body=None, error=None, gate=None):
        self.body = body if body is not None else encoded("JPEG")
        self.error, self.gate, self.urls = error, gate, []

    async def __call__(self, url):
        assert isinstance(url, yarl.URL)
        self.urls.append(str(url))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.body


class Decode:
    def __init__(self, out=PIXELS, error=None):
        self.out, self.error, self.calls = out, error, []

    async def __call__(self, fmt, data):
        self.calls.append(fmt)
        if self.error is not None:
            raise self.error
        return self.out


def service(fetch=None, decode=None, hosts=DEFAULT, clock=None):
    return CoverService(hosts, fetch=fetch or Fetch(), decode=decode or Decode(), clock=clock or Clock())


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)  # só cede a vez ao laço; não depende de tempo


@pytest.mark.asyncio
async def test_a_cover_is_fetched_once_decoded_and_cached_by_the_exact_url():
    fetch = Fetch()
    clock = Clock()
    svc = service(fetch, clock=clock)

    first, again = await svc.pixels(COVER), await svc.pixels(COVER)
    clock.now += 1
    other = await svc.pixels(COVER.replace("segredo", "outro"))

    assert first == again == other == CoverResult(PIXELS)
    assert fetch.urls == [COVER, COVER.replace("segredo", "outro")], "query diferente = outra busca"


@pytest.mark.asyncio
async def test_a_private_call_or_radio_mode_without_cover_costs_no_network_nor_cache():
    fetch = Fetch()
    svc = service(fetch)

    assert await svc.pixels(None) == CoverResult() and fetch.urls == [] and svc.cached_bytes == 0


@pytest.mark.asyncio
async def test_ten_requests_for_the_same_url_make_one_fetch():
    gate = asyncio.Event()
    fetch = Fetch(gate=gate)
    svc = service(fetch)
    tasks = [asyncio.create_task(svc.pixels(COVER)) for _ in range(10)]
    await settle()
    gate.set()

    assert all(r.pixels == PIXELS for r in await asyncio.gather(*tasks)) and len(fetch.urls) == 1


@pytest.mark.asyncio
async def test_a_hung_fetch_answers_with_the_logo_in_time_and_the_cover_arrives_later():
    gate = asyncio.Event()
    svc = service(Fetch(gate=gate))

    assert await svc.pixels(COVER, wait_s=0) == CoverResult(pending=True)
    gate.set()
    await settle()
    assert await svc.pixels(COVER, wait_s=0) == CoverResult(PIXELS)


@pytest.mark.asyncio
async def test_at_most_two_fetches_at_once_the_third_gets_the_logo_without_queueing():
    gate = asyncio.Event()
    fetch = Fetch(gate=gate)
    clock = Clock()
    svc = service(fetch, clock=clock)

    first = [await svc.pixels(f"https://i.scdn.co/{n}.jpg", wait_s=0) for n in range(2)]
    clock.now += 10  # o balde enche: o que segura o 3º é só o teto de buscas simultâneas
    third = await svc.pixels("https://i.scdn.co/2.jpg", wait_s=0)
    await settle()

    assert all(r.pending for r in first) and third == CoverResult(pending=True) and len(fetch.urls) == 2
    gate.set()
    await settle()
    assert (await svc.pixels("https://i.scdn.co/2.jpg")).pixels == PIXELS, "a vaga de saída voltou"


@pytest.mark.asyncio
async def test_an_empty_bucket_fetches_nothing_new_until_it_refills():
    fetch = Fetch()
    clock = Clock()
    svc = service(fetch, clock=clock)

    burst = [await svc.pixels(f"https://i.scdn.co/{n}.jpg") for n in range(3)]

    assert [r.pixels is not None for r in burst] == [True, True, False] and burst[2].pending and len(fetch.urls) == 2
    clock.now += 1
    assert (await svc.pixels("https://i.scdn.co/2.jpg")).pixels == PIXELS and len(fetch.urls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [b"<svg/>", b"II*\x00tiff", b"8BPS psd", encoded("GIF"), b"\x00\x00\x01\x00\x01\x00ico"]
)
async def test_formats_outside_jpeg_png_webp_never_reach_the_worker(body):
    decode = Decode()
    svc = service(Fetch(body), decode)

    assert await svc.pixels(COVER) == CoverResult() and decode.calls == []


@pytest.mark.asyncio
async def test_the_parent_never_opens_outside_bytes_with_pillow(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("o pai abriu bytes de fora")

    monkeypatch.setattr(Image, "open", refuse)
    svc = CoverService(DEFAULT, fetch=Fetch(encoded("JPEG")), clock=Clock())  # o filho de verdade

    assert len((await svc.pixels(COVER)).pixels or b"") == COVER_RAW_BYTES


@pytest.mark.asyncio
async def test_the_cover_cache_never_passes_its_byte_ceiling_and_a_huge_entry_does_not_raise(monkeypatch):
    clock = Clock()
    svc = service(clock=clock)
    for n in range(60):
        clock.now += 1
        await svc.pixels(f"https://i.scdn.co/{n}.jpg")
        assert svc.cached_bytes <= covers_module.COVER_CACHE_BYTES

    monkeypatch.setattr(covers_module, "COVER_CACHE_BYTES", 1000)
    tiny = service()
    assert await tiny.pixels(COVER) == CoverResult(PIXELS) and tiny.cached_bytes == 0


@pytest.mark.asyncio
async def test_an_unfetchable_url_is_logged_once_per_hour(caplog):
    svc = service()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert await svc.pixels("http://i.scdn.co/c.jpg") == CoverResult()

    assert [r.getMessage() for r in caplog.records] == ["Capa sem imagem (url)"]


@pytest.mark.asyncio
async def test_a_failure_is_remembered_for_an_hour_then_tried_again():
    fetch = Fetch(error=CoverError("status"))
    clock = Clock()
    svc = service(fetch, clock=clock)

    await svc.pixels(COVER)
    await svc.pixels(COVER)
    clock.now += 3601
    await svc.pixels(COVER)

    assert len(fetch.urls) == 2


OUTSIDE = "https://evil.example/c.jpg?token=segredo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "fetch", "decode", "code"),
    [
        ("http://i.scdn.co/c.jpg?token=segredo", None, None, "url"),
        (OUTSIDE, None, None, "host"),
        (COVER, Fetch(error=CoverError("address")), None, "address"),
        (COVER, Fetch(error=CoverError("redirect")), None, "redirect"),
        (COVER, Fetch(error=CoverError("status")), None, "status"),
        (COVER, Fetch(error=CoverError("size")), None, "size"),
        (COVER, Fetch(error=CoverError("encoding")), None, "encoding"),
        (COVER, Fetch(b"<svg/>"), None, "format"),
        (COVER, None, Decode(error=CoverError("decode")), "decode"),
        (COVER, Fetch(error=TimeoutError()), None, "deadline"),
        (COVER, Fetch(error=OSError(f"falhou em {COVER}")), None, "network"),
        (COVER, Fetch(error=RuntimeError(f"inesperado {COVER}")), None, "internal"),
    ],
)
async def test_the_log_carries_only_the_reason_code_never_the_url(caplog, raw, fetch, decode, code):
    with caplog.at_level(logging.DEBUG):
        result = await service(fetch, decode).pixels(raw)

    text = " ".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    assert result.pixels is None and text == f"Capa sem imagem ({code}) "
    assert not any(r.exc_info for r in caplog.records)
    assert "segredo" not in text and "http" not in text and "scdn" not in text


@pytest.mark.asyncio
async def test_the_outer_deadline_bounds_a_fetch_that_never_ends(monkeypatch):
    monkeypatch.setattr(covers_module, "COVER_TOTAL_TIMEOUT_S", -1)  # prazo externo = 0: vence na hora, sem esperar
    svc = service(Fetch(gate=asyncio.Event()))

    assert await svc.pixels(COVER) == CoverResult()


@pytest.mark.asyncio
async def test_cache_keys_stay_32_bytes_whatever_the_url_and_raw_invalid_text_is_never_kept():
    clock = Clock()
    svc = service(Fetch(error=CoverError("status")), clock=clock)
    for n in range(20):
        clock.now += 1
        await svc.pixels(f"https://evil.example/{n}/" + "😀" * 500)  # fora da lista
        await svc.pixels(f"http://i.scdn.co/{n}/" + "é" * 4000)  # inválida
        await svc.pixels(f"https://i.scdn.co/{n}?" + "é" * 600)  # aceita e falha na busca

    keys = list(svc._misses.keys())
    assert len(keys) == 60 and all(isinstance(k, bytes) and len(k) == 32 for k in keys)


@pytest.mark.asyncio
async def test_a_cancelled_flight_answers_its_waiters_with_the_logo_but_a_cancelled_waiter_still_stops():
    svc = service(Fetch(gate=asyncio.Event()))
    waiter = asyncio.create_task(svc.pixels(COVER, wait_s=5))
    await settle()
    (flight,) = svc._flights.values()
    flight.cancel()

    assert await waiter == CoverResult() and svc._inflight == 0 and not svc._flights

    other = asyncio.create_task(svc.pixels(COVER.replace("segredo", "outro"), wait_s=5))
    await settle()
    other.cancel()
    with pytest.raises(asyncio.CancelledError):
        await other


@contextlib.asynccontextmanager
async def stalling_server():
    """Manda o cabeçalho (com corpo prometido) e para: só o prazo total da sessão encerra a leitura."""
    port = free_port()
    release = asyncio.Event()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 1000\r\n\r\n")
            await writer.drain()
            await release.wait()
        except ConnectionError, asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        yield port
    finally:
        release.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_server_that_stalls_after_the_headers_ends_by_the_deadline(monkeypatch, caplog):
    monkeypatch.setattr(fetch_module, "COVER_TOTAL_TIMEOUT_S", 0.05)  # prazo curto real: o servidor nunca termina
    async with stalling_server() as port:
        svc = CoverService(DEFAULT, fetch=local(port), decode=Decode(), clock=Clock())
        with caplog.at_level(logging.WARNING):
            assert await svc.pixels(COVER) == CoverResult()

    assert [r.getMessage() for r in caplog.records] == ["Capa sem imagem (deadline)"]
