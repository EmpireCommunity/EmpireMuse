"""Playlists do servidor no store (item 17, A1, A2, A4, A5, A9): migração só do principal, falha que não grava, escritas
simultâneas sem perda, versão velha recusada e o embed ocioso só depois de gravar. Reaproveita o banco falso do documento único."""

import asyncio
import json
import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from empiremuse.rules.errors import RuleError
from empiremuse.rules.limits import MAX_SERVER_PLAYLISTS, PLAYLIST_FILE_MAX_BYTES, PLAYLIST_PASTE_MAX
from empiremuse.rules.servers import new_doc
from empiremuse.rules.playlists import Playlist, check_attachment, check_paste, parse_file
from empiremuse.utils.db import DBModel
from empiremuse.utils.server_playlists import select_options
from empiremuse.utils.server_playlists_files import list_import_files, read_import_file
from empiremuse.utils.servers import ServerConfigConflict, ServerConfigUnavailable, ServerStore, UnknownGuild
from tests.persistence.test_servers_store import GID, MAIN_ID, RESERVE_ID, SERVERS, Banco, make_bot

pytestmark = pytest.mark.critical

OTHER = 222
BASE = "https://exemplo.com/"
SECRET_URL = "https://exemplo.com/segredo-do-servidor"


def run(coro):
    return asyncio.run(coro)


def item(name, order=0, url=None, description="uma descrição", genres=()):
    return Playlist(order, name, description, tuple(genres), url or f"{BASE}{name.lower().replace(' ', '-')}", None)


def test_the_select_option_emoji_is_the_icon_or_else_the_platform_one(monkeypatch):
    monkeypatch.setattr("empiremuse.utils.others.music_source_emoji_url", lambda url, bot: ("🔊",))
    with_icon = Playlist(1, "Rock", "uma descrição", (), f"{BASE}rock", "🎧")
    without = Playlist(2, "Pop", "uma descrição", (), f"{BASE}pop", None)

    options = select_options(None, (with_icon, without))

    assert [option.emoji.name for option in options] == ["🎧", "🔊"]


def legacy_doc(links):
    return {"_id": str(GID), "player_controller": {"channel": "5", "message_id": "6", "fav_links": links}}


def link(url, description="clássicos"):
    return {"url": url, "description": description}


def parsed(*names):
    raw = json.dumps({"playlists": [{"name": n, "url": f"{BASE}{n.lower()}"} for n in names]})
    return parse_file(raw, max_bytes=PLAYLIST_FILE_MAX_BYTES)


@pytest.fixture
def world():
    banco = Banco()
    bots = [make_bot(MAIN_ID, "TOKEN", guilds=(GID, OTHER)), make_bot(RESERVE_ID, "TOKEN_2", guilds=(GID, OTHER))]
    for bot in bots:
        bot.music = SimpleNamespace(players={GID: 1, OTHER: 1})  # tocando: o embed ocioso não tem o que atualizar
    pool = SimpleNamespace(database=banco, get_all_bots=lambda: bots)
    store = ServerStore(pool)
    return SimpleNamespace(banco=banco, bots=bots, pool=pool, store=store, playlists=store.playlists)


def names(playlists):
    return [p.name for p in playlists]


# ---------------------------------------------------------------------- A4: migração só do principal


def test_migration_copies_only_the_main_document_skips_repeats_and_keeps_the_old_data(world):
    main_links = {
        "Rock": link(f"{BASE}rock"),
        "rock": link(f"{BASE}rock-2"),  # mesmo nome
        "Pop": link(f"{BASE}rock/"),  # mesma URL normalizada
        "Jazz": link(f"{BASE}jazz"),
    }
    world.banco.put(DBModel.guilds, str(MAIN_ID), GID, legacy_doc(main_links))
    world.banco.put(DBModel.guilds, str(RESERVE_ID), GID, legacy_doc({"Reserva": link(f"{BASE}reserva")}))
    before = deepcopy(world.banco.docs[(DBModel.guilds, str(MAIN_ID), str(GID))])

    assert names(run(world.playlists.list(GID))) == ["Rock", "Jazz"]

    doc = world.banco.servers_doc()
    assert doc["migrated"]["playlists"] is True and [p["order"] for p in doc["playlists"]] == [1, 2]
    assert world.banco.docs[(DBModel.guilds, str(MAIN_ID), str(GID))] == before  # o doc do principal fica (D11-7)
    assert (DBModel.guilds, str(RESERVE_ID), str(GID)) in world.banco.docs  # e o da reserva nem foi lido para mudar


def test_migration_keeps_everything_even_above_the_quota_and_then_accepts_nothing_new(world):  # D4
    many = {f"Lista {n}": link(f"{BASE}l{n}") for n in range(MAX_SERVER_PLAYLISTS + 5)}
    world.banco.put(DBModel.guilds, str(MAIN_ID), GID, legacy_doc(many))
    assert len(run(world.playlists.list(GID))) == MAX_SERVER_PLAYLISTS + 5
    assert run(world.playlists.add(GID, item("Nova"))).code == "quota_full"


def test_two_boots_give_the_same_list_and_the_second_writes_nothing(world):
    world.banco.put(DBModel.guilds, str(MAIN_ID), GID, legacy_doc({"Rock": link(f"{BASE}rock")}))
    first = run(world.playlists.list(GID))
    saves = len(world.banco.saves)

    assert run(ServerStore(world.pool).playlists.list(GID)) == first and len(world.banco.saves) == saves


def test_a_missing_main_document_migrates_to_an_empty_list_without_creating_the_bots_document(world):
    assert run(world.playlists.list(GID)) == ()
    assert world.banco.servers_doc()["migrated"]["playlists"] is True
    assert (DBModel.guilds, str(MAIN_ID), str(GID)) not in world.banco.docs


@pytest.mark.parametrize("cause", ["read_error", "main_offline"])
def test_migration_that_cannot_decide_raises_writes_nothing_and_retries_later(world, cause):
    world.banco.put(DBModel.guilds, str(MAIN_ID), GID, legacy_doc({"Rock": link(f"{BASE}rock")}))
    if cause == "read_error":
        world.banco.read_error = True
    else:
        world.bots[0].user = None

    with pytest.raises(ServerConfigUnavailable):
        run(world.playlists.list(GID))
    assert world.banco.servers_doc() is None and world.banco.saves == []

    world.banco.read_error, world.bots[0].user = False, SimpleNamespace(id=MAIN_ID)
    assert names(run(world.playlists.list(GID))) == ["Rock"]  # nunca a lista vazia do modelo


def test_the_migration_log_has_only_counts_never_names_or_urls(world, caplog):  # P-V1
    world.banco.put(DBModel.guilds, str(MAIN_ID), GID, legacy_doc({"Segredo": link(SECRET_URL)}))
    with caplog.at_level(logging.INFO):
        run(world.playlists.list(GID))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "playlists copiadas" in text and "Segredo" not in text and SECRET_URL not in text


# ---------------------------------------------------------------------- falha de escrita: nada muda


def test_a_failed_write_changes_neither_the_list_nor_the_migration_flag_and_gives_no_report(world):
    world.banco.put(DBModel.guilds, str(MAIN_ID), GID, legacy_doc({"Rock": link(f"{BASE}rock")}))
    run(world.playlists.list(GID))
    before = deepcopy(world.banco.servers_doc())

    world.banco.write_error = True
    with pytest.raises(ServerConfigUnavailable):
        run(world.playlists.import_file(GID, parsed("Novo")))  # sem relatório: só existe depois de gravar
    with pytest.raises(ServerConfigUnavailable):
        run(world.playlists.add(GID, item("Outro")))
    world.banco.write_error = False

    assert world.banco.servers_doc() == before and names(run(world.playlists.list(GID))) == ["Rock"]


def test_a_failed_first_write_leaves_the_server_unmigrated(world):
    world.banco.put(DBModel.guilds, str(MAIN_ID), GID, legacy_doc({"Rock": link(f"{BASE}rock")}))
    world.banco.write_error = True
    with pytest.raises(ServerConfigUnavailable):
        run(world.playlists.add(GID, item("Novo")))
    world.banco.write_error = False
    assert world.banco.servers_doc() is None and names(run(world.playlists.list(GID))) == ["Rock"]  # sem o 'Novo'


# ---------------------------------------------------------------------- A2/A1: importar só acrescenta e o que é inválido não grava


def test_import_through_the_store_never_changes_existing_items_and_reports_why_each_was_skipped(world):
    run(world.playlists.add(GID, item("Rock", url=f"{BASE}rock")))
    existing = run(world.playlists.list(GID))
    raw = json.dumps(
        {
            "playlists": [
                {"name": "ROCK", "url": f"{BASE}outra"},  # nome
                {"name": "Copia", "url": f"{BASE}rock/"},  # URL
                {"name": "x", "url": f"{BASE}curto"},  # inválido no arquivo
                {"name": "Novo", "url": f"{BASE}novo"},
            ]
        }
    )

    report = run(world.playlists.import_file(GID, parse_file(raw, max_bytes=PLAYLIST_FILE_MAX_BYTES)))

    assert names(report.added) == ["Novo"]
    assert sorted(s.reason for s in report.skipped) == ["dup_name", "dup_url", "invalid"]
    assert run(world.playlists.list(GID))[0] == existing[0] and names(run(world.playlists.list(GID))) == [
        "Rock",
        "Novo",
    ]


def test_an_import_that_adds_nothing_does_not_write(world):
    run(world.playlists.add(GID, item("Rock")))
    rev = run(world.playlists.ver(GID))
    report = run(world.playlists.import_file(GID, parsed("rock")))
    assert report.added == () and run(world.playlists.ver(GID)) == rev


@pytest.mark.parametrize(
    "bad",
    [item("x"), item("Nome‮ruim"), item("Ok", url="javascript:alert(1)aaaa"), item("Ok", description="d")],
    ids=["short_name", "bidi_name", "bad_url", "short_description"],
)
def test_invalid_items_come_back_as_a_rule_error_and_nothing_is_written(world, bad):
    run(world.playlists.list(GID))
    saves = len(world.banco.saves)
    assert isinstance(run(world.playlists.add(GID, bad)), RuleError) and len(world.banco.saves) == saves


def test_duplicates_quota_and_unknown_names_are_rule_errors_not_exceptions(world):
    for n in range(MAX_SERVER_PLAYLISTS):
        assert isinstance(run(world.playlists.add(GID, item(f"Lista {n}"))), Playlist)
    saves = len(world.banco.saves)

    assert run(world.playlists.add(GID, item("Outra"))).code == "quota_full"
    assert run(world.playlists.edit(GID, "Lista 1", item("lista 2", url=f"{BASE}x"))).code == "name_taken"
    assert run(world.playlists.edit(GID, "não existe", item("Qualquer"))).code == "playlist_not_found"
    assert run(world.playlists.remove(GID, "não existe")).code == "playlist_not_found"
    assert len(world.banco.saves) == saves


def test_the_icon_is_stored_when_it_is_an_emoji_and_dropped_when_it_is_not(world):
    run(world.playlists.add(GID, item("Rock")._replace(icon="icons/hack.png")))
    assert run(world.playlists.find(GID, "rock")).icon is None

    run(world.playlists.edit(GID, "Rock", item("Rock Novo", 1, url=f"{BASE}novo")._replace(icon="🎧")))
    assert run(world.playlists.find(GID, "rock novo")).icon == "🎧"

    world.banco.servers_doc()["playlists"][0]["icon"] = "icons/rock.png"  # formato antigo já gravado
    world.store._docs.clear()
    assert run(world.playlists.find(GID, "rock novo")).icon is None


def test_the_first_read_migration_already_moves_the_leading_emoji(world):
    world.banco.put(DBModel.guilds, str(MAIN_ID), GID, legacy_doc({"🔴 Red Line": link(f"{BASE}red")}))

    playlist = run(world.playlists.list(GID))[0]

    assert (playlist.name, playlist.icon) == ("Red Line", "🔴")


def test_the_boot_migration_moves_only_the_leading_emoji_and_runs_once(world, caplog):
    stored = {**new_doc(), "_id": str(GID)}
    stored["migrated"] = {section: True for section in stored["migrated"]}
    stored["rev"] = 4
    stored["playlists"] = [
        {**item("🔴 Red Line", 1)._asdict(), "genres": []},
        {**item("Funk G[old] 🎶", 2)._asdict(), "genres": []},  # emoji no fim: fica no nome
        {**item("🎲 Domingo", 3)._asdict(), "genres": [], "icon": "🎧"},  # já tem ícone: intocado
        {**item("Rock ", 4)._asdict(), "genres": []},  # o nome é lido aparado
        {**item("🔴 Rock", 5, url=f"{BASE}rock2")._asdict(), "genres": []},  # ficaria igual a "Rock": não muda
    ]
    world.banco.put(*SERVERS, GID, stored)

    with caplog.at_level(logging.INFO):
        assert run(world.playlists.migrate_icons()) == (1, 1, (GID,))

    saved = world.banco.servers_doc()["playlists"]
    assert [(p["name"], p.get("icon")) for p in saved] == [
        ("Red Line", "🔴"),
        ("Funk G[old] 🎶", None),
        ("🎲 Domingo", "🎧"),
        ("Rock ", None),
        ("🔴 Rock", None),
    ]
    assert world.banco.servers_doc()["rev"] == 5
    assert "Red Line" not in caplog.text and "Funk" not in caplog.text  # o log leva só contagens

    assert run(world.playlists.migrate_icons()) == (0, 0, ())  # segunda vez: nada muda
    assert world.banco.servers_doc()["rev"] == 5 and len(world.banco.saves) == 1


def test_a_select_value_from_before_the_migration_still_finds_the_playlist(world):
    """Regressão: a mensagem montada na 2.1.3 guarda ``> pin: 🔴 Red Line``; a migração renomeou o item para ``Red Line`` + ícone."""
    stored = {**new_doc(), "_id": str(GID)}
    stored["migrated"] = {section: True for section in stored["migrated"]}
    stored["playlists"] = [
        {**item("🔴 Red Line", 1, url=f"{BASE}red")._asdict(), "genres": []},
        {**item("Rock", 2, url=f"{BASE}rock")._asdict(), "genres": []},
    ]
    world.banco.put(*SERVERS, GID, stored)
    run(world.playlists.migrate_icons())

    assert run(world.playlists.find(GID, "🔴 Red Line")).name == "Red Line"  # valor velho do select
    assert run(world.playlists.find(GID, "red line")).name == "Red Line"  # nome exato, sem diferenciar caixa
    assert run(world.playlists.find(GID, "🎧 Rock")).name == "Rock"  # o emoji só é tirado se nada casa exato
    assert run(world.playlists.find(GID, "🔴 Inexistente")) is None


def test_the_boot_migration_redraws_the_panel_of_each_changed_server_only_when_the_bots_are_ready():
    from empiremuse.utils.client import BotPool

    order = []
    ready = SimpleNamespace(wait_until_ready=AsyncMock(side_effect=lambda: order.append("ready")))
    playlists = SimpleNamespace(
        migrate_icons=AsyncMock(return_value=(2, 3, (GID, GID + 1))),
        refresh_idle=MagicMock(side_effect=lambda gid: order.append(gid)),
    )
    pool = SimpleNamespace(servers=SimpleNamespace(playlists=playlists), get_all_bots=lambda: [ready])

    run(BotPool.migrate_playlist_icons(pool))

    assert order == ["ready", GID, GID + 1]

    playlists.migrate_icons.return_value = (0, 0, ())  # nada mudou: ninguém espera nem redesenha
    order.clear()
    run(BotPool.migrate_playlist_icons(pool))
    assert order == []


# ---------------------------------------------------------------------- A5: escritas simultâneas e versão velha


def test_simultaneous_imports_and_edits_all_survive(world):
    async def scenario():
        await world.playlists.list(GID)
        await asyncio.gather(
            world.playlists.import_file(GID, parsed("Rock", "Pop")),
            world.playlists.import_file(GID, parsed("Jazz", "Blues")),
            world.playlists.add(GID, item("Soul")),
            world.playlists.add(GID, item("Funk")),
        )
        return await world.playlists.list(GID)

    assert sorted(names(run(scenario()))) == ["Blues", "Funk", "Jazz", "Pop", "Rock", "Soul"]


def test_a_stale_version_is_refused_and_writes_nothing(world):  # P-V7: painel × comando
    async def scenario():
        _, rev = await world.playlists.snapshot(GID)  # o painel abriu o formulário
        await world.playlists.add(GID, item("Rock"))  # o comando mudou a lista no meio
        saves = len(world.banco.saves)
        with pytest.raises(ServerConfigConflict):
            await world.playlists.add(GID, item("Pop"), expected_rev=rev)
        with pytest.raises(ServerConfigConflict):
            await world.playlists.clear(GID, expected_rev=rev)
        assert len(world.banco.saves) == saves
        _, fresh = await world.playlists.snapshot(GID)
        return await world.playlists.add(GID, item("Pop"), expected_rev=fresh)

    assert isinstance(run(scenario()), Playlist)
    assert names(run(world.playlists.list(GID))) == ["Rock", "Pop"]


# ---------------------------------------------------------------------- A9 / S-V11 / embed ocioso


def test_export_then_import_into_an_empty_server_gives_the_same_list(world):
    run(world.playlists.add(GID, item("Rock", genres=["Rock", "Anos 80"])))
    run(world.playlists.add(GID, item("Jazz café", url=f"{BASE}j?list=PL1", description="")))
    exported = run(world.playlists.export_bytes(GID))
    assert b"icon" not in exported and "café".encode() in exported  # texto legível, não \\u00e9

    report = run(world.playlists.import_file(OTHER, parse_file(exported, max_bytes=PLAYLIST_FILE_MAX_BYTES)))

    assert report.skipped == () and run(world.playlists.list(OTHER)) == run(world.playlists.list(GID))


@pytest.mark.parametrize("gid", [999, 0, -1, "abc", None, True])
def test_a_guild_outside_the_pool_is_refused_and_nothing_is_created(world, gid):
    with pytest.raises(UnknownGuild):
        run(world.playlists.add(gid, item("Rock")))
    assert world.banco.servers_doc(gid) is None and world.banco.saves == []


def test_the_idle_embed_is_refreshed_only_after_a_write_that_changed_the_list(world, monkeypatch):
    refreshed = []
    monkeypatch.setattr(world.playlists, "refresh_idle", refreshed.append)

    run(world.playlists.add(GID, item("Rock")))  # mudou
    run(world.playlists.add(GID, item("rock")))  # RuleError
    run(world.playlists.remove(GID, "não existe"))  # RuleError
    run(world.playlists.import_file(GID, parsed("rock")))  # nada novo
    world.banco.write_error = True
    with pytest.raises(ServerConfigUnavailable):
        run(world.playlists.add(GID, item("Pop")))  # a gravação falhou

    assert refreshed == [GID]


# ---------------------------------------------------------------------- A7/A8: arquivos do bot, anexo e texto colado


@pytest.fixture
def folder(tmp_path):
    pasta = tmp_path / "playlists"
    pasta.mkdir()
    (pasta / "rock-80.json").write_text('{"playlists": []}')
    (pasta / "Maiuscula.json").write_text("{}")  # fora do padrão do nome
    (pasta / "texto.txt").write_text("x")
    (pasta / "diretorio.json").mkdir()
    (pasta / "atalho.json").symlink_to(tmp_path / "segredo.json")
    (tmp_path / "segredo.json").write_text("{}")
    (pasta / "grande.json").write_bytes(b" " * (PLAYLIST_FILE_MAX_BYTES + 1))
    return pasta


def test_only_regular_files_with_a_safe_name_are_importable(folder):
    assert list_import_files(folder) == ("grande.json", "rock-80.json")
    assert list_import_files(folder / "não existe") == ()


@pytest.mark.parametrize(
    "name",
    [
        "../segredo.json",
        "rock-80.json/..",
        "./rock-80.json",
        "/etc/passwd",
        "atalho.json",
        "diretorio.json",
        "Maiuscula.json",
        "nao-existe.json",
        None,
        5,
    ],
)  # A7: caminho, link simbólico, diretório, fora do padrão, fora da lista e não-texto
def test_a_file_name_outside_the_listed_regular_files_is_refused(folder, name):
    assert isinstance(read_import_file(folder, name), RuleError)


def test_a_listed_file_is_read_and_one_over_the_limit_is_refused(folder):
    assert read_import_file(folder, "rock-80.json") == b'{"playlists": []}'
    assert read_import_file(folder, "grande.json").code == "file_too_large"


@pytest.mark.parametrize(
    ("filename", "size", "ok"),
    [
        ("lista.json", 100, True),
        ("LISTA.JSON", PLAYLIST_FILE_MAX_BYTES, True),
        ("lista.txt", 100, False),
        ("lista.json", PLAYLIST_FILE_MAX_BYTES + 1, False),  # A8: recusado pelo tamanho informado, antes de baixar
        ("lista.json", -1, False),
        ("lista.json", None, False),
        ("lista.json", True, False),
        (None, 100, False),
    ],
)
def test_an_attachment_is_checked_by_name_and_declared_size_before_any_download(filename, size, ok):
    assert (check_attachment(filename, size) is None) is ok


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("{}", True),
        ("x" * PLAYLIST_PASTE_MAX, True),
        ("x" * (PLAYLIST_PASTE_MAX + 1), False),
        ("   ", False),
        ("", False),
        (None, False),
    ],
)
def test_pasted_text_is_limited_to_4000_characters_on_the_server_too(text, ok):
    assert (check_paste(text) == text) is ok


@pytest.mark.regression
@pytest.mark.parametrize("raised", [ServerConfigConflict, ServerConfigUnavailable, UnknownGuild])
def test_displaying_the_list_never_breaks_when_the_store_cannot_answer(raised):
    from empiremuse.utils.server_playlists import safe_list

    async def failing(gid):
        raise raised

    bot = SimpleNamespace(pool=SimpleNamespace(servers=SimpleNamespace(playlists=SimpleNamespace(list=failing))))
    assert run(safe_list(bot, GID)) == ()
