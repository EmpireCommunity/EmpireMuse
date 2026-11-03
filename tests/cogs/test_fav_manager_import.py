"""``/fav_manager`` (playlists por escopo, item 8): o menu não tem mais a seção de playlists do servidor, e os favoritos
do PRÓPRIO membro entram por anexo ``.json`` ou texto colado, lidos por ``rules/playlists.parse_file`` (mesmos limites e
textos de erro do ``/playlists import``), com a cota e os tamanhos por usuário de sempre. Nada toca o store do servidor."""

import importlib.util
import json
from types import SimpleNamespace

import pytest

from empiremuse.utils.music.interactions import FavModalImport, ViewMode
from tests.support.modais import (
    URL_BASE,
    ModalBot,
    ModalInteraction,
    gravados,
    importar,
    modal_view,
    trocar_process_idle_embed,
)

pytestmark = pytest.mark.critical

FAVORITOS = ViewMode.fav_manager
ROCK, POP = f"{URL_BASE}/rock", f"{URL_BASE}/pop"


@pytest.fixture(autouse=True)
def _sem_idle_embed(monkeypatch):
    async def nada(*args, **kwargs):
        return None

    trocar_process_idle_embed(monkeypatch, nada)


def attachment(data: bytes, filename: str = "favoritos.json", read=None):
    async def default_read():
        return data

    return SimpleNamespace(filename=filename, size=len(data), read=read or default_read)


async def importar_anexo(bot, data: bytes, *, filename: str = "favoritos.json", read=None):
    view = modal_view(bot, FAVORITOS)
    inter = ModalInteraction(json_data="", estrito=True)
    inter.resolved_values = {"json_file": [attachment(data, filename, read)]}
    await FavModalImport(view).callback(inter)
    return inter


def playlists_file(*items) -> bytes:
    return json.dumps({"playlists": list(items)}).encode("utf-8")


@pytest.mark.asyncio
async def test_an_attachment_in_the_playlists_format_imports_the_members_favorites():
    bot = ModalBot(fav_links={"Antigo": f"{URL_BASE}/antigo"})

    inter = await importar_anexo(
        bot, playlists_file({"name": "Rock", "url": ROCK}, {"name": "Pop", "url": POP, "genres": ["pop"]})
    )

    assert gravados(bot, FAVORITOS) == {"Antigo": f"{URL_BASE}/antigo", "Rock": ROCK, "Pop": POP}
    assert bot.salvos_servidor == [], "favoritos do membro nunca tocam o documento do servidor"
    assert "importados" in inter.texto


@pytest.mark.asyncio
async def test_pasted_text_in_the_old_name_to_url_format_still_imports():
    bot = ModalBot()

    inter = await importar(bot, FAVORITOS, {"Rock": ROCK}, estrito=True)

    assert gravados(bot, FAVORITOS) == {"Rock": ROCK}
    assert "importado" in inter.texto


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"[1, 2]", b"nao e json", b"{}"])
async def test_a_file_that_is_not_a_playlists_file_is_refused_and_nothing_is_saved(raw):
    bot = ModalBot()

    inter = await importar_anexo(bot, raw)

    assert bot.salvos_usuario == []
    assert "válido" in inter.texto  # o texto de erro é o do /playlists import (rules/playlists)


@pytest.mark.asyncio
async def test_the_per_user_quota_and_the_name_limit_still_apply():
    cheio = ModalBot(favs=1)
    await importar_anexo(cheio, playlists_file({"name": "Rock", "url": ROCK}, {"name": "Pop", "url": POP}))
    assert cheio.salvos_usuario == []

    nome_longo = ModalBot(nome_max=5)
    inter = await importar_anexo(nome_longo, playlists_file({"name": "Nome comprido", "url": ROCK}))
    assert nome_longo.salvos_usuario == [] and "nome" in inter.texto.lower()


@pytest.mark.asyncio
async def test_an_attachment_that_is_not_a_json_file_is_refused_before_being_read():
    bot = ModalBot()
    lido = []

    async def read():
        lido.append(True)
        return b"{}"

    inter = await importar_anexo(bot, b"{}", filename="favoritos.exe", read=read)

    assert lido == [] and bot.salvos_usuario == [] and inter.texto


def test_the_menu_has_no_server_playlists_mode_any_more():
    assert not hasattr(ViewMode, "guild_fav_manager")
    assert importlib.util.find_spec("empiremuse.utils.music.interactions.fav_menu_playlists") is None
