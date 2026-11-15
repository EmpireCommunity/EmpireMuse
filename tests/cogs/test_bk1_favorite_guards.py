"""BK-1 e BK-2: cada porta de modal descarta a gravação se os dados do membro foram apagados depois do envio.

O documento é relido e gravado sob o lock do usuário (``change_user_doc``), o mesmo do apagamento; o que ainda pode acontecer é o
apagamento rodar ANTES de o modal chegar ao lock (aqui, durante a consulta da cota).
"""

import json

import pytest

from empiremuse.utils.music.interactions import FavModalAdd, FavModalImport, ViewMode
from tests.support.modais import ModalBot, ModalInteraction, modal_view

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]


@pytest.mark.parametrize("path", ["import_favorites", "import_integrations", "add_favorite"])
async def test_modal_guard_rejects_data_erased_before_the_write_reaches_the_lock(path):
    bot = ModalBot()

    async def erased_during_the_quota_lookup(user):
        bot.pool.erasure_tombstones.mark(user.id)
        return False

    bot.is_owner = erased_during_the_quota_lookup
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
