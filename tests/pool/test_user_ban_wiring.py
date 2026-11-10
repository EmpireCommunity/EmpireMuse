"""E5: a FIAÇÃO do banimento no pool real (``BotPool.setup`` e ``run_bots``), que os testes de unidade não enxergam.

Sem estes testes, tirar ``install_interaction_gate`` do ``load_bot``, o ``boot()`` do ``run_bots`` ou os ``OWNER_IDS`` do ``UserBans`` mantém tudo verde.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from empiremuse.utils import client
from empiremuse.utils.client import BotPool
from empiremuse.utils.user_ban import UserBans, user_bans_of
from empiremuse.utils.user_ban_gate import InteractionGate
from tests.support.laco_do_pool import encerrar_laco

pytestmark = pytest.mark.critical

TOKEN_FALSO = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAx.GabcDE.abcdefghijklmnopqrstuvwxyz1"  # só o formato; não é um token real
DONOS = (100000000000000001, 100000000000000002)


def ambiente(monkeypatch, tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(home))
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("RUN_LOCAL_LAVALINK", "false")
    monkeypatch.setenv("RUN_RPC_SERVER", "false")
    monkeypatch.setenv("TOKEN", TOKEN_FALSO)
    monkeypatch.setenv("OWNER_IDS", "||".join(str(i) for i in DONOS))


@pytest.fixture
def pool(monkeypatch, tmp_path):
    """``BotPool.setup`` real; cada ``BotCore`` dublado leva um parser ``INTERACTION_CREATE`` próprio, como o do disnake."""
    ambiente(monkeypatch, tmp_path)
    pool = BotPool()
    bots = []

    def bot_falso(**kwargs):
        bot = MagicMock(identifier=kwargs["identifier"])
        bot.loop.create_task.side_effect = lambda corotina: corotina.close()
        bot._connection.parsers = {"INTERACTION_CREATE": MagicMock(name="parser_original")}
        bots.append(bot)
        return bot

    monkeypatch.setattr(client, "BotCore", bot_falso)
    for nome in ("run_bots", "setup_pool_extras", "cache_updater", "connect_rpc_ws"):
        setattr(pool, nome, AsyncMock())
    pool.node_check = MagicMock()
    pool.start_lavalink = AsyncMock()
    pool.created_bots = bots
    monkeypatch.setattr(client, "check_output", lambda *a, **k: b"")
    yield pool
    encerrar_laco(pool.loop)
    asyncio.set_event_loop(None)


def test_the_real_pool_has_the_ban_list_with_the_env_owners(pool):
    bans = user_bans_of(pool)

    assert isinstance(bans, UserBans) and not bans.loaded
    assert all(not bans.is_blocked(dono) for dono in DONOS)
    bans._records = {dono: object() for dono in DONOS}  # mesmo "banidos", o dono do .env nunca é bloqueado
    assert not any(bans.is_blocked(dono) for dono in DONOS)


def test_setup_installs_the_gate_in_every_bot_of_the_pool(pool):
    pool.setup()

    assert pool.created_bots, "o setup deveria ter criado ao menos um bot"
    for bot in pool.created_bots:
        parser = bot._connection.parsers["INTERACTION_CREATE"]
        assert isinstance(parser, InteractionGate) and parser.bans is pool.user_bans


@pytest.mark.asyncio
async def test_run_bots_loads_the_ban_list_before_any_bot_connects():
    ordem = []
    pool = BotPool.__new__(BotPool)
    pool.user_bans = MagicMock()
    pool.user_bans.boot = AsyncMock(side_effect=lambda: ordem.append("boot"))
    pool.start_bot = AsyncMock(side_effect=lambda bot: ordem.append(f"start:{bot}"))

    await BotPool.run_bots(pool, ["a", "b"])

    assert ordem[0] == "boot" and sorted(ordem[1:]) == ["start:a", "start:b"]


@pytest.mark.asyncio
async def test_a_non_timeout_load_failure_at_boot_still_starts_the_bots_with_the_list_not_loaded():
    """D2: qualquer falha da carga (não só ``TimeoutError``) abre o bot sem lista; o login do painel segue fechado (``loaded`` falso)."""

    class ErroDoDriver(Exception):  # como o erro de seleção de servidor do driver: não é ``TimeoutError`` embutido
        pass

    pool = BotPool.__new__(BotPool)
    pool.user_bans = UserBans(MagicMock(), owners=frozenset(DONOS), sleep=lambda _s: asyncio.Event().wait())
    pool.user_bans.ensure_loaded = AsyncMock(side_effect=ErroDoDriver("banco fora"))
    pool.start_bot = AsyncMock()

    await BotPool.run_bots(pool, ["a", "b"])

    assert pool.start_bot.await_count == 2
    assert not pool.user_bans.loaded and pool.user_bans._retry_task is not None  # nova tentativa em segundo plano
    pool.user_bans.stop()
