"""Regras puras das playlists do servidor (item 17, A1-A4, A6, A9): arquivo inválido não grava, importar só acrescenta, formato
antigo, migração só do principal, exportar → importar volta a mesma lista, permissão e texto de terceiros no relatório."""

import json

import pytest

from empiremuse.rules.errors import FILE_FORMAT, RuleError
from empiremuse.rules.limits import MAX_SERVER_PLAYLISTS, PLAYLIST_FILE_MAX_BYTES, PLAYLIST_FILE_MAX_ITEMS
from empiremuse.rules.playlists import (
    DUP_IN_FILE,
    DUP_NAME,
    DUP_URL,
    INVALID,
    QUOTA_FULL,
    Playlist,
    add_item,
    check_import_filename,
    check_playlist,
    edit_item,
    clean_stored_url,
    format_report,
    may_manage,
    normalize_url,
    parse_file,
    plan_import,
    plan_migration,
    select_description,
    to_export,
)

pytestmark = pytest.mark.critical

BASE = "https://exemplo.com/"


def item(name, order=0, url=None, description="uma descrição", genres=(), icon=None):
    return Playlist(order, name, description, tuple(genres), url or f"{BASE}{name.lower().replace(' ', '-')}", icon)


def parse(document, **kwargs):
    raw = document if isinstance(document, (str, bytes)) else json.dumps(document)
    return parse_file(raw, max_bytes=kwargs.pop("max_bytes", PLAYLIST_FILE_MAX_BYTES), **kwargs)


# ---------------------------------------------------------------------- A1: arquivo inválido não grava


@pytest.mark.parametrize(
    "raw",
    [
        "não é json",
        "[1, 2]",
        '"texto"',
        b"\xff\xfe\x00",  # UTF-8 estrito
        '{"playlists": [{"name": "Rock", "url": "https://exemplo.com/r", "x": NaN}]}',  # NaN recusado
        '{"playlists": [{"name": "Rock", "name": "Pop", "url": "https://exemplo.com/r"}]}',  # chave repetida num item
    ],
)
def test_a_file_with_a_bad_structure_is_an_error_and_nothing_is_read(raw):
    assert isinstance(parse(raw), RuleError)


def test_a_json_nested_deep_enough_to_overflow_the_parser_is_invalid_not_a_crash():
    assert parse("[" * 500_000 + "]" * 500_000, max_bytes=10**7).code == "file_format"


def test_size_and_item_count_limits_are_enforced_before_anything_else():
    too_big = json.dumps({"playlists": [], "pad": "x" * PLAYLIST_FILE_MAX_BYTES})
    assert parse(too_big).code == "file_too_large"
    many = {"playlists": [{"name": f"Lista {n}", "url": f"{BASE}{n}"} for n in range(PLAYLIST_FILE_MAX_ITEMS + 1)]}
    assert parse(many).code == "file_too_many"


def test_a_bad_item_is_reported_and_the_good_ones_are_kept_and_unknown_keys_are_ignored():
    parsed = parse(
        {
            "playlists": [
                {"name": "Rock", "url": f"{BASE}rock", "extra": 1},
                {"name": "x", "url": f"{BASE}curto"},  # nome curto demais
                {"name": "Pop", "url": "javascript:alert(1)"},
                {"name": "Jazz", "url": f"{BASE}jazz", "order": True},  # bool não é order
                "texto solto",
            ]
        }
    )
    assert [p.name for p in parsed.items] == ["Rock"]
    assert [s.reason for s in parsed.invalid] == [INVALID] * 4


def test_a_bom_is_accepted():
    raw = b"\xef\xbb\xbf" + json.dumps({"playlists": [{"name": "Rock", "url": f"{BASE}r"}]}).encode()
    assert [p.name for p in parse(raw).items] == ["Rock"]


@pytest.mark.parametrize("name", ["Ro‮k", "Ro\nck", "Ro​ck", "Ro\x00ck", " ", 42, None])
def test_names_with_control_characters_are_refused(name):
    assert isinstance(check_playlist({"name": name, "url": f"{BASE}rock"}), RuleError)


def test_a_zero_width_joiner_is_allowed_only_inside_a_composed_emoji():
    assert isinstance(check_playlist({"name": "👮‍♂️ Polícia", "url": f"{BASE}p"}), Playlist)
    assert isinstance(check_playlist({"name": "Rock‍", "url": f"{BASE}p"}), RuleError)


def test_the_icon_is_one_emoji_from_any_source_and_anything_else_is_dropped_without_refusing_the_item():
    def icon_of(value):
        document = {"playlists": [{"name": "Rock", "url": f"{BASE}rock", "icon": value}]}
        return parse(document).items[0].icon

    assert icon_of("🎧") == "🎧"
    assert icon_of("👮\u200d♂️") == "👮\u200d♂️"
    for invalid in ("icons/rock.png", "../../etc/passwd", "<:rock:123456789>", "🎧🎶", "rock", "", None, 7):
        assert icon_of(invalid) is None


def test_adding_and_editing_take_the_icon_of_the_item_and_none_clears_it():
    assert add_item([], item("Rock", icon="🎧"))[0].icon == "🎧"
    stored = [item("Rock", 1, icon="🎧")]
    edited = edit_item(stored, "rock", item("Rock Clássico", 1, url=f"{BASE}novo", icon="🔥"))
    assert [(p.name, p.icon) for p in edited] == [("Rock Clássico", "🔥")]
    cleared = edit_item(stored, "rock", item("Rock", 1, icon=None))
    assert cleared[0].icon is None


# ---------------------------------------------------------------------- A3: formato antigo


def test_the_old_format_is_accepted_in_dictionary_order_and_a_repeated_name_is_reported():
    raw = (
        '{"Rock": {"url": "https://exemplo.com/rock", "description": "clássicos"},'
        ' "Pop": {"url": "https://exemplo.com/pop", "description": "hits do ano"},'
        ' "Rock": {"url": "https://exemplo.com/rock2", "description": "outra"}}'
    )
    parsed = parse(raw)
    assert [(p.order, p.name, p.url) for p in parsed.items] == [(1, "Rock", f"{BASE}rock"), (2, "Pop", f"{BASE}pop")]
    assert [(s.name, s.reason) for s in parsed.invalid][:1] == [("Rock", DUP_IN_FILE)]


# ---------------------------------------------------------------------- A2: importar só acrescenta


def test_import_never_changes_existing_items_and_appends_after_the_highest_order_in_file_order():
    existing = [item("Rock", 1), item("Pop", 5)]
    report = plan_import(existing, [item("Jazz"), item("Blues")])
    assert [(p.name, p.order) for p in report.added] == [("Jazz", 6), ("Blues", 7)]
    assert report.skipped == ()
    assert existing == [item("Rock", 1), item("Pop", 5)]


@pytest.mark.parametrize(
    ("incoming", "reason"),
    [
        (item("ROCK", url=f"{BASE}outra"), DUP_NAME),  # nome por casefold
        (item("Outra", url="HTTPS://www.Exemplo.com/rock/?si=x&utm_source=y#t"), DUP_URL),  # URL normalizada
    ],
)
def test_duplicates_by_name_or_normalized_url_go_to_the_report_with_the_reason(incoming, reason):
    report = plan_import([item("Rock", 1)], [incoming])
    assert report.added == () and [(s.name, s.reason) for s in report.skipped] == [(incoming.name, reason)]


def test_duplicates_inside_the_file_and_a_full_quota_are_reported():
    report = plan_import([], [item("Rock"), item("rock", url=f"{BASE}x"), item("Outro", url=f"{BASE}rock")])
    assert [p.name for p in report.added] == ["Rock"]
    assert [s.reason for s in report.skipped] == [DUP_IN_FILE, DUP_IN_FILE]

    full = [item(f"Lista {n}", n) for n in range(1, MAX_SERVER_PLAYLISTS + 1)]
    over = plan_import(full, [item("Nova")])
    assert over.added == () and [s.reason for s in over.skipped] == [QUOTA_FULL]
    assert isinstance(add_item(full, item("Nova")), RuleError)  # adicionar um a um também respeita a cota


def test_stored_urls_lose_tracking_parameters_but_keep_the_list():
    assert clean_stored_url("https://youtu.be/x?si=a&list=PL1&utm_campaign=z&feature=s&pp=q&drli=1") == (
        "https://youtu.be/x?list=PL1&drli=1"
    )
    assert normalize_url("https://a.com/p?b=2&a=1") == normalize_url("HTTPS://WWW.A.COM/p/?a=1&b=2&si=x#f")
    assert check_playlist({"name": "Rock", "url": "https://youtu.be/x?si=abc&list=PL12345"}).url == (
        "https://youtu.be/x?list=PL12345"
    )


# ---------------------------------------------------------------------- A4: migração só do principal


def test_migration_keeps_dictionary_order_skips_repeats_and_invalid_items_and_ignores_the_quota():
    legacy = {
        "Rock": {"url": f"{BASE}rock", "description": "clássicos"},
        "rock": {"url": f"{BASE}rock-2", "description": "mesmo nome"},
        "Pop": {"url": f"{BASE}rock/", "description": "mesma URL normalizada"},
        "Ruim": {"url": "não é url", "description": "inválido"},
        "Sem dict": "texto",
        **{f"Lista {n}": {"url": f"{BASE}l{n}", "description": "mais uma"} for n in range(MAX_SERVER_PLAYLISTS + 5)},
    }
    plan = plan_migration(legacy)
    assert [p.name for p in plan.items][:2] == ["Rock", "Lista 0"] and (plan.skipped, plan.invalid) == (2, 2)
    assert len(plan.items) == MAX_SERVER_PLAYLISTS + 6  # D4: a migração mantém tudo, mesmo acima de 25
    assert [p.order for p in plan.items] == list(range(1, len(plan.items) + 1))


@pytest.mark.parametrize("legacy", [None, {}, [], "texto", 42])
def test_migration_of_a_missing_or_broken_document_is_empty(legacy):
    assert plan_migration(legacy).items == ()


# ---------------------------------------------------------------------- A9: exportar → importar


def test_export_then_import_into_an_empty_server_gives_the_same_list_with_icons():
    original = [
        item("Rock", 1, genres=["Rock", "Anos 80"], icon="🎧"),
        item("Pop", 2, description=""),
        item("Jazz café", 3, url=f"{BASE}j?list=PL1"),
    ]
    exported = json.dumps(to_export(original), ensure_ascii=False)
    assert '"icon": "🎧"' in exported

    imported = plan_import([], parse(exported).items)
    assert imported.skipped == ()
    assert imported.added == tuple(original)


# ---------------------------------------------------------------------- A7: nome de arquivo do bot


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("rock-80.json", True),
        ("nao-listado.json", False),  # bem formado, mas fora da lista dos que existem
        ("../rock-80.json", False),
        ("sub/rock-80.json", False),
        ("Rock-80.json", False),
        ("rock-80.json ", False),
        ("rock_80.json", False),
        ("a" * 65 + ".json", False),
        (".json", False),
        ("", False),
        (None, False),
    ],
)
def test_an_import_file_name_must_be_well_formed_and_one_of_the_listed_files(name, ok):
    listed = {"rock-80.json", "../rock-80.json", "sub/rock-80.json", "Rock-80.json", "rock-80.json ", "rock_80.json"}
    assert (check_import_filename(name, listed) == name) is ok


# ---------------------------------------------------------------------- A6: permissão


@pytest.mark.parametrize(
    ("owner", "admin", "manage", "allowed"),
    [(True, False, False, True), (False, True, False, True), (False, False, True, True), (False, False, False, False)],
)
def test_only_the_server_owner_administrator_or_manage_guild_may_manage(owner, admin, manage, allowed):
    assert may_manage(owner, admin, manage) is allowed


# ---------------------------------------------------------------------- P-V4: texto de terceiros


def test_report_escapes_third_party_names_and_never_passes_2000_characters():
    hostile = item("@everyone <@&5> [x](http://e.vil)", url=f"{BASE}h")
    text = format_report(plan_import([item("Rock", 1, url=f"{BASE}h")], [hostile]))
    assert "@everyone" not in text and "<@&5>" not in text and "](http" not in text

    police = item("Midnight", url=f"{BASE}m", icon="👮\u200d♂️")
    assert "👮\u200d♂️ Midnight" in format_report(plan_import([], [police]))  # o ZWJ do ícone sobrevive ao relatório

    many = plan_import([item("Rock", 1)], [item(f"Rock {n}", url=f"{BASE}rock") for n in range(300)])
    long_text = format_report(many)
    assert len(long_text) <= 2000 and long_text.rstrip().split("\n")[-1].startswith("+")


def test_select_descriptions_are_cut_at_100_characters_with_an_ellipsis():
    assert select_description("x" * 100) == "x" * 100
    assert len(select_description("x" * 101)) == 100 and select_description("x" * 250).endswith("…")


@pytest.mark.regression
def test_pasted_text_with_a_lone_surrogate_is_a_format_error_not_a_crash():
    result = parse_file('{"playlists": [{"name": "Rock \ud800", "url": "https://exemplo.com/rock"}]}', max_bytes=65536)
    assert isinstance(result, RuleError) and result.code == FILE_FORMAT
