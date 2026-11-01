"""O relay das rádios ao vivo (item 8, R3, R-8 e R-9), de ponta a ponta com estações falsas em loopback: o Lavalink só recebe áudio, de uma
estação conferida, por um token, um consumidor por vez. As estações ficam em portas da faixa permitida (8000-8999)."""

import asyncio
import socket
from contextlib import asynccontextmanager

import pytest

from empiremuse.utils.radio.relay import MAX_SOCKETS, RadioRelay, RateLimiter

pytestmark = pytest.mark.critical

MP3 = b"\xff\xfb\x90\x00"
HOLD: asyncio.Event | None = (
    None  # estação "que não termina": solta quando a estação falsa fecha, para o teste não esperar 30 s
)


@pytest.fixture(autouse=True)
def fresh_hold():
    global HOLD
    HOLD = asyncio.Event()
    yield
    HOLD = None


def free_port(start=8100) -> int:
    for port in range(start, 8999):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("nenhuma porta livre na faixa 8000-8999")


class Station:
    """Uma estação falsa: responde ao primeiro pedido com ``response`` e registra quem conectou."""

    def __init__(self, handler):
        self.handler = handler
        self.connections = 0
        self.port = free_port(8100 + (id(self) % 400))
        self.server = None

    async def __aenter__(self):
        async def wrapped(reader, writer):
            self.connections += 1
            try:
                await self.handler(reader, writer)
            finally:
                writer.close()

        self.server = await asyncio.start_server(wrapped, "127.0.0.1", self.port)
        return self

    async def __aexit__(self, *exc):
        if HOLD is not None:
            HOLD.set()
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self):
        return f"http://radio.example:{self.port}/live"


def sends(head: bytes, body: bytes = b"", *, keep_open=False):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(head + body)
        await writer.drain()
        if keep_open and HOLD is not None:
            await HOLD.wait()

    return handle


async def to_loopback(host, port):
    return ["127.0.0.1"]


@asynccontextmanager
async def relay(**kwargs):
    r = RadioRelay(resolve=to_loopback, public=lambda address: True, **kwargs)
    await r.start()
    try:
        yield r
    finally:
        await r.close()


async def fetch(r: RadioRelay, token: str, *, limit=1 << 16) -> tuple[bytes, bytes]:
    """Como o Lavalink: pede o token ao relay e devolve (cabeçalhos, corpo)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
    writer.write(f"GET /r/{token} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(limit), 3)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


def icy_stream(metaint: int, audio: bytes, title: str) -> bytes:
    raw = f"StreamTitle='{title}';".encode()
    size = -(-len(raw) // 16) * 16
    block = bytes([size // 16]) + raw.ljust(size, b"\x00")
    out, i = b"", 0
    while i < len(audio):
        out += audio[i : i + metaint] + block
        i += metaint
    return out


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_audio_reaches_the_consumer_clean_with_only_fixed_headers_and_the_title_goes_to_the_callback(self):
        titles = []
        audio = MP3 + b"A" * 28
        body = icy_stream(16, audio, "Artista - Faixa")
        head = b"ICY 200 OK\r\nicy-name:Estacao\r\nicy-description:Desc\r\nicy-metaint:16\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, body)) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128, on_title=titles.append)

            got_head, got_body = await fetch(r, token)

        assert got_head.startswith(b"HTTP/1.0 200 OK") and b"Content-Type: audio/mpeg" in got_head
        assert b"icy-" not in got_head.lower() and b"Estacao" not in got_head and b"Desc" not in got_head
        assert got_body == audio and b"StreamTitle" not in got_body
        assert titles[0] == "Artista - Faixa"

    @pytest.mark.asyncio
    async def test_the_local_url_carries_the_token_and_never_the_station(self):
        async with relay() as r:
            token = r.register("http://radio.example:8000/live", codec="MP3", bitrate=128)

            url = r.local_url(token)

        assert url == f"http://127.0.0.1:{r.port}/r/{token}" and "radio.example" not in url

    @pytest.mark.asyncio
    async def test_tokens_have_128_bits_and_are_unique(self):
        async with relay() as r:
            tokens = {r.register("http://x.example:8000/a", codec="MP3", bitrate=0) for _ in range(200)}

        assert len(tokens) == 200 and all(len(t) == 22 for t in tokens)  # 16 bytes em base64 url-safe


class TestR8NoSsrf:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            b"[playlist]\nFile1=http://mongo:27017/\n",
            b"#EXTM3U\nhttp://127.0.0.1:2333/\n",
            b'<?xml version="1.0"?><playlist><track><location>http://127.0.0.1:2333/</location></track></playlist>',
            b"<asx><entry><ref href='http://10.0.0.1/'/></entry></asx>",
        ],
    )
    async def test_a_playlist_body_with_an_audio_content_type_never_reaches_the_consumer(self, body):
        head = b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, body)) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)

            got_head, got_body = await fetch(r, token)

        assert got_head.startswith(b"HTTP/1.0 502") and got_body == b"" and body[:8] not in got_body

    @pytest.mark.asyncio
    async def test_a_redirect_to_the_lavalink_port_is_refused_and_nothing_connects_there(self):
        async with (
            Station(sends(b"HTTP/1.0 302 Found\r\nLocation: http://127.0.0.1:2333/\r\n\r\n")) as station,
            relay() as r,
        ):
            token = r.register(station.url, codec="MP3", bitrate=128)

            got_head, _ = await fetch(r, token)

        assert got_head.startswith(b"HTTP/1.0 502")

    @pytest.mark.asyncio
    async def test_a_redirect_chain_longer_than_three_hops_is_refused(self):
        async with Station(None) as _unused:
            pass
        hops = []

        async def redirecting(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            hops.append(1)
            writer.write(f"HTTP/1.0 302 Found\r\nLocation: http://radio.example:{port}/n{len(hops)}\r\n\r\n".encode())
            await writer.drain()

        port = free_port(8500)
        server = await asyncio.start_server(redirecting, "127.0.0.1", port)
        try:
            async with relay() as r:
                token = r.register(f"http://radio.example:{port}/start", codec="MP3", bitrate=128)

                got_head, _ = await fetch(r, token)
        finally:
            server.close()
            await server.wait_closed()

        assert got_head.startswith(b"HTTP/1.0 502") and len(hops) == 4  # o pedido original e 3 saltos

    @pytest.mark.asyncio
    async def test_a_redirect_to_a_private_address_is_refused_with_the_default_public_check(self):
        async with Station(sends(b"HTTP/1.0 302 Found\r\nLocation: http://10.0.0.1:8000/live\r\n\r\n")) as station:
            r = RadioRelay(resolve=to_loopback, public=lambda a: a == "127.0.0.1")  # só o primeiro salto é "público"
            await r.start()
            try:
                token = r.register(station.url, codec="MP3", bitrate=128)

                got_head, _ = await fetch(r, token)
            finally:
                await r.close()

        assert got_head.startswith(b"HTTP/1.0 502")

    @pytest.mark.asyncio
    async def test_a_pls_url_is_resolved_by_the_relay_and_the_consumer_gets_audio_not_the_playlist(self):
        async with Station(sends(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n", MP3 + b"ZZZZ")) as real:
            pls = f"[playlist]\nNumberOfEntries=1\nFile1={real.url}\n".encode()
            port = free_port(8600)
            server = await asyncio.start_server(
                sends(b"HTTP/1.0 200 OK\r\nContent-Type: audio/x-scpls\r\n\r\n", pls), "127.0.0.1", port
            )
            try:
                async with relay() as r:
                    token = r.register(f"http://radio.example:{port}/radio.pls", codec="MP3", bitrate=128)

                    got_head, got_body = await fetch(r, token)
            finally:
                server.close()
                await server.wait_closed()

        assert got_head.startswith(b"HTTP/1.0 200") and got_body == MP3 + b"ZZZZ" and b"playlist" not in got_body

    @pytest.mark.asyncio
    async def test_a_playlist_inside_a_playlist_is_not_followed(self):
        inner = b"http://radio.example:8001/other.pls\n"
        port = free_port(8650)
        server = await asyncio.start_server(
            sends(b"HTTP/1.0 200 OK\r\nContent-Type: audio/x-mpegurl\r\n\r\n", inner), "127.0.0.1", port
        )
        try:
            async with relay() as r:
                token = r.register(f"http://radio.example:{port}/a.m3u", codec="MP3", bitrate=128)

                got_head, _ = await fetch(r, token)
        finally:
            server.close()
            await server.wait_closed()

        assert got_head.startswith(b"HTTP/1.0 502")

    @pytest.mark.asyncio
    async def test_a_hostile_icy_name_never_reaches_the_consumer(self):
        head = b"ICY 200 OK\r\nicy-name:@everyone <@1>\r\nicy-description:https://evil.example\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, MP3 + b"data")) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)

            got_head, _ = await fetch(r, token)

        assert b"@everyone" not in got_head and b"evil" not in got_head

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content_type", ["text/html", "application/octet-stream", "audio/mp4", ""])
    async def test_a_wrong_content_type_is_refused_before_the_first_byte(self, content_type):
        head = f"HTTP/1.0 200 OK\r\nContent-Type: {content_type}\r\n\r\n".encode()
        async with Station(sends(head, MP3 + b"data")) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)

            got_head, got_body = await fetch(r, token)

        assert got_head.startswith(b"HTTP/1.0 502") and got_body == b""

    @pytest.mark.asyncio
    async def test_the_wrong_signature_for_the_codec_is_refused(self):
        head = b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, b"OggS....")) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)

            got_head, _ = await fetch(r, token)

        assert got_head.startswith(b"HTTP/1.0 502")


class TestEntrance:
    @pytest.mark.asyncio
    async def test_an_unknown_token_closes_without_reading_anything_more(self):
        async with relay() as r:
            reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
            writer.write(b"GET /r/AAAAAAAAAAAAAAAAAAAAAA HTTP/1.0\r\n\r\n" + b"x" * 100_000)
            await writer.drain()

            data = await asyncio.wait_for(reader.read(), 3)
            writer.close()

        assert data == b""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "request_line",
        [
            b"POST /r/AAAAAAAAAAAAAAAAAAAAAA HTTP/1.0",
            b"GET /x HTTP/1.0",
            b"GET /r/short HTTP/1.0",
            b"GET /r/" + b"A" * 5000,
            b"\x00\x01\x02",
        ],
    )
    async def test_anything_that_is_not_a_token_request_is_dropped_silently(self, request_line):
        async with relay() as r:
            reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
            writer.write(request_line + b"\r\n\r\n")
            await writer.drain()

            data = await asyncio.wait_for(reader.read(), 3)
            writer.close()

        assert data == b""

    @pytest.mark.asyncio
    async def test_it_listens_on_loopback_only(self):
        async with relay() as r:
            assert r._server.sockets[0].getsockname()[0] == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_a_second_consumer_takes_over_and_the_first_output_is_closed_so_there_is_still_one_at_a_time(self):
        head = b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, MP3 + b"data", keep_open=True)) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)
            reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
            writer.write(f"GET /r/{token} HTTP/1.0\r\n\r\n".encode())
            await writer.drain()
            await asyncio.wait_for(
                reader.read(10), 3
            )  # o primeiro consumidor (loadtracks do Lavalink) ainda está aberto
            assert r.sessions() == 1

            second_head, second_body = await fetch(r, token)  # o segundo (play) assume
            first_tail = await asyncio.wait_for(reader.read(), 3)  # e a primeira saída foi fechada
            writer.close()

            assert second_head.startswith(b"HTTP/1.0 200") and second_body.startswith(MP3)
            assert r.touched(token) and not r.refused(token)
            assert first_tail.endswith(b"data") or first_tail == b""

    @pytest.mark.asyncio
    async def test_after_the_first_consumer_leaves_a_second_one_may_connect(self):
        head = b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, MP3 + b"data")) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)

            first, _ = await fetch(r, token)
            second, _ = await fetch(r, token)

        assert first.startswith(b"HTTP/1.0 200") and second.startswith(
            b"HTTP/1.0 200"
        )  # loadtracks e play em sequência

    @pytest.mark.asyncio
    async def test_a_revoked_token_stops_working_and_closes_the_open_output(self):
        head = b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, MP3 + b"data", keep_open=True)) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)
            reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
            writer.write(f"GET /r/{token} HTTP/1.0\r\n\r\n".encode())
            await writer.drain()
            await asyncio.wait_for(reader.read(10), 3)

            await r.revoke(token)
            tail = await asyncio.wait_for(reader.read(), 3)  # a saída fechou
            writer.close()
            after, _ = await fetch(r, token)

        assert tail.endswith(
            b"data"
        )  # o resto do que já estava no ar; depois disso a saída está FECHADA (o read terminou)
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert after == b""  # token invalidado: fecha sem responder

    @pytest.mark.asyncio
    async def test_the_socket_cap_drops_the_extras(self):
        async with relay(max_sockets=3) as r:
            held = []
            for _ in range(3):
                reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
                held.append((reader, writer))  # ficam abertos sem mandar o pedido
            await asyncio.sleep(0.05)
            reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
            writer.write(b"GET /r/AAAAAAAAAAAAAAAAAAAAAA HTTP/1.0\r\n\r\n")

            try:
                data = await asyncio.wait_for(reader.read(), 3)  # o 4º é fechado na hora
            except ConnectionResetError:
                data = b""  # fechado com dados ainda não lidos: o sistema entrega como "reset"

            for _, w in [*held, (reader, writer)]:
                w.close()
        assert data == b"" and MAX_SOCKETS == 64


class TestRateLimiter:
    def test_the_burst_goes_through_and_then_the_pace_is_one_and_a_half_times_the_bitrate(self):
        limiter = RateLimiter(128)  # 128 kbps = 16000 B/s; x1,5 = 24000 B/s; rajada de 2 s = 48000 B

        assert limiter.delay_for(40_000, 0.0) == 0.0
        late = limiter.delay_for(24_000, 0.0)  # 64000 enviados contra 48000 permitidos

        assert late == pytest.approx(16_000 / 24_000)

    def test_the_credit_never_grows_past_the_burst_however_long_the_station_was_slow(self):
        limiter = RateLimiter(128)
        limiter.delay_for(1_000, 0.0)

        wait = limiter.delay_for(
            48_000 + 24_000, 3600.0
        )  # uma hora depois: só a rajada de 2 s vale, não 1 h de crédito

        assert wait == pytest.approx(24_000 / 24_000)

    def test_time_passing_gives_the_allowance_back(self):
        limiter = RateLimiter(128)
        limiter.delay_for(48_000, 0.0)

        assert limiter.delay_for(24_000, 1.0) == 0.0

    def test_a_flood_at_50_mb_per_s_is_throttled_to_the_pace(self):
        limiter = RateLimiter(128)
        now, total_wait = 0.0, 0.0

        for _ in range(100):  # 100 x 512 KiB "instantâneos"
            wait = limiter.delay_for(512 * 1024, now)
            total_wait += wait
            now += wait

        sent = 100 * 512 * 1024
        assert total_wait == pytest.approx((sent - 48_000) / 24_000, rel=0.01)

    def test_without_a_bitrate_the_ceiling_is_the_biggest_accepted_one(self):
        assert RateLimiter(0).rate == RateLimiter(320).rate


class TestReviewFindings:
    """Achados da revisão da R3: título limpo e com teto, taxa pelo que a estação manda, sem socket vazado, DNS e prazo."""

    @pytest.mark.asyncio
    async def test_a_hostile_stream_title_is_cleaned_and_titles_are_published_at_most_every_fifteen_seconds(self):
        titles = []
        hostile = "@everyone <@1> https://evil.example/x " + "z" * 3000
        body = icy_stream(16, MP3 + b"A" * 60, hostile)
        head = b"ICY 200 OK\r\nicy-metaint:16\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, body)) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128, on_title=titles.append)

            await fetch(r, token)

        assert len(titles) == 1  # cinco blocos iguais na janela de 15 s: um título
        assert (
            "@everyone" not in titles[0]
            and "<@" not in titles[0]
            and "https://" not in titles[0]
            and len(titles[0]) <= 150
        )

    @pytest.mark.asyncio
    async def test_a_tiny_metaint_cannot_buy_line_speed_because_the_stations_bytes_are_charged(self):
        sleeps = []

        async def record(seconds):
            sleeps.append(seconds)

        body = icy_stream(4, MP3 + b"A" * 3000, "t")  # metaint pequeno: quase tudo que a estação manda é metadado
        head = b"ICY 200 OK\r\nicy-metaint:4\r\nContent-Type: audio/mpeg\r\n\r\n"
        async with Station(sends(head, body)) as station, relay(clock=lambda: 0.0, sleep=record) as r:
            token = r.register(station.url, codec="MP3", bitrate=8)  # 8 kbps: rajada de 3000 B, ritmo de 1500 B/s
            reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
            writer.write(f"GET /r/{token} HTTP/1.0\r\n\r\n".encode())
            await writer.drain()

            await asyncio.wait_for(reader.read(), 5)  # lê até o fim: o consumidor fica até o relay terminar
            writer.close()

        assert len(body) > 10_000 and sum(sleeps) > 5  # o fluxo ficou parado por muito tempo, não passou direto

    @pytest.mark.asyncio
    async def test_revoking_while_the_station_is_still_answering_closes_the_stations_socket(self):
        closed = asyncio.Event()

        async def slow(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.0 200 OK\r\n")  # nunca termina os cabeçalhos
            await writer.drain()
            await reader.read()  # só volta quando o relay fecha a conexão
            closed.set()

        async with Station(slow) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)
            reader, writer = await asyncio.open_connection("127.0.0.1", r.port)
            writer.write(f"GET /r/{token} HTTP/1.0\r\n\r\n".encode())
            await writer.drain()
            for _ in range(200):
                await asyncio.sleep(0.01)
                if station.connections:
                    break

            await r.revoke(token)

            await asyncio.wait_for(closed.wait(), 3)
            writer.close()

    @pytest.mark.asyncio
    async def test_a_slow_station_is_cut_by_the_overall_deadline_and_frees_the_open_slot(self, monkeypatch):
        from empiremuse.utils.radio import relay as relay_module

        monkeypatch.setattr(relay_module, "DEADLINE_S", 0.3)

        async def never(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            await HOLD.wait()

        async with Station(never) as station, relay() as r:
            token = r.register(station.url, codec="MP3", bitrate=128)

            got_head, _ = await fetch(r, token)

            assert got_head.startswith(b"HTTP/1.0 502") and r._open._value == 4  # as 4 vagas voltaram

    @pytest.mark.asyncio
    async def test_a_playlist_that_arrives_in_two_segments_is_read_whole(self):
        async with Station(sends(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n", MP3 + b"ZZZZ")) as real:
            pls = f"[playlist]\nNumberOfEntries=1\nFile1={real.url}\n".encode()
            half = len(pls) // 2

            async def split(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: audio/x-scpls\r\n\r\n" + pls[:half])
                await writer.drain()
                await asyncio.sleep(0.05)
                writer.write(pls[half:])
                await writer.drain()
                writer.close()

            port = free_port(8700)
            server = await asyncio.start_server(split, "127.0.0.1", port)
            try:
                async with relay() as r:
                    token = r.register(f"http://radio.example:{port}/radio.pls", codec="MP3", bitrate=128)

                    got_head, got_body = await fetch(r, token)
            finally:
                server.close()
                await server.wait_closed()

        assert got_head.startswith(b"HTTP/1.0 200") and got_body == MP3 + b"ZZZZ"

    @pytest.mark.asyncio
    async def test_a_playlist_entry_that_does_not_resolve_does_not_stop_the_next_ones(self):
        async with Station(sends(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n", MP3 + b"ZZZZ")) as real:
            pls = f"[playlist]\nFile1=http://nxdomain.example:8000/a\nFile2={real.url}\n".encode()
            port = free_port(8750)
            server = await asyncio.start_server(
                sends(b"HTTP/1.0 200 OK\r\nContent-Type: audio/x-scpls\r\n\r\n", pls), "127.0.0.1", port
            )

            async def resolver(host, p):
                if host == "nxdomain.example":
                    raise socket.gaierror("Name or service not known")
                return ["127.0.0.1"]

            try:
                r = RadioRelay(resolve=resolver, public=lambda a: True)
                await r.start()
                try:
                    token = r.register(f"http://radio.example:{port}/radio.pls", codec="MP3", bitrate=128)

                    got_head, got_body = await fetch(r, token)
                finally:
                    await r.close()
            finally:
                server.close()
                await server.wait_closed()

        assert got_head.startswith(b"HTTP/1.0 200") and got_body == MP3 + b"ZZZZ"
