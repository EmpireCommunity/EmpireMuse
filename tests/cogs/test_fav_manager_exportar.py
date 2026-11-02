"""``/fav_manager`` → Exportar (playlists por escopo, item 8): o anexo ``.json`` efêmero leva SÓ os favoritos do próprio membro, no formato que o
import lê de volta, e respeita a espera de 15 s; nada do store de playlists do servidor entra no arquivo. Sem este teste o export não tinha nenhum."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.rules.limits import PLAYLIST_FILE_MAX_BYTES
from empiremuse.rules.playlists import parse_file
from empiremuse.utils.music.interactions import ViewMode
from empiremuse.utils.music.interactions.fav_menu import FavMenuView
from tests.support.modais import URL_BASE

pytestmark = pytest.mark.critical

MEUS = {"Rock": f"{URL_BASE}/rock", "Pop": f"{URL_BASE}/pop"}


def view(*, wait=0.0, favorites=None):
    bucket = SimpleNamespace(update_rate_limit=lambda: wait)
    cog = SimpleNamespace(
        fav_import_export_cd=SimpleNamespace(get_bucket=lambda inter: bucket),
        fav_manager=SimpleNamespace(name="fav_manager"),
    )
    bot = SimpleNamespace(
        get_cog=lambda name: cog,
        get_global_command_named=lambda *args, **kwargs: SimpleNamespace(id=1),
        get_color=lambda *args: 0,
        pool=SimpleNamespace(
            servers=SimpleNamespace(playlists=AsyncMock())
        ),  # se tocar o store do servidor, o AsyncMock registra
    )
    data = {"fav_links": MEUS if favorites is None else favorites, "integration_links": {}}
    return SimpleNamespace(bot=bot, data=data, mode=ViewMode.fav_manager, tr=get_i18n().for_locale("pt-BR"))


def click():
    return SimpleNamespace(
        send=AsyncMock(), response=SimpleNamespace(defer=AsyncMock()), edit_original_message=AsyncMock()
    )


@pytest.mark.asyncio
async def test_export_sends_an_ephemeral_json_with_only_the_members_own_favorites_that_the_import_reads_back():
    menu, inter = view(), click()

    await FavMenuView.export_callback(menu, inter)

    sent = inter.send.await_args.kwargs
    assert sent["ephemeral"] is True
    content = sent["file"].fp.read().decode()
    assert json.loads(content) == MEUS and sent["file"].filename == "favoritos.json"
    parsed = parse_file(content, max_bytes=PLAYLIST_FILE_MAX_BYTES)
    assert {item.name: item.url for item in parsed.items} == MEUS and not parsed.invalid
    menu.bot.pool.servers.playlists.assert_not_called()
    assert menu.bot.pool.servers.playlists.mock_calls == []  # nenhuma playlist do servidor lida ou escrita


@pytest.mark.asyncio
async def test_export_within_the_wait_time_sends_no_file_and_without_favorites_says_so():
    waiting, inter = view(wait=7.0), click()
    await FavMenuView.export_callback(waiting, inter)
    assert inter.send.await_args.kwargs["ephemeral"] is True and "file" not in inter.send.await_args.kwargs

    empty, inter = view(favorites={}), click()
    await FavMenuView.export_callback(empty, inter)
    assert inter.send.await_args.kwargs["ephemeral"] is True and "file" not in inter.send.await_args.kwargs
