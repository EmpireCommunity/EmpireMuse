"""S-31, achados do CodeRabbit no #130 — o link do aviso de remoção de favorito/integração e o título sem URL da skin estática ``embed_link``.

* ``fav_menu.remove_callback``: o favorito do usuário é uma ``str`` (URL), o do servidor é ``{url, description}`` e a integração é ``{url, avatar}``;
  o ``md_link`` recebe sempre a URL (``str``), nunca o dicionário, senão o aviso perde o link.
* ``skins/static_player/embed_link.py``: título sem URI sai por ``md_link`` só com o rótulo (uma linha, sem marcas), não só por ``fix_characters``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.i18n import get_i18n
from empiremuse.utils.music.interactions.common import ViewMode
from empiremuse.utils.music.interactions.fav_menu import FavMenuView
from tests.support.marcas_md import NOME_COM_MARCAS, destinos, links_alheios, marcas_soltas

pytestmark = pytest.mark.critical

URL = "https://ok.exemplo/favorito"


def visao(modo: str, nome: str, valor, onde: str):
    """``FavMenuView`` sem Discord: ``valor`` entra em ``data`` (usuário/integração) ou em ``guild_data`` (servidor), sob o nome ``nome``."""
    v = FavMenuView.__new__(FavMenuView)
    v.mode, v.current, v.log = modo, nome, ""
    v.tr = get_i18n().for_locale("pt-BR")
    v.build_txt = lambda: ""
    guild = SimpleNamespace(me=SimpleNamespace())
    v.bot = SimpleNamespace(
        update_global_data=AsyncMock(),
        update_data=AsyncMock(),
        get_guild=lambda _id: guild,
        get_color=lambda *a: 0,
        pool=SimpleNamespace(get_all_bots=lambda: []),
    )
    v.data = {"fav_links": {}, "integration_links": {}}
    v.guild_data = {"player_controller": {"fav_links": {}}}
    v.bot.get_global_data = AsyncMock(side_effect=lambda *a, **k: v.data)
    v.bot.get_data = AsyncMock(side_effect=lambda *a, **k: v.guild_data)
    if onde == "usuario":
        v.data["fav_links"][nome] = valor
    elif onde == "servidor":
        v.guild_data["player_controller"]["fav_links"][nome] = valor
    else:
        v.data["integration_links"][nome] = valor
    return v


def interacao() -> MagicMock:
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response = SimpleNamespace(defer=AsyncMock())
    inter.edit_original_message, inter.send = AsyncMock(), AsyncMock()
    inter.author = SimpleNamespace(id=7)
    inter.guild_id = 1
    return inter


CASOS = [
    pytest.param(ViewMode.fav_manager, URL, "usuario", id="favorito-do-usuario"),
    pytest.param(
        ViewMode.integrations_manager, {"url": URL, "avatar": "https://ok.exemplo/a.png"}, "integracao", id="integracao"
    ),
]


@pytest.mark.asyncio
class TestAvisoDeRemocao:
    @pytest.mark.parametrize(("modo", "valor", "onde"), CASOS)
    async def test_a_name_with_marks_cannot_add_a_link_to_the_notice(self, modo, valor, onde):
        v = visao(modo, NOME_COM_MARCAS, valor, onde)

        await v.remove_callback(interacao())

        assert destinos(v.log) == [URL] and links_alheios(v.log) == [] and marcas_soltas(v.log) == 0
