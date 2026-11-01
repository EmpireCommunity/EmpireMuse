"""O cliente do Radio Browser (item 8, R3, R-7): o diretório é público e hostil. Espelhos validados, resposta limitada, itens ruins descartados,
2 req/s e cache; a busca nunca derruba a rádio.

A busca roda com um ``fetcher`` falso; o transporte (``AiohttpJsonFetcher``) roda contra um servidor local de verdade, em loopback."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import pytest

from empiremuse.utils.radio.directory import AiohttpJsonFetcher, Directory, DirectoryError, TokenBucket

pytestmark = pytest.mark.critical

UUID = "960e57c5-0601-11e8-ae97-52543be04c81"
UUID_2 = "11111111-2222-3333-4444-555555555555"
MIRROR = "de1.api.radio-browser.info"


def station(uuid=UUID, **over):
    return {
        "stationuuid": uuid,
        "name": "Rádio Boa",
        "url_resolved": "http://stream.example:8000/live",
        "codec": "MP3",
        "bitrate": 128,
        "hls": 0,
        "country": "Brasil",
        "homepage": "http://evil.example",
        **over,
    }


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Scripted:
    """Um fetcher falso: responde por caminho e registra cada pedido."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, mirror, path, query):
        self.calls.append((mirror, path, query))
        answer = self.answers[path] if path in self.answers else self.answers.get("*")
        if isinstance(answer, Exception):
            raise answer
        return answer


def directory(answers, *, mirrors=(MIRROR,), clock=None, bucket=None):
    clock = clock or Clock()

    async def found():
        return list(mirrors)

    fetcher = Scripted(answers)
    return (
        Directory(fetcher=fetcher, mirrors=found, clock=clock, bucket=bucket or TokenBucket(clock=clock)),
        fetcher,
        clock,
    )


class TestSearch:
    @pytest.mark.asyncio
    async def test_it_returns_clean_stations_and_drops_the_bad_ones_silently(self):
        payload = [
            station(),
            station(UUID_2, url_resolved="http://127.0.0.1:2333/x"),  # porta do Lavalink
            station("nao-e-uuid"),
            station(UUID_2, codec="WMA"),
            station(UUID_2, hls=1),
            station(UUID_2, bitrate=9999),
            "lixo",
            None,
            station(UUID_2, name="@everyone <@1> https://evil.example"),
        ]
        d, fetcher, _ = directory({"/json/stations/search": payload})

        found = await d.search("rock")

        assert [s.uuid for s in found] == [UUID, UUID_2]
        hostile = found[1].name  # neutralizado pela external_text (R-11): sem menção, sem marcação, sem link clicável
        assert "@everyone" not in hostile and "<@" not in hostile and "https://" not in hostile
        assert not hasattr(found[0], "homepage")
        _, path, query = fetcher.calls[0]
        assert (
            path == "/json/stations/search"
            and query["hidebroken"] == "true"
            and query["limit"] == "25"
            and query["name"] == "rock"
        )

    @pytest.mark.asyncio
    async def test_at_most_25_results(self):
        payload = [station(f"00000000-0000-0000-0000-{n:012d}") for n in range(60)]
        d, _, _ = directory({"/json/stations/search": payload})

        assert len(await d.search("rock")) == 25

    @pytest.mark.asyncio
    @pytest.mark.parametrize("term", ["", "a", "x" * 65, None, 5, "ro\x00ck", "  "])
    async def test_a_bad_term_never_reaches_the_directory(self, term):
        d, fetcher, _ = directory({"*": [station()]})

        assert await d.search(term) == [] and fetcher.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [{"a": 1}, "texto", 7, None, [None] * 5])
    async def test_a_payload_that_is_not_a_list_of_stations_is_an_empty_result(self, payload):
        d, _, _ = directory({"/json/stations/search": payload})

        assert await d.search("rock") == []

    @pytest.mark.asyncio
    async def test_the_same_term_is_served_from_the_cache_for_ten_minutes(self):
        d, fetcher, clock = directory({"/json/stations/search": [station()]})

        await d.search("Rock")
        await d.search("rock")
        assert len(fetcher.calls) == 1

        clock.now += 601
        await d.search("rock")
        assert len(fetcher.calls) == 2

    @pytest.mark.asyncio
    async def test_the_cache_holds_at_most_500_queries(self):
        d, _, _ = directory({"/json/stations/search": [station()]}, bucket=TokenBucket(rate=1e9, capacity=1e9))

        for n in range(520):
            await d.search(f"termo{n}")

        assert len(d._cache) == 500

    @pytest.mark.asyncio
    async def test_a_failed_search_is_not_cached(self):
        d, fetcher, _ = directory({"*": DirectoryError("status")})

        assert await d.search("rock") == []
        assert await d.search("rock") == []
        assert len(fetcher.calls) == 2  # tentou de novo, não guardou o vazio da falha


class TestRateAndMirrors:
    def test_the_bucket_gives_two_per_second_and_refills_with_time(self):
        clock = Clock()
        bucket = TokenBucket(clock=clock)

        assert [bucket.try_acquire() for _ in range(3)] == [True, True, False]
        clock.now += 0.5
        assert bucket.try_acquire() is True and bucket.try_acquire() is False

    @pytest.mark.asyncio
    async def test_beyond_two_requests_per_second_the_directory_is_not_called(self):
        d, fetcher, _ = directory({"/json/stations/search": [station()]})

        results = [await d.search(f"termo{n}") for n in range(5)]

        assert len(fetcher.calls) == 2 and results[2:] == [[], [], []]

    @pytest.mark.asyncio
    async def test_a_failing_mirror_falls_through_to_the_next_one(self):
        class Flaky(Scripted):
            async def __call__(self, mirror, path, query):
                self.calls.append((mirror, path, query))
                if mirror == "de1.api.radio-browser.info":
                    raise DirectoryError("status")
                return [station()]

        clock = Clock()

        async def found():
            return ["de1.api.radio-browser.info", "fr1.api.radio-browser.info"]

        class Fixed:
            def shuffle(self, items):
                pass

        d = Directory(fetcher=Flaky({}), mirrors=found, clock=clock, rng=Fixed())  # type: ignore[arg-type]

        assert [s.uuid for s in await d.search("rock")] == [UUID]
        assert [c[0] for c in d._fetch.calls] == ["de1.api.radio-browser.info", "fr1.api.radio-browser.info"]  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_only_valid_mirror_names_are_ever_used(self):
        d, fetcher, _ = directory(
            {"*": [station()]}, mirrors=("evil.example", "x.api.radio-browser.info.evil.com", "127.0.0.1", MIRROR)
        )

        await d.search("rock")

        assert {c[0] for c in fetcher.calls} == {MIRROR}

    @pytest.mark.asyncio
    async def test_no_mirror_at_all_is_an_empty_result_not_an_error(self):
        d, _, _ = directory({"*": [station()]}, mirrors=())

        assert await d.search("rock") == []

    @pytest.mark.asyncio
    async def test_a_discovery_that_raises_is_an_empty_result(self):
        async def broken():
            raise OSError("dns fora")

        d = Directory(fetcher=Scripted({"*": [station()]}), mirrors=broken)

        assert await d.search("rock") == []


class TestPlayAndHealth:
    @pytest.mark.asyncio
    async def test_by_uuid_asks_byuuid_which_does_not_register_a_click(self):
        d, fetcher, _ = directory({"/json/stations/byuuid": [station()]})

        found = await d.by_uuid(UUID)

        assert found is not None and found.uuid == UUID
        assert [c[1] for c in fetcher.calls] == ["/json/stations/byuuid"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("uuid", ["x", "", None, "../../etc/passwd", UUID + "/x", 5])
    async def test_a_bad_uuid_never_builds_a_request(self, uuid):
        d, fetcher, _ = directory({"*": [station()]})

        assert await d.by_uuid(uuid) is None and await d.fresh_for_play(uuid) is None and fetcher.calls == []

    @pytest.mark.asyncio
    async def test_by_uuid_rejects_a_different_station_in_the_answer(self):
        d, _, _ = directory({"/json/stations/byuuid": [station(UUID_2)]})

        assert await d.by_uuid(UUID) is None

    @pytest.mark.asyncio
    async def test_fresh_for_play_registers_the_click_and_revalidates_the_new_url(self):
        d, fetcher, _ = directory(
            {
                "/json/stations/byuuid": [station()],
                f"/json/url/{UUID}": {"ok": True, "url": "http://new.example:8010/stream"},
            },
            bucket=TokenBucket(rate=10, capacity=10),
        )

        found = await d.fresh_for_play(UUID)

        assert found is not None and found.url_resolved == "http://new.example:8010/stream" and found.uuid == UUID
        assert [c[1] for c in fetcher.calls] == ["/json/stations/byuuid", f"/json/url/{UUID}"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            {"ok": True, "url": "http://127.0.0.1:2333/x"},
            {"ok": True, "url": "file:///etc/passwd"},
            {"ok": True, "url": "http://user:pw@new.example:8000/x"},
            {"ok": False, "url": "http://new.example:8000/x"},
            {"ok": True},
            {"url": 5},
            [],
            "texto",
        ],
    )
    async def test_a_hostile_url_from_the_click_endpoint_is_not_played(self, answer):
        d, _, _ = directory(
            {"/json/stations/byuuid": [station()], f"/json/url/{UUID}": answer},
            bucket=TokenBucket(rate=10, capacity=10),
        )

        assert await d.fresh_for_play(UUID) is None


# ---------------------------------------------------------------------- transporte


def free_port() -> int:
    for port in range(8700, 8999):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("nenhuma porta livre")


@asynccontextmanager
async def http_server(raw: bytes):
    port = free_port()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(raw)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


def response(body: bytes, *, status="200 OK", extra="", length=True) -> bytes:
    head = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n{extra}"
    if length:
        head += f"Content-Length: {len(body)}\r\n"
    return head.encode() + b"Connection: close\r\n\r\n" + body


async def _loopback(host, port):
    return ["127.0.0.1"]


def fetcher_for(port, **kwargs):
    return AiohttpJsonFetcher(resolve=_loopback, public=lambda a: True, scheme="http", port=port, timeout=2, **kwargs)


class TestTransport:
    @pytest.mark.asyncio
    async def test_it_parses_a_json_answer(self):
        async with http_server(response(json.dumps([station()]).encode())) as port:
            data = await fetcher_for(port)(MIRROR, "/json/stations/search", {"name": "rock"})

        assert data[0]["stationuuid"] == UUID

    @pytest.mark.asyncio
    async def test_the_request_is_a_plain_get_with_our_user_agent_and_no_cookie(self):
        seen = {}
        port = free_port()

        async def handle(reader, writer):
            seen["head"] = (await reader.readuntil(b"\r\n\r\n")).decode()
            writer.write(response(b"[]"))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", port)
        try:
            await fetcher_for(port)(MIRROR, "/json/stations/search", {"name": "rock & roll"})
        finally:
            server.close()
            await server.wait_closed()

        head = seen["head"]
        assert (
            head.startswith("GET /json/stations/search?name=rock+%26+roll HTTP/1.1") or "name=rock%20%26%20roll" in head
        )
        assert "User-Agent: EmpireMuse/" in head and "Cookie" not in head and f"Host: {MIRROR}:{port}" in head

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            (response(b"x" * (300 * 1024)), "size"),  # Content-Length enorme
            (
                response(b"[" + b" " * (300 * 1024) + b"]", length=False),
                "size",
            ),  # sem Content-Length, o corpo passa de 256 KiB
            (response(b"[]", status="302 Found", extra="Location: http://127.0.0.1:2333/\r\n"), "status"),
            (response(b"[]", status="500 Oops"), "status"),
            (response(b"[]", extra="Content-Encoding: gzip\r\n"), "encoding"),
            (response(b"nao e json"), "json"),
            (response(b"[" * 200_000), "json"),  # aninhamento que estoura a recursão
        ],
        ids=[
            "declared_size",
            "undeclared_size",
            "redirect_not_followed",
            "server_error",
            "gzip",
            "not_json",
            "deep_nesting",
        ],
    )
    async def test_a_hostile_answer_becomes_a_directory_error_with_a_stable_reason(self, raw, reason):
        async with http_server(raw) as port:
            with pytest.raises(DirectoryError) as refusal:
                await fetcher_for(port)(MIRROR, "/json/stations/search", {})

        assert refusal.value.reason == reason

    @pytest.mark.asyncio
    async def test_a_mirror_outside_the_pattern_is_refused_before_resolving(self):
        async def must_not_resolve(host, port):
            raise AssertionError("não deveria resolver")

        fetcher = AiohttpJsonFetcher(resolve=must_not_resolve)

        for bad in ("evil.example", "127.0.0.1", "x.api.radio-browser.info.evil.com", ""):
            with pytest.raises(DirectoryError) as refusal:
                await fetcher(bad, "/json/stations/search", {})
            assert refusal.value.reason == "mirror"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "addresses", [["127.0.0.1"], ["10.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::ffff:127.0.0.1"], []]
    )
    async def test_a_mirror_that_resolves_inside_is_refused_with_the_default_public_check(self, addresses):
        async def inside(host, port):
            return addresses

        with pytest.raises(DirectoryError) as refusal:
            await AiohttpJsonFetcher(resolve=inside)(MIRROR, "/json/stations/search", {})

        assert refusal.value.reason == "address"

    @pytest.mark.asyncio
    async def test_the_default_transport_is_https(self):
        assert AiohttpJsonFetcher()._scheme == "https"

    @pytest.mark.asyncio
    async def test_a_dead_port_fails_as_an_error_the_directory_swallows(self):
        d = Directory(fetcher=fetcher_for(free_port()), mirrors=_one_mirror)

        assert await d.search("rock") == []


async def _one_mirror():
    return [MIRROR]


class TestReviewFindings:
    @pytest.mark.asyncio
    async def test_fresh_for_play_tells_a_missing_station_from_a_directory_that_did_not_answer(self):
        gone, _, _ = directory({"/json/stations/byuuid": []})
        assert await gone.fresh_for_play(UUID) is None  # respondeu: a estação não existe

        down, _, _ = directory({"*": DirectoryError("status")})
        with pytest.raises(DirectoryError):
            await down.fresh_for_play(UUID)  # não respondeu: não é culpa da estação

        empty, _, _ = directory({"/json/stations/byuuid": [station()]}, bucket=TokenBucket(rate=0.0, capacity=0.0))
        with pytest.raises(DirectoryError) as throttled:
            await empty.fresh_for_play(UUID)
        assert throttled.value.reason == "throttled"

    @pytest.mark.asyncio
    async def test_a_failed_discovery_is_not_repeated_for_a_minute_and_runs_one_at_a_time(self):
        calls = []
        clock = Clock()

        async def failing():
            calls.append(clock.now)
            await asyncio.sleep(0)
            raise OSError("dns fora")

        d = Directory(
            fetcher=Scripted({"*": [station()]}),
            mirrors=failing,
            clock=clock,
            bucket=TokenBucket(rate=1e9, capacity=1e9),
        )

        await asyncio.gather(*(d.search(f"termo{n}") for n in range(10)))  # dez teclas ao mesmo tempo
        assert len(calls) == 1

        clock.now += 30
        await d.search("outro")
        assert len(calls) == 1  # dentro do minuto: sem nova descoberta

        clock.now += 40
        await d.search("mais um")
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_discovery_that_hangs_is_cut_after_five_seconds(self, monkeypatch):
        from empiremuse.utils.radio import directory as module

        monkeypatch.setattr(module, "DISCOVERY_TIMEOUT_S", 0.1)

        async def hangs():
            await asyncio.sleep(10)

        d = Directory(fetcher=Scripted({"*": [station()]}), mirrors=hangs)

        assert await d.search("rock") == []
