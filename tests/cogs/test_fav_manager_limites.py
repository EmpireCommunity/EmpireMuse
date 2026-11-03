"""Limites de tamanho do import de favoritos pelo ``/fav_manager`` (playlists por escopo, item 8), com os números das regras como LITERAIS:
anexo até 64 KiB conferido pelo ``size`` informado ANTES de baixar, texto colado até 4.000 caracteres e o corpo baixado conferido de novo
(o ``size`` do Discord é só informado). Mutar qualquer um dos três para 100x não derrubava nenhum teste."""

import json

import pytest

from tests.cogs.test_fav_manager_import import FAVORITOS, ROCK, importar, importar_anexo, playlists_file  # noqa: F401
from tests.cogs.test_fav_manager_import import _sem_idle_embed  # noqa: F401
from tests.support.modais import ModalBot

pytestmark = pytest.mark.critical

ARQUIVO_MAX = 64 * 1024
COLAGEM_MAX = 4000


@pytest.mark.asyncio
async def test_an_attachment_over_64_kib_is_refused_by_its_declared_size_before_being_read():
    bot, lido = ModalBot(), []

    async def read():
        lido.append(True)
        return playlists_file({"name": "Rock", "url": ROCK})

    grande = playlists_file({"name": "Rock", "url": ROCK}) + b" " * (ARQUIVO_MAX + 1)
    inter = await importar_anexo(bot, grande, read=read)

    assert lido == [] and bot.salvos_usuario == [] and inter.texto


@pytest.mark.asyncio
async def test_a_body_over_64_kib_is_refused_even_when_the_declared_size_was_small():
    bot = ModalBot()
    corpo = playlists_file({"name": "Rock", "url": ROCK}) + b" " * (ARQUIVO_MAX + 1)

    async def read():
        return corpo

    inter = await importar_anexo(bot, playlists_file({"name": "Rock", "url": ROCK}), read=read)

    assert bot.salvos_usuario == [] and inter.texto


@pytest.mark.asyncio
async def test_pasted_text_over_4000_characters_is_refused_and_nothing_is_saved():
    bot = ModalBot()
    texto = json.dumps({"Rock": ROCK}) + " " * COLAGEM_MAX

    inter = await importar(bot, FAVORITOS, texto, estrito=True)

    assert bot.salvos_usuario == [] and inter.texto
