"""Registro e configurações das skins 2.3 (``rules/skins.py``, o ``look`` da rádio e o documento global ``guilds``).

Preferência de aparência nunca invalida um documento; o campo antigo ``skin`` da rádio sai sempre com um valor que o rc
anterior aceita; documento antigo migra só pela regra de leitura; o padrão da Jukebox chega a quem nunca escolheu."""

import copy
from types import SimpleNamespace

import pytest

from empiremuse.rules import skins
from empiremuse.rules.radio import check_radio, radio_id, to_document
from empiremuse.rules.skins import (
    DEFAULT_JUKEBOX_SKIN,
    DEFAULT_RADIO_LOOK,
    JUKEBOX_IDS,
    RADIO_LOOKS,
    REGISTRY,
    Surface,
    disc_motion_on,
    global_skin_changes,
    jukebox_choice,
    jukebox_skin,
    saved_player_skins,
    legacy_radio_skin,
    radio_look,
)
from empiremuse.utils.client import BotPool
from empiremuse.utils.db import DBModel, global_db_models, update_values

pytestmark = pytest.mark.critical

RC4_RADIO_SKINS = ("black", "white")  # o que o rc.4 aceita no campo ``skin`` da rádio (``rules/radio.py:249-251`` dele)
RC4_GUILDS_MODEL = {  # o documento global ``guilds`` do rc.4 (``ver`` 1.5)
    "ver": 1.5,
    "prefix": "",
    "global_skin": False,
    "player_skin": None,
    "player_skin_static": None,
    "voice_channel_status": "",
    "voice_status_permanent": None,
    "custom_skins": {},
    "custom_skins_static": {},
    "listen_along_invites": {},
}


def radio_doc(**fields):
    return {"_id": radio_id(2, 500), "channel_id": "900", "kind": "live", "source": {}, **fields}


# ---------------------------------------------------------------------- registro


def test_the_registry_has_unique_ids_per_surface_and_the_defaults_are_in_it():
    for surface in Surface:
        ids = [entry.id for entry in REGISTRY if entry.surface is surface]
        assert len(ids) == len(set(ids))
    assert DEFAULT_JUKEBOX_SKIN in JUKEBOX_IDS and DEFAULT_RADIO_LOOK in RADIO_LOOKS
    assert {"black", "white"} <= set(RADIO_LOOKS)  # as de hoje continuam válidas


# ---------------------------------------------------------------------- rádio: resolução e legado


@pytest.mark.parametrize(
    ("look", "guild", "expected"),
    [
        ("text", "portrait_white", "text"),  # a própria vence
        (None, "portrait_white", "portrait_white"),  # sem a própria, a de todas as rádios
        (None, None, DEFAULT_RADIO_LOOK),
        ("../x", "white", "white"),  # desconhecida cai no próximo nível
        ("neon", "neon", DEFAULT_RADIO_LOOK),
        (7, ["black"], DEFAULT_RADIO_LOOK),  # tipo errado nunca levanta
    ],
)
def test_the_radio_look_is_its_own_then_all_radios_then_the_default(look, guild, expected):
    assert radio_look(look, guild) == expected


@pytest.mark.parametrize(
    ("raw", "look", "skin"),
    [
        ({"skin": "white"}, "white", "white"),  # documento antigo branco continua branco
        ({"skin": "black"}, None, "black"),  # antigo preto era o padrão de criação: segue a de todas as rádios
        ({}, None, "black"),
        ({"skin": "black", "look": "portrait_white"}, "portrait_white", "white"),
        ({"skin": "black", "look": "neon"}, None, "black"),  # ``look`` desconhecido não invalida o documento
        ({"skin": "white", "look": None}, None, "white"),
    ],
)
def test_reading_old_and_new_radio_documents(raw, look, skin):
    radio = check_radio(radio_doc(**raw))

    assert (radio.look, radio.skin) == (look, skin)


@pytest.mark.parametrize("look", [*RADIO_LOOKS, None])
def test_every_document_written_is_read_by_the_previous_rc_and_back_by_this_one(look):
    radio = check_radio(radio_doc(look=look))
    written = to_document(radio)

    assert written["skin"] in RC4_RADIO_SKINS
    assert written["skin"] == legacy_radio_skin(look)
    assert check_radio(written).look == look
    rewritten_by_rc4 = {k: v for k, v in written.items() if k != "look"}  # o rc.4 grava o documento sem a chave nova
    assert check_radio(rewritten_by_rc4).skin == written["skin"]


@pytest.mark.parametrize(
    ("look", "legacy"),
    [("white", "white"), ("portrait_white", "white"), ("portrait_black", "black"), ("text", "black"), (None, "black")],
)
def test_the_legacy_skin_is_white_only_for_a_light_look(look, legacy):
    assert legacy_radio_skin(look) == legacy


# ---------------------------------------------------------------------- documento global ``guilds``


def test_an_old_guild_document_migrates_to_never_chosen():
    old = {**RC4_GUILDS_MODEL, "prefix": "?", "player_skin": "mini"}

    migrated = update_values(copy.deepcopy(global_db_models[DBModel.guilds]), old)

    assert (migrated["radio_skin"], migrated["disc_motion"]) == (None, None)
    assert (migrated["prefix"], migrated["player_skin"]) == ("?", "mini")
    assert radio_look(None, migrated["radio_skin"]) == DEFAULT_RADIO_LOOK and disc_motion_on(migrated["disc_motion"])


def test_the_previous_rc_keeps_the_new_guild_fields_and_this_one_reads_them_back():
    new = {**copy.deepcopy(global_db_models[DBModel.guilds]), "radio_skin": "portrait_white", "disc_motion": False}

    seen_by_rc4 = update_values(copy.deepcopy(RC4_GUILDS_MODEL), new)
    seen_by_rc4["ver"] = RC4_GUILDS_MODEL["ver"]
    back = update_values(copy.deepcopy(global_db_models[DBModel.guilds]), seen_by_rc4)

    assert (back["radio_skin"], back["disc_motion"]) == ("portrait_white", False)


@pytest.mark.parametrize(("value", "on"), [(None, True), (True, True), (False, False), ("off", True)])
def test_disc_motion_is_on_unless_turned_off(value, on):
    assert disc_motion_on(value) is on


# ---------------------------------------------------------------------- Jukebox (D2 e Q1)


@pytest.mark.parametrize(
    ("q1", "saved", "choice"),
    [
        (True, "default", "default"),  # Q1 = sim: quem salvou a Clássica padrão a mantém
        (False, "default", None),  # Q1 = não: o "default" gravado é lido como nunca escolhido
        (True, None, None),
        (False, None, None),
        (False, "mini", "mini"),  # qualquer outra escolha passa
        (False, "> custom_skin: x", "> custom_skin: x"),
    ],
)
def test_what_counts_as_a_saved_jukebox_choice(monkeypatch, q1, saved, choice):
    monkeypatch.setattr(skins, "SAVED_DEFAULT_IS_A_CHOICE", q1)

    assert jukebox_choice(saved) == choice


@pytest.mark.parametrize(
    ("saved", "loaded", "expected"),
    [
        (None, True, DEFAULT_JUKEBOX_SKIN),  # nunca escolhido: a Expandida escura
        ("default", True, "default"),  # Q1 = sim
        ("muse_slim_white", True, "muse_slim_white"),
        ("sumiu", True, DEFAULT_JUKEBOX_SKIN),  # id desconhecido: o padrão
        ("> custom_skin: minha", True, "> custom_skin: minha"),  # personalizada mantida
        (DEFAULT_JUKEBOX_SKIN, False, "default"),  # rollback: o rc.4 não conhece a skin nova e cai no padrão dele
    ],
)
def test_the_jukebox_skin_a_player_gets(saved, loaded, expected):
    skins_loaded = {"default": object(), **({skin: object() for skin in JUKEBOX_IDS} if loaded else {})}
    pool = SimpleNamespace(player_skins=skins_loaded, default_skin=DEFAULT_JUKEBOX_SKIN if loaded else "default")

    assert BotPool.check_skin(pool, jukebox_choice(saved)) == expected


@pytest.mark.parametrize(
    ("q1", "saved", "shown", "global_field"),
    [
        (True, "default", "default", "default"),
        (False, "default", DEFAULT_JUKEBOX_SKIN, ""),  # Q1 = não: o "default" gravado aparece como nunca escolhido
        (False, None, DEFAULT_JUKEBOX_SKIN, ""),
        (False, "mini", "mini", "mini"),
    ],
)
def test_the_panel_skin_forms_follow_the_q1_switch(monkeypatch, q1, saved, shown, global_field):
    from empiremuse.panel.services.appearance import SkinsGlobalForm, SkinsServerForm

    monkeypatch.setattr(skins, "SAVED_DEFAULT_IS_A_CHOICE", q1)
    ctx = SimpleNamespace(
        pool=SimpleNamespace(default_skin=DEFAULT_JUKEBOX_SKIN, default_static_skin=DEFAULT_JUKEBOX_SKIN)
    )

    server = SkinsServerForm.display(
        SkinsServerForm.__new__(SkinsServerForm), ctx, {"skin": saved, "static_skin": saved}
    )
    shown_global = SkinsGlobalForm.display(
        SkinsGlobalForm.__new__(SkinsGlobalForm),
        ctx,
        {"global_skin": True, "player_skin": saved, "player_skin_static": saved},
    )

    assert jukebox_skin(saved, DEFAULT_JUKEBOX_SKIN) == shown
    assert server == {"skin": shown, "static_skin": shown}
    assert shown_global["player_skin"] == shown_global["player_skin_static"] == global_field


@pytest.mark.parametrize(
    ("folder", "listed"), [("normal_player", skins.CLASSIC_NORMAL), ("static_player", skins.CLASSIC_STATIC)]
)
def test_the_classic_lists_match_the_skin_files_of_each_mode(folder, listed):
    from pathlib import Path

    import empiremuse.utils.music.skins as package

    files = {
        path.stem for path in (Path(next(iter(package.__path__))) / folder).glob("*.py") if path.stem != "__init__"
    }

    assert sorted(listed) == sorted(files)


@pytest.mark.parametrize(
    ("q1", "global_mode", "global_skin", "expected"),
    [
        (False, True, "default", ("mini", "classic")),  # Q1 = não: o "default" global não esconde a skin do servidor
        (True, True, "default", ("default", "default")),
        (False, True, "slim", ("slim", "slim")),
        (False, True, None, ("mini", "classic")),
        (False, False, "slim", ("mini", "classic")),  # sem modo global, a global não conta
    ],
)
def test_the_new_player_skin_applies_q1_at_each_level_before_falling_back(
    monkeypatch, q1, global_mode, global_skin, expected
):
    monkeypatch.setattr(skins, "SAVED_DEFAULT_IS_A_CHOICE", q1)
    global_data = {"global_skin": global_mode, "player_skin": global_skin, "player_skin_static": global_skin}

    assert saved_player_skins("mini", "classic", global_data) == expected


@pytest.mark.parametrize(("q1", "expected"), [(False, (None, None)), (True, ("default", "default"))])
def test_a_never_chosen_server_skin_falls_to_the_registry_default(monkeypatch, q1, expected):
    monkeypatch.setattr(skins, "SAVED_DEFAULT_IS_A_CHOICE", q1)

    assert saved_player_skins("default", "default", {"global_skin": False}) == expected


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize(
    ("global_mode", "global_skin", "applied"),
    [
        (True, None, ("mini", "classic")),  # sem escolha global, o painel aplica a do servidor (como a criação)
        (True, "default", ("mini", "classic")),  # Q1 = não: o "default" global também cai na do servidor
        (True, "slim", ("slim", "slim")),
        (False, "slim", ("mini", "classic")),
    ],
)
async def test_the_panel_applies_skins_with_the_same_precedence_as_a_new_player(
    monkeypatch, global_mode, global_skin, applied
):
    from unittest.mock import AsyncMock

    from empiremuse.panel.services.appearance import SkinsGlobalForm

    monkeypatch.setattr(skins, "SAVED_DEFAULT_IS_A_CHOICE", False)
    servers = SimpleNamespace(settings=AsyncMock(return_value={"skin": "mini", "static_skin": "classic"}))
    ctx = SimpleNamespace(
        guild_id=1, pool=SimpleNamespace(default_skin="pool", default_static_skin="pool_static", servers=servers)
    )
    new = {"global_skin": global_mode, "player_skin": global_skin, "player_skin_static": global_skin}

    assert await SkinsGlobalForm._effective(ctx, new) == applied


@pytest.mark.regression
@pytest.mark.parametrize(
    ("shown", "picked", "written"),
    [
        (("mini", "classic"), ("mini", "classic"), {}),  # salvar sem trocar não grava a skin herdada como escolha
        (("mini", "classic"), ("slim", "classic"), {"player_skin": "slim"}),
        (("mini", "classic"), ("mini", "slim"), {"player_skin_static": "slim"}),
    ],
)
def test_the_global_skin_command_writes_only_what_was_changed(shown, picked, written):
    assert global_skin_changes(shown, picked) == written
