"""Regras puras do documento único por servidor: modelo, validação das alterações e a migração só do principal (falha fechada)."""

import pytest

from empiremuse.rules.limits import CUSTOM_SKIN_PREFIX, MAX_DJ_ROLES
from empiremuse.rules.servers import (
    SETTINGS_FIELDS,
    Allowed,
    SettingError,
    changed_settings,
    default_settings,
    is_main_identifier,
    migrate_settings,
    new_doc,
    jukebox_auto_delete,
    settings_from_legacy,
    to_legacy,
    validate_changes,
)

pytestmark = pytest.mark.critical

ALLOWED = Allowed(skins=frozenset({"default"}), static_skins=frozenset({"static"}), role_ids=frozenset({"10", "20"}))


def legacy(**fields):
    """Documento antigo de um bot: as skins ficam em ``player_controller``, o resto na raiz."""
    controller = {k: fields.pop(k) for k in ("skin", "static_skin") if k in fields}
    return {**fields, "player_controller": controller}


# ---------------------------------------------------------------------- modelo


def test_new_document_starts_with_defaults_and_nothing_migrated():
    doc = new_doc()
    assert doc["settings"] == default_settings() and set(default_settings()) == set(SETTINGS_FIELDS)
    assert doc["rev"] == 0 and doc["migrated"] == {"settings": False, "jukebox": False, "playlists": False}


@pytest.mark.parametrize(
    ("identifier", "is_main"),
    [("TOKEN", True), ("TOKEN_1", True), ("TOKEN_2", False), ("token", False), ("", False), (None, False)],
)
def test_only_the_main_token_identifies_the_main_bot(identifier, is_main):
    assert is_main_identifier(identifier) is is_main


# ---------------------------------------------------------------------- validação


@pytest.mark.parametrize(
    "changes",
    [
        {"volume": 50},  # campo desconhecido
        {"default_player_volume": 101},
        {"default_player_volume": True},
        {"autoplay": "sim"},
        {"enable_restrict_mode": 1},
        {"skin": "> inexistente"},
        {"static_skin": ""},
        {"djroles": "10"},
        {"djroles": ["10", "10"]},
        {"djroles": ["abc"]},
        {"djroles": [True]},
        {"djroles": [str(n) for n in range(MAX_DJ_ROLES + 1)]},
    ],
)
def test_invalid_changes_are_refused(changes):
    with pytest.raises(SettingError):
        validate_changes(changes, ALLOWED)


def test_valid_changes_are_normalized():
    changes = {
        "default_player_volume": 50,
        "autoplay": True,
        "skin": f"{CUSTOM_SKIN_PREFIX}minha",
        "static_skin": None,
        "djroles": [10, "20"],
    }
    assert validate_changes(changes, ALLOWED) == changes | {"djroles": ["10", "20"]}


# ---------------------------------------------------------------------- migração: só o principal, valor inválido → padrão


def test_missing_document_migrates_to_the_defaults():
    assert migrate_settings(None, ALLOWED) == default_settings()


def test_valid_main_document_is_copied_field_by_field():
    old = legacy(
        djroles=[10, "20"], default_player_volume=40, autoplay=True, enable_restrict_mode=True,
        check_other_bots_in_vc=True, skin="default", static_skin="static",
    )  # fmt: skip
    assert migrate_settings(old, ALLOWED) == {
        "djroles": ["10", "20"], "default_player_volume": 40, "autoplay": True, "enable_restrict_mode": True,
        "check_other_bots_in_vc": True, "skin": "default", "static_skin": "static", "language": "pt-BR",
        "jukebox_auto_delete": False,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("default_player_volume", 7),
        ("default_player_volume", "100"),
        ("autoplay", "sim"),
        ("check_other_bots_in_vc", 1),
        ("skin", "> some_skin_que_nao_existe"),
        ("static_skin", 42),
    ],
)
def test_invalid_value_falls_back_to_the_default_and_keeps_the_rest(field, invalid):
    other = "check_other_bots_in_vc" if field == "autoplay" else "autoplay"
    settings = migrate_settings(legacy(**{field: invalid, other: True}), ALLOWED)
    assert settings == default_settings() | {other: True, "language": "pt-BR"}


@pytest.mark.parametrize("invalid", ["sim", 1, 0, None, [], "False"])
def test_invalid_restrict_mode_fails_closed(invalid):
    assert migrate_settings(legacy(enable_restrict_mode=invalid), ALLOWED)["enable_restrict_mode"] is True


def test_absent_restrict_mode_stays_the_default():
    assert migrate_settings(legacy(autoplay=True), ALLOWED)["enable_restrict_mode"] is False


def test_djroles_drop_only_the_bad_ids():
    old = legacy(djroles=[10, "abc", "20", 10, "999", True, None])
    assert migrate_settings(old, ALLOWED)["djroles"] == ["10", "20"]  # malformado, repetido e cargo apagado saem


def test_djroles_that_are_not_a_list_become_empty():
    assert migrate_settings(legacy(djroles="10"), ALLOWED)["djroles"] == []


# ---------------------------------------------------------------------- fachada no formato antigo


def test_legacy_view_round_trips_and_detects_a_writer_that_changed_settings():
    base = {"_id": "1", "player_controller": {"channel": "5", "message_id": "6", "skin": "velha"}}
    settings = default_settings() | {"skin": "default", "autoplay": True, "djroles": ["10"]}
    flat = to_legacy(base, settings)
    assert flat["player_controller"] == {"channel": "5", "message_id": "6", "skin": "default", "static_skin": None}
    assert settings_from_legacy(flat) == settings
    assert changed_settings(flat, settings) == {}
    flat["autoplay"], flat["player_controller"]["skin"] = False, "outra"
    assert changed_settings(flat, settings) == {"autoplay": False, "skin": "outra"}
    assert base["player_controller"]["skin"] == "velha"  # a base não é alterada


# ---------------------------------------------------------------------- idioma (item 21)

LANGUAGES = Allowed(languages=frozenset({"en-US", "pt-BR", "de"}))


def test_the_language_is_a_setting_field_and_the_default_is_automatic():
    assert (
        "language" in SETTINGS_FIELDS
        and default_settings()["language"] is None
        and new_doc()["settings"]["language"] is None
    )


@pytest.mark.parametrize("value", [None, "pt-BR", "en-US", "de"])
def test_an_enabled_language_or_none_is_accepted(value):
    assert validate_changes({"language": value}, LANGUAGES) == {"language": value}


@pytest.mark.parametrize("value", ["pt-br", "en-US ", "../x", "fr", "", "auto", 5, True, ["pt-BR"], "pt-BR\n"])
def test_anything_but_an_exact_enabled_locale_is_refused(value):
    with pytest.raises(SettingError):
        validate_changes({"language": value}, LANGUAGES)


def test_no_enabled_language_means_only_none_is_valid():
    assert validate_changes({"language": None}, Allowed()) == {"language": None}
    with pytest.raises(SettingError):
        validate_changes({"language": "pt-BR"}, Allowed())


def test_an_old_document_migrates_to_pt_br_and_a_missing_one_stays_automatic():
    assert migrate_settings(legacy(autoplay=True), ALLOWED)["language"] == "pt-BR"
    assert migrate_settings(None, ALLOWED)["language"] is None


def test_the_old_format_never_sees_or_writes_the_language():
    assert settings_from_legacy({"language": "de"})["language"] is None  # o formato antigo nunca lê o campo
    flat = to_legacy({}, default_settings() | {"language": "de"}, None)

    assert "language" not in flat and "language" not in flat.get("player_controller", {})
    assert changed_settings(flat, {}).get("language") is None


# ---------------------------------------------------------------------- Jukebox estrita (item 8, J1)


def test_the_strict_channel_is_off_by_default_and_unknown_to_the_legacy_format():
    assert default_settings()["jukebox_auto_delete"] is False and new_doc()["settings"]["jukebox_auto_delete"] is False
    assert "jukebox_auto_delete" not in to_legacy({}, default_settings())
    assert migrate_settings(legacy(autoplay=True), ALLOWED)["jukebox_auto_delete"] is False


@pytest.mark.parametrize("value", [True, False])
def test_the_strict_flag_accepts_only_booleans(value):
    assert validate_changes({"jukebox_auto_delete": value}, ALLOWED) == {"jukebox_auto_delete": value}


@pytest.mark.parametrize("value", [1, 0, "true", None, [], "sim"])
def test_the_strict_flag_refuses_anything_that_is_not_a_boolean(value):
    with pytest.raises(SettingError):
        validate_changes({"jukebox_auto_delete": value}, ALLOWED)


def test_a_document_without_the_key_is_not_strict():
    assert jukebox_auto_delete({}) is False
    assert jukebox_auto_delete({"jukebox_auto_delete": "true"}) is False
    assert jukebox_auto_delete({"jukebox_auto_delete": True}) is True
