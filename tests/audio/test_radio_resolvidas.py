"""O cache de faixas resolvidas (contrato R5 §4), o custo do sorteio no banco e o semáforo global do adiantado (§3)."""

import asyncio
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio_shuffle import Track
from empiremuse.utils.radio.custom_ports import CustomRadioPorts
from empiremuse.utils.radio.custom_source import (
    ABSENT_TTL_S,
    FAIL_TTL_S,
    RESOLVED_TTL_S,
    STOCK_SLOTS,
    CustomSession,
    ResolvedTracks,
)
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

TRACK = Track("Djavan", "Oceano")


def resolved_partial(encoded="QAAAencoded"):
    return SimpleNamespace(
        id=encoded, info={"uri": "https://example.test/o", "sourceName": "soundcloud", "length": 215000}
    )


class CountingDb(FakeDb):
    def __init__(self):
        super().__init__()
        self.reads = 0

    async def find_doc(self, id_, *, db_name, collection):
        self.reads += 1
        return await super().find_doc(id_, db_name=db_name, collection=collection)


class TestResolvedCache:
    @pytest.mark.asyncio
    async def test_a_resolved_track_survives_a_restart(self):
        db = FakeDb()
        await ResolvedTracks(db).store(TRACK, resolved_partial())

        entry = await ResolvedTracks(db).get(Track("  DJAVAN", "oceano "))  # outro processo, mesma faixa

        assert entry is not None
        assert {k: entry[k] for k in ("encoded", "uri", "source", "length_ms")} == {
            "encoded": "QAAAencoded",
            "uri": "https://example.test/o",
            "source": "soundcloud",
            "length_ms": 215000,
        }

    @pytest.mark.asyncio
    async def test_a_resolved_track_expires_after_seven_days(self):
        now = [1000.0]
        cache = ResolvedTracks(FakeDb(), clock=lambda: now[0])
        await cache.store(TRACK, resolved_partial())
        now[0] += RESOLVED_TTL_S + 1

        assert await cache.get(TRACK) is None

    @pytest.mark.asyncio
    async def test_a_track_without_encoded_is_not_stored(self):
        db = FakeDb()
        cache = ResolvedTracks(db)
        await cache.store(TRACK, SimpleNamespace(id=None, info={}))

        assert await ResolvedTracks(db).get(TRACK) is None

    @pytest.mark.asyncio
    async def test_a_failure_replaces_the_resolved_entry_and_the_other_way_round(self):
        db = FakeDb()
        cache = ResolvedTracks(db)
        await cache.store(TRACK, resolved_partial())
        await cache.mark(TRACK)
        assert await ResolvedTracks(db).get(TRACK) is None and await ResolvedTracks(db).failed(TRACK) is True

        await cache.store(TRACK, resolved_partial("QAAAnew"))
        assert (await ResolvedTracks(db).get(TRACK))["encoded"] == "QAAAnew"
        assert await ResolvedTracks(db).failed(TRACK) is False

    @pytest.mark.asyncio
    async def test_forgetting_deletes_the_entry_everywhere(self):
        db = FakeDb()
        cache = ResolvedTracks(db)
        await cache.store(TRACK, resolved_partial())

        await cache.forget(TRACK)

        assert await cache.get(TRACK) is None and await ResolvedTracks(db).get(TRACK) is None

    @pytest.mark.asyncio
    async def test_the_failure_ttl_is_still_24_hours(self):
        now = [1000.0]
        cache = ResolvedTracks(FakeDb(), clock=lambda: now[0])
        await cache.mark(TRACK)
        now[0] += FAIL_TTL_S + 1

        assert await cache.failed(TRACK) is False


class TestTheDrawDoesNotHammerTheDatabase:
    @pytest.mark.asyncio
    async def test_an_absent_entry_is_read_once_not_once_per_draw(self):
        db = CountingDb()
        pool = [Track(f"Artista {i // 10}", f"Faixa {i}") for i in range(300)]
        session = CustomSession(pool, ResolvedTracks(db))

        async def resolve(track):
            return object()

        for _ in range(4):  # quatro sorteios (duas faixas, play_next e _stock de cada)
            await session.next_playable(resolve)

        assert db.reads <= len(pool)  # uma leitura por faixa do pool no total, nunca uma por faixa a cada sorteio

    @pytest.mark.asyncio
    async def test_the_absence_is_forgotten_after_a_few_minutes(self):
        now = [1000.0]
        db = CountingDb()
        cache = ResolvedTracks(db, clock=lambda: now[0])
        await cache.failed(TRACK)
        await cache.failed(TRACK)
        assert db.reads == 1

        now[0] += ABSENT_TTL_S + 1
        await cache.failed(TRACK)

        assert db.reads == 2


class TestGlobalStockSemaphore:
    @pytest.mark.asyncio
    async def test_at_most_two_radios_stock_a_track_at_the_same_time(self):
        slots = asyncio.Semaphore(STOCK_SLOTS)
        running, peak = [0], [0]

        class Session:
            async def next_playable(self, resolve):
                running[0] += 1
                peak[0] = max(peak[0], running[0])
                await asyncio.sleep(0.01)
                running[0] -= 1
                return Track("a", "b"), object()

        def make_ports():
            ports = object.__new__(CustomRadioPorts)
            queue = []
            player = SimpleNamespace(queue=SimpleNamespace(append=queue.append, __len__=None))
            player.queue = _Queue()
            ports._player = lambda: player
            ports.session = Session()
            ports.pool = SimpleNamespace(radio=SimpleNamespace(stock_slots=slots))
            ports._stocking = asyncio.Lock()
            ports._resolve = None
            return ports

        await asyncio.gather(*(make_ports()._stock() for _ in range(6)))

        assert peak[0] == STOCK_SLOTS


class _Queue(list):
    pass


class TestThePortsUseTheCache:
    @staticmethod
    def make_ports(db, *, decodes=True, resolves=True):
        from unittest.mock import AsyncMock

        calls = SimpleNamespace(resolve=0)

        async def resolve_track(partial):
            calls.resolve += 1
            if resolves:
                partial.id = "QAAAfresh"
                partial.info.update({"uri": "https://example.test/f", "sourceName": "youtube", "length": 1000})

        node = SimpleNamespace(build_track=AsyncMock() if decodes else AsyncMock(side_effect=RuntimeError("bad")))
        player = SimpleNamespace(node=node, resolve_track=resolve_track)
        ports = object.__new__(CustomRadioPorts)
        ports._player = lambda: player
        ports.radio = SimpleNamespace(bot_id=7)
        ports.pool = SimpleNamespace(radio=SimpleNamespace(resolved=ResolvedTracks(db)))
        return ports, calls

    @pytest.mark.asyncio
    async def test_a_cached_track_plays_without_asking_the_node_to_search(self):
        db = FakeDb()
        await ResolvedTracks(db).store(TRACK, resolved_partial())
        ports, calls = self.make_ports(db)

        partial = await ports._resolve(TRACK)

        assert partial.id == "QAAAencoded" and calls.resolve == 0
        assert partial.info["uri"] == "https://example.test/o" and partial.info["sourceName"] == "soundcloud"

    @pytest.mark.asyncio
    async def test_an_entry_that_no_longer_decodes_is_deleted_and_the_track_is_searched_again(self):
        db = FakeDb()
        await ResolvedTracks(db).store(TRACK, resolved_partial())
        ports, calls = self.make_ports(db, decodes=False)

        partial = await ports._resolve(TRACK)

        assert partial.id == "QAAAfresh" and calls.resolve == 1
        assert (await ResolvedTracks(db).get(TRACK))["encoded"] == "QAAAfresh"  # a nova resolução ocupou o lugar

    @pytest.mark.asyncio
    async def test_a_fresh_resolution_is_stored_for_the_next_time(self):
        db = FakeDb()
        ports, _ = self.make_ports(db)

        await ports._resolve(TRACK)

        assert (await ResolvedTracks(db).get(TRACK))["encoded"] == "QAAAfresh"

    @pytest.mark.asyncio
    async def test_a_track_that_does_not_resolve_stores_nothing(self):
        db = FakeDb()
        ports, _ = self.make_ports(db, resolves=False)

        assert await ports._resolve(TRACK) is None
        assert await ResolvedTracks(db).get(TRACK) is None
