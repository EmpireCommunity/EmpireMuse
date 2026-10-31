"""``/playlists importar`` e ``/playlists exportar`` (item 17, A6, A7, A8, A11): quem não gerencia é negado em TODO comando e modal,
o anexo grande é recusado antes de baixar, o nome do arquivo do bot não vira caminho, o ícone só vale vindo de arquivo do bot e
toda resposta é efêmera, sem menção e sem texto de exceção."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.cogs import server_playlists as cog_module
from empiremuse.cogs.server_playlists import (
    PasteModal,
    ServerPlaylistsCog,
    ensure_manage,
    member_may_manage,
)
from empiremuse.rules.errors import FILE_TOO_LARGE, RuleError
from empiremuse.rules.limits import PLAYLIST_FILE_MAX_BYTES, PLAYLIST_PASTE_MAX
from empiremuse.rules.playlists import ImportReport
from empiremuse.utils.music.errors import GenericError

pytestmark = pytest.mark.critical

GID, OWNER_ID = 111, 1
FILE = {"playlists": [{"name": "Rock", "url": "https://exemplo.com/rock", "icon": "🎧"}]}


def member(*, user_id=50, administrator=False, manage_guild=False):
    return MagicMock(
        spec=disnake.Member,
        id=user_id,
        guild_permissions=SimpleNamespace(administrator=administrator, manage_guild=manage_guild),
    )


GUILD = SimpleNamespace(id=GID, owner_id=OWNER_ID)
ADMIN, MANAGER, OWNER = member(administrator=True), member(manage_guild=True), member(user_id=OWNER_ID)
PLAIN = member()


def make_inter(author, **extra):
    return SimpleNamespace(
        author=author,
        guild=GUILD,
        guild_id=GID,
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock()),
        edit_original_message=AsyncMock(),
        text_values={},
        **extra,
    )


@pytest.fixture
def world():
    playlists = SimpleNamespace(
        import_file=AsyncMock(return_value=ImportReport((), ())),
        list=AsyncMock(return_value=(object(),)),
        export_bytes=AsyncMock(return_value=b"{}"),
    )
    bot = SimpleNamespace(pool=SimpleNamespace(servers=SimpleNamespace(playlists=playlists)))
    return SimpleNamespace(cog=ServerPlaylistsCog(bot), playlists=playlists)


def sent_text(inter):
    return inter.edit_original_message.await_args.args[0]


def assert_private_and_without_mentions(inter):
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    mentions = inter.edit_original_message.await_args.kwargs["allowed_mentions"]
    assert (mentions.everyone, mentions.users, mentions.roles) == (False, False, False)


# ---------------------------------------------------------------------- A6: permissão


@pytest.mark.parametrize(
    ("who", "allowed"),
    [
        (OWNER, True),
        (ADMIN, True),
        (MANAGER, True),
        (PLAIN, False),
        (None, False),
        (SimpleNamespace(id=OWNER_ID), False),
    ],
    ids=["owner", "administrator", "manage_guild", "plain", "no_member", "no_guild_permissions"],
)
def test_only_the_owner_administrator_or_manage_guild_may_manage(who, allowed):
    assert member_may_manage(who, GUILD) is allowed
    assert member_may_manage(who, None) is False
    if allowed:
        ensure_manage(make_inter(who))
    else:
        with pytest.raises(GenericError):
            ensure_manage(make_inter(who))


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["import_", "export_", "paste_modal"])
async def test_a_plain_member_is_denied_in_every_command_and_modal_before_anything_happens(world, entry):
    inter = make_inter(PLAIN)
    inter.text_values = {
        "json_data": json.dumps(FILE)
    }  # o modal foi aberto quando ele ainda era admin (rebaixado depois)

    with pytest.raises(GenericError):
        if entry == "paste_modal":
            await PasteModal.callback(PasteModal(world.cog), inter)
        elif entry == "import_":
            await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=None, link=None)
        else:
            await ServerPlaylistsCog.export_.callback(world.cog, inter)

    inter.response.defer.assert_not_awaited()
    inter.response.send_modal.assert_not_awaited()
    world.playlists.import_file.assert_not_awaited()
    world.playlists.export_bytes.assert_not_awaited()


# ---------------------------------------------------------------------- origens


@pytest.mark.asyncio
async def test_no_origin_opens_the_paste_modal_and_two_origins_are_refused(world):
    inter = make_inter(ADMIN)
    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=None, link=None)
    assert isinstance(inter.response.send_modal.await_args.args[0], PasteModal)

    with pytest.raises(GenericError):
        await ServerPlaylistsCog.import_.callback(
            world.cog, make_inter(ADMIN), file="rock.json", attachment=object(), link=None
        )
    world.playlists.import_file.assert_not_awaited()


def attachment(filename="lista.json", size=100, content=None):
    return SimpleNamespace(
        filename=filename, size=size, read=AsyncMock(return_value=json.dumps(content or FILE).encode())
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "size"),
    [("lista.json", PLAYLIST_FILE_MAX_BYTES + 1), ("lista.txt", 100), ("lista.json", None)],
    ids=["too_big", "not_json", "unknown_size"],
)
async def test_a_bad_attachment_is_refused_before_it_is_downloaded(world, filename, size):  # A8
    inter, file = make_inter(ADMIN), attachment(filename, size)

    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=file, link=None)

    file.read.assert_not_awaited()
    world.playlists.import_file.assert_not_awaited()
    assert_private_and_without_mentions(inter)


@pytest.mark.asyncio
async def test_an_attachment_import_uses_the_interaction_guild_and_keeps_the_emoji_icon(world):  # A11
    inter = make_inter(MANAGER)

    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=attachment(), link=None)

    (gid, parsed), _ = world.playlists.import_file.await_args
    assert gid == GID and [p.name for p in parsed.items] == ["Rock"] and parsed.items[0].icon == "🎧"
    assert_private_and_without_mentions(inter)


@pytest.mark.asyncio
async def test_a_file_that_is_not_json_changes_nothing_and_shows_no_exception_text(world):  # A1
    inter, file = make_inter(ADMIN), attachment()
    file.read = AsyncMock(return_value=b"\xff nao e json")

    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=file, link=None)

    world.playlists.import_file.assert_not_awaited()
    assert "Traceback" not in sent_text(inter) and "Decode" not in sent_text(inter)


# ---------------------------------------------------------------------- A7: arquivo do bot


@pytest.fixture
def bot_files(tmp_path, monkeypatch):
    pasta = tmp_path / "playlists"
    pasta.mkdir()
    (pasta / "rock-80.json").write_text(json.dumps(FILE))
    (tmp_path / "segredo.json").write_text(json.dumps(FILE))
    (pasta / "atalho.json").symlink_to(tmp_path / "segredo.json")
    monkeypatch.setattr(cog_module, "IMPORT_DIR", pasta)
    return pasta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["../segredo.json", "atalho.json", "nao-existe.json", "/etc/passwd", "rock-80.json/.."]
)
async def test_a_file_name_that_is_not_a_listed_regular_file_is_refused(world, bot_files, name):
    inter = make_inter(ADMIN)

    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=name, attachment=None, link=None)

    world.playlists.import_file.assert_not_awaited()
    assert "não está disponível" in sent_text(inter)


@pytest.mark.asyncio
async def test_a_bot_file_is_imported_with_its_emoji_icon(world, bot_files):
    inter = make_inter(ADMIN)

    await ServerPlaylistsCog.import_.callback(world.cog, inter, file="rock-80.json", attachment=None, link=None)

    (gid, parsed), _ = world.playlists.import_file.await_args
    assert gid == GID and parsed.items[0].icon == "🎧"


# ---------------------------------------------------------------------- texto colado e exportar


@pytest.mark.asyncio
async def test_pasted_text_is_limited_on_the_server_even_if_the_modal_limit_is_bypassed(world):
    inter = make_inter(ADMIN)
    inter.text_values = {"json_data": "x" * (PLAYLIST_PASTE_MAX + 1)}

    await PasteModal.callback(PasteModal(world.cog), inter)

    world.playlists.import_file.assert_not_awaited()
    assert "grande demais" in sent_text(inter)


@pytest.mark.asyncio
async def test_pasted_playlists_are_imported_into_the_interaction_guild_and_the_report_has_no_mentions(world):
    inter = make_inter(ADMIN)
    inter.text_values = {"json_data": json.dumps(FILE)}

    await PasteModal.callback(PasteModal(world.cog), inter)

    assert world.playlists.import_file.await_args.args[0] == GID
    assert_private_and_without_mentions(inter)


@pytest.mark.asyncio
async def test_export_sends_a_private_json_named_after_the_guild_and_an_empty_server_gets_a_message(world):
    inter = make_inter(OWNER)
    await ServerPlaylistsCog.export_.callback(world.cog, inter)
    assert inter.edit_original_message.await_args.kwargs["file"].filename == f"playlists-{GID}.json"
    inter.response.defer.assert_awaited_once_with(ephemeral=True)

    world.playlists.list.return_value = ()
    empty = make_inter(OWNER)
    await ServerPlaylistsCog.export_.callback(world.cog, empty)
    assert "ainda não tem playlists" in sent_text(empty) and "file" not in empty.edit_original_message.await_args.kwargs


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True])
async def test_a_modal_failure_always_answers_the_user_privately_and_without_mentions(world, answered):
    from empiremuse.utils.servers import ServerConfigUnavailable

    inter = make_inter(ADMIN)
    inter.response.is_done = lambda: answered
    inter.send = AsyncMock()

    await PasteModal(world.cog).on_error(ServerConfigUnavailable(), inter)

    sender = inter.edit_original_message if answered else inter.send
    sender.assert_awaited_once()
    assert "indisponível" in sender.await_args.args[0]
    assert sender.await_args.kwargs["allowed_mentions"].everyone is False
    if not answered:
        assert sender.await_args.kwargs["ephemeral"] is True


@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_command_check_denies_a_plain_member_before_the_cooldown_is_spent(world):
    for command in (ServerPlaylistsCog.import_, ServerPlaylistsCog.export_):
        assert cog_module._manage_check in command.checks
    with pytest.raises(GenericError):
        cog_module._manage_check(make_inter(PLAIN))
    assert cog_module._manage_check(make_inter(MANAGER)) is True


@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_failed_attachment_download_is_a_friendly_answer_not_an_exception(world):
    inter = make_inter(ADMIN)
    failing = attachment()
    failing.read = AsyncMock(side_effect=disnake.HTTPException(SimpleNamespace(status=500, reason="x"), "boom"))

    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=failing, link=None)

    assert "anexo" in sent_text(inter)
    world.playlists.import_file.assert_not_awaited()


@pytest.mark.regression
def test_every_command_and_option_description_fits_the_discord_limit_of_100_characters():
    # uma descrição maior faz o Discord recusar o registro dos comandos (HTTP 400)
    for command in (ServerPlaylistsCog.playlists, ServerPlaylistsCog.import_, ServerPlaylistsCog.export_):
        body = command.body
        assert len(str(body.description)) <= 100, body.name
        for option in body.options:
            assert len(str(option.description)) <= 100, (body.name, option.name)


# ---------------------------------------------------------------------- P5: origem por link


LINK = "https://raw.githubusercontent.com/dono/repo/main/playlists.json?token=segredo-9f3a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origins",
    [{"file": "rock-80.json", "link": LINK}, {"attachment": object(), "link": LINK}],
    ids=["file+link", "attachment+link"],
)
async def test_two_origins_including_the_link_are_refused(world, monkeypatch, origins):
    fetched = AsyncMock()
    monkeypatch.setattr(cog_module, "fetch_import", fetched)
    inter = make_inter(ADMIN)

    with pytest.raises(GenericError) as refused:
        await ServerPlaylistsCog.import_.callback(
            world.cog, inter, **{"file": None, "attachment": None, "link": None, **origins}
        )

    assert "Escolha só uma origem" in refused.value.text
    fetched.assert_not_awaited()
    inter.response.defer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "http://raw.githubusercontent.com/p.json",
        "https://evil.com/p.json",
        "https://192.168.0.1/p.json?token=segredo-9f3a",
    ],
)
async def test_a_bad_link_is_refused_before_deferring_and_without_any_fetch(world, monkeypatch, bad):
    fetched = AsyncMock()
    monkeypatch.setattr(cog_module, "fetch_import", fetched)
    inter = make_inter(ADMIN)

    with pytest.raises(GenericError) as refused:
        await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=None, link=bad)

    fetched.assert_not_awaited()
    inter.response.defer.assert_not_awaited()
    assert "segredo-9f3a" not in refused.value.text and bad not in refused.value.text  # a resposta nunca repete o link
    world.playlists.import_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_good_link_is_fetched_imported_with_its_emoji_icon_and_answered_with_the_report(world, monkeypatch):
    fetched = AsyncMock(return_value=json.dumps(FILE).encode())
    monkeypatch.setattr(cog_module, "fetch_import", fetched)
    inter = make_inter(MANAGER)

    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=None, link=LINK)

    fetched.assert_awaited_once_with(LINK)
    (gid, parsed), _ = world.playlists.import_file.await_args
    assert gid == GID and parsed.items[0].name == "Rock" and parsed.items[0].icon == "🎧"
    assert_private_and_without_mentions(inter)
    assert "segredo-9f3a" not in sent_text(inter)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        RuleError(cog_module.IMPORT_FETCH_FAILED),
        RuleError(cog_module.IMPORT_URL_REFUSED),
        RuleError(FILE_TOO_LARGE, limit=PLAYLIST_FILE_MAX_BYTES),
    ],
    ids=["fetch_failed", "refused_by_dns", "too_large"],
)
async def test_a_failed_fetch_answers_with_a_fixed_text_that_never_has_the_link(world, monkeypatch, failure):
    monkeypatch.setattr(cog_module, "fetch_import", AsyncMock(return_value=failure))
    inter = make_inter(ADMIN)

    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=None, link=LINK)

    world.playlists.import_file.assert_not_awaited()
    assert "segredo-9f3a" not in sent_text(inter) and "raw.githubusercontent.com/dono" not in sent_text(inter)
    assert_private_and_without_mentions(inter)


@pytest.mark.asyncio
async def test_a_plain_member_cannot_use_the_link_origin_and_no_origin_still_opens_the_modal(world, monkeypatch):
    fetched = AsyncMock()
    monkeypatch.setattr(cog_module, "fetch_import", fetched)
    with pytest.raises(GenericError):
        await ServerPlaylistsCog.import_.callback(world.cog, make_inter(PLAIN), file=None, attachment=None, link=LINK)
    fetched.assert_not_awaited()  # sem permissão, nenhuma busca de rede

    inter = make_inter(ADMIN)
    await ServerPlaylistsCog.import_.callback(world.cog, inter, file=None, attachment=None, link=None)
    assert isinstance(inter.response.send_modal.await_args.args[0], PasteModal)
