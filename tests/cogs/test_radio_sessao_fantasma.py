"""Uma rádio desligada ou movida NUNCA é ressuscitada pela retomada genérica de sessões (live test): a sessão antiga de uma rádio voltava no canal VELHO
como um player fantasma e colidia com a rádio de verdade no palco (``_voice_state`` nulo, Lavalink sem conexão, silêncio). A rádio só volta do banco de
rádios (ligada e no canal de agora); a retomada de sessões pula bots e servidores de rádio."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs.player_resume import PlayerSession
from empiremuse.rules.radio import check_radio, radio_id
from empiremuse.utils.radio import RadioHub
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

BOT_ID, OTHER_BOT, GUILD, OTHER_GUILD, CHANNEL = 11, 12, 500, 501, 900


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    real_sleep = asyncio.sleep

    async def quick(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", quick)  # a retomada espera 1 s entre sessões


def build(sessions, *, identifier="TOKEN_2"):
    db = FakeDb()
    pool = SimpleNamespace(database=db)
    hub = RadioHub(pool)
    pool.radio = hub
    bot = SimpleNamespace(
        pool=pool,
        user=SimpleNamespace(id=BOT_ID),
        identifier=identifier,
        player_resuming=False,
        player_resumed=False,
        bot_ready=True,
        wait_until_ready=AsyncMock(),
        config={"PLAYER_SESSIONS_MONGODB": False, "MONGO": False, "EXTRA_HINTS": ""},
        players_resumed={},
        loop=asyncio.get_event_loop(),
    )
    cog = SimpleNamespace(
        bot=bot,
        get_player_sessions_mongo=AsyncMock(return_value=[]),
        get_player_sessions_local=AsyncMock(return_value=[dict(s) for s in sessions]),
        save_session_local=AsyncMock(),
        save_session_mongo=AsyncMock(),
        delete_data_local=lambda _id: None,
        delete_data=AsyncMock(),
        resume_player=AsyncMock(),
    )
    cog._radio_owns_session = lambda data: PlayerSession._radio_owns_session(cog, data)
    return SimpleNamespace(cog=cog, hub=hub, db=db, bot=bot)


async def add_radio(w, *, enabled, guild=GUILD, bot_id=BOT_ID, kind="custom"):
    await w.hub.premium.grant(guild, by=1)
    source = (
        {"playlist": "Rock"}
        if kind == "custom"
        else {"station": "11111111-1111-1111-1111-111111111111", "reserve": None}
    )
    radio = check_radio({"_id": radio_id(bot_id, guild), "channel_id": str(CHANNEL), "kind": kind, "source": source})
    created = await w.hub.store.create(radio)
    if enabled:
        await w.hub.store.set_enabled(created.id, True)
    return created


async def resume(w):
    await PlayerSession.resume_players(w.cog)
    await asyncio.gather(*w.bot.players_resumed.values())
    return sorted(call.args[0]["_id"] for call in w.cog.resume_player.await_args_list)


def forgotten(w):
    return sorted(call.args[0] for call in w.cog.delete_data.await_args_list)


class TestGhostSessions:
    @pytest.mark.asyncio
    async def test_a_session_of_a_guild_with_an_enabled_radio_of_this_bot_is_skipped_and_forgotten(self):
        w = build([{"_id": GUILD, "keep_connected": False}])
        await add_radio(w, enabled=True)

        assert await resume(w) == [] and forgotten(w) == [GUILD]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["custom", "live"])
    async def test_the_old_session_of_a_turned_off_radio_never_comes_back(self, kind):
        w = build([{"_id": GUILD, "keep_connected": True, "loop": "queue", "voice_channel": 777}])  # o canal ANTIGO
        await add_radio(w, enabled=False, kind=kind)

        assert await resume(w) == [] and forgotten(w) == [GUILD]

    @pytest.mark.asyncio
    async def test_an_ordinary_music_session_of_a_bot_with_a_turned_off_radio_still_resumes(self):
        w = build([{"_id": GUILD, "keep_connected": False}])
        await add_radio(w, enabled=False)

        assert await resume(w) == [GUILD] and forgotten(w) == []

    @pytest.mark.asyncio
    async def test_without_any_radio_every_session_resumes_as_always(self):
        w = build([{"_id": GUILD, "keep_connected": True}, {"_id": OTHER_GUILD, "keep_connected": False}])

        assert await resume(w) == [GUILD, OTHER_GUILD] and forgotten(w) == []

    @pytest.mark.asyncio
    async def test_a_radio_in_another_guild_or_of_another_bot_does_not_hide_this_session(self):
        w = build([{"_id": GUILD, "keep_connected": True}])
        await add_radio(w, enabled=True, guild=OTHER_GUILD)
        await add_radio(w, enabled=True, bot_id=OTHER_BOT)

        assert await resume(w) == [GUILD] and forgotten(w) == []

    @pytest.mark.asyncio
    async def test_every_session_of_a_radio_only_bot_is_skipped_and_forgotten(self):
        w = build([{"_id": GUILD, "keep_connected": False}, {"_id": OTHER_GUILD, "keep_connected": True}])
        await w.hub.roles.add("TOKEN_2")

        assert await resume(w) == [] and forgotten(w) == [GUILD, OTHER_GUILD]

    @pytest.mark.asyncio
    async def test_when_the_radio_database_is_down_nothing_is_resumed_and_nothing_is_forgotten(self):
        w = build([{"_id": GUILD, "keep_connected": True}])
        await add_radio(w, enabled=True)
        w.db.fail = True

        assert await resume(w) == [] and forgotten(w) == []  # na próxima subida decide

    @pytest.mark.asyncio
    async def test_the_main_bot_never_a_radio_only_one_resumes_its_sessions(self):
        w = build([{"_id": GUILD, "keep_connected": True}], identifier="TOKEN")
        await w.hub.roles.load()

        assert await resume(w) == [GUILD]
