"""A skin própria de uma rádio no banco (``RadioStore.set_look``/``set_skin``), a skin efetiva no retrato do painel e o
carregador das skins do EmpireMuse (``BotPool.load_muse_skins``). Fakes do repositório, sem rede."""

import dataclasses
import logging
import sys
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio import radio_id
from empiremuse.rules.skins import DEFAULT_RADIO_LOOK, JUKEBOX_IDS
from empiremuse.utils import client as client_module
from empiremuse.utils.client import BotPool
from empiremuse.utils.radio.snapshot import live_snapshot
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound
from tests.audio.test_radio_snapshot import TestSnapshot
from tests.cogs.radio_config import configure
from tests.cogs.test_radio_comandos import CHANNEL, GUILD, build, premium

pytestmark = pytest.mark.critical

OTHER_GUILD = GUILD + 1


async def a_radio(w):
    await premium(w)
    return await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2, kind="live")


def count_writes(monkeypatch, store):
    writes = []
    original = store._save

    async def spy(*args, **kwargs):
        writes.append(args[0])
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "_save", spy)
    return writes


# ---------------------------------------------------------------------- store


@pytest.mark.asyncio
@pytest.mark.parametrize(("look", "skin"), [("portrait_white", "white"), ("text", "black"), (None, "black")])
async def test_set_look_saves_the_look_and_the_legacy_theme_with_the_rev(look, skin):
    w = build()
    radio = await a_radio(w)

    saved = await w.hub.store.set_look(GUILD, radio.id, look, expected_rev=radio.rev)

    assert (saved.look, saved.skin, saved.rev) == (look, skin, radio.rev + 1)
    assert await w.hub.store.get(radio.id) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["set_look", "set_skin"])
@pytest.mark.parametrize("guild", [OTHER_GUILD, None, str(GUILD), True, "x", 0])  # None: interação fora de servidor
async def test_a_radio_of_another_server_is_refused_without_writing(monkeypatch, method, guild):
    w = build()
    radio = await a_radio(w)
    writes = count_writes(monkeypatch, w.hub.store)

    with pytest.raises(RadioNotFound):
        await getattr(w.hub.store, method)(guild, radio.id, "white", expected_rev=radio.rev)

    assert writes == [] and (await w.hub.store.get(radio.id)).look is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "value"), [("set_look", "../x"), ("set_look", "neon"), ("set_look", 3), ("set_skin", "portrait_black")]
)
async def test_an_unknown_value_is_refused_without_writing(monkeypatch, method, value):
    w = build()
    radio = await a_radio(w)
    writes = count_writes(monkeypatch, w.hub.store)

    with pytest.raises(RadioInvalid):
        await getattr(w.hub.store, method)(GUILD, radio.id, value, expected_rev=radio.rev)

    assert writes == []


@pytest.mark.asyncio
async def test_set_look_is_compare_and_set_and_a_missing_radio_is_not_found():
    w = build()
    radio = await a_radio(w)
    saved = await w.hub.store.set_look(GUILD, radio.id, "text", expected_rev=radio.rev)

    with pytest.raises(RadioConflict):
        await w.hub.store.set_look(GUILD, radio.id, "white", expected_rev=radio.rev)  # rev velho
    with pytest.raises(RadioNotFound):
        await w.hub.store.set_look(GUILD, radio_id(3, GUILD), "white", expected_rev=0)
    assert (await w.hub.store.get(radio.id)).look == saved.look


@pytest.mark.asyncio
async def test_set_look_losing_the_compare_and_set_race_is_a_conflict(monkeypatch):
    w = build()
    radio = await a_radio(w)

    async def lost(*args, **kwargs):  # outra escrita entrou entre a leitura e a gravação
        return False

    monkeypatch.setattr(w.hub.store, "_save", lost)

    with pytest.raises(RadioConflict):
        await w.hub.store.set_look(GUILD, radio.id, "text", expected_rev=radio.rev)
    assert (await w.hub.store.get(radio.id)).look is None


# ---------------------------------------------------------------------- retrato do painel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own", "guild_radio_skin", "look", "skin"),
    [
        ("white", "portrait_black", "white", "white"),  # a própria vence a de todas as rádios
        (None, "portrait_white", "portrait_white", "black"),  # sem a própria, a de todas as rádios
        (None, None, DEFAULT_RADIO_LOOK, "black"),
    ],
)
async def test_the_snapshot_carries_the_effective_look_and_keeps_the_old_skin(own, guild_radio_skin, look, skin):
    hub, radio, ports, runner, _ = TestSnapshot.world()
    radio = dataclasses.replace(radio, look=own, skin="white" if own == "white" else "black")

    snap = await live_snapshot(hub, radio, ports, runner, locale="pt-BR", guild_radio_skin=guild_radio_skin)

    assert (snap.look, snap.skin) == (look, skin)


# ---------------------------------------------------------------------- carregador


@pytest.fixture
def muse_package(tmp_path, monkeypatch):
    package = tmp_path / "fake_muse_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    good, broken = JUKEBOX_IDS[0], JUKEBOX_IDS[1]
    (package / f"{good}.py").write_text(
        "class Skin:\n    name = preview = ''\n    def setup_features(self, player): pass\n"
        "    def load(self, player): return {}\n\ndef load():\n    return Skin()\n"
    )
    (package / f"{broken}.py").write_text("def load():\n    raise RuntimeError('quebrada')\n")
    (package / f"{JUKEBOX_IDS[2]}.py").write_text("import a_dependency_that_is_missing\n")  # falta DENTRO da skin
    (package / "not_in_the_registry.py").write_text("raise SystemExit('nunca importar')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(client_module, "MUSE_SKINS_PACKAGE", "fake_muse_pkg")
    yield good, broken
    for name in [m for m in sys.modules if m.startswith("fake_muse_pkg")]:
        del sys.modules[name]


def test_the_loader_registers_each_muse_skin_in_both_modes_and_only_registry_ids(muse_package, caplog):
    good, broken = muse_package
    pool = SimpleNamespace(player_skins={"default": object()}, player_static_skins={"default": object()})

    with caplog.at_level(logging.WARNING, logger=client_module.logger.name):
        BotPool.load_muse_skins(pool)

    assert pool.player_skins[good] is pool.player_static_skins[good]
    assert broken not in pool.player_skins and broken in caplog.text
    assert JUKEBOX_IDS[2] not in pool.player_skins and JUKEBOX_IDS[2] in caplog.text
    assert set(pool.player_skins) == {"default", good} == set(pool.player_static_skins)
    assert "fake_muse_pkg.not_in_the_registry" not in sys.modules


def test_without_the_muse_package_nothing_is_registered_and_nothing_is_logged(caplog):
    pool = SimpleNamespace(player_skins={}, player_static_skins={})

    with caplog.at_level(logging.WARNING, logger=client_module.logger.name):
        BotPool.load_muse_skins(pool)

    assert pool.player_skins == {} and caplog.text == ""
