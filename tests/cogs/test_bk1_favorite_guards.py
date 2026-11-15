"""BK-1: cada porta de modal descarta o documento apagado durante a leitura."""

import json

import pytest

from empiremuse.utils.music.interactions import FavModalAdd, FavModalImport, ViewMode
from tests.support.modais import ModalBot, ModalInteraction, modal_view

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]


@pytest.mark.parametrize("path", ["import_favorites", "import_integrations", "add_favorite"])
async def test_modal_guard_rejects_data_erased_after_read(path):
    bot = ModalBot()
    read = bot.get_global_data

    async def erased_after_read(user, **kwargs):
        doc = await read(user, **kwargs)
        bot.pool.erasure_tombstones.mark(user)
        return doc

    bot.get_global_data = erased_after_read
    mode = ViewMode.integrations_manager if path == "import_integrations" else ViewMode.fav_manager
    view = modal_view(bot, mode)
    if path == "add_favorite":
        inter = ModalInteraction(user_fav_name="Rock", user_fav_url="https://example.com/rock")
        await FavModalAdd(None, None, view).callback(inter)
    else:
        inter = ModalInteraction(json_data=json.dumps({"Rock": "https://example.com/rock"}))
        await FavModalImport(view).callback(inter)
    assert bot.salvos_usuario == []
    assert inter.enviados
