"""B9 · exclusão de dados e as favoritas de rádio (contrato rev. 9 §4 ``[r9-fav]`` j): o que as lojas gravam é achado pelo apagamento (id com o do usuário como
prefixo ou sufixo fica), a lista do servidor fica, e uma gravação que começou ANTES do apagamento nunca recria o documento de quem foi apagado (sepultura)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from empiremuse.i18n import get_i18n
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.custom_favorites import CUSTOM_FAVORITES_DB
from empiremuse.utils.radio.favorites import FAVORITES_DB
from empiremuse.utils.radio.premium import GLOBAL_DB, RadioUnavailable
from empiremuse.utils.radio.user_favorites import USER_FAVORITES_DB
from tests.persistence.test_radio_favoritas import station, uuid
from tests.support.erasure_world import (
    ALVO,
    PREFIXO_DO_ALVO,
    SERVIDOR,
    SERVIDOR_2,
    SUFIXO_DO_ALVO,
    existe,
    gravar,
    montar_mundo,
)

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

CUSTOM = "a" * 24
GENRE = "g:rock"


@pytest_asyncio.fixture
async def mundo(tmp_path, monkeypatch):
    mundo = montar_mundo(tmp_path)
    mundo.pool.i18n = get_i18n()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "runtime"))
    hub = RadioHub(mundo.pool)
    hub.directory = SimpleNamespace(by_uuid_strict=AsyncMock(side_effect=lambda value: station(int(value[-12:]))))
    hub.locations = SimpleNamespace(node=lambda value: object() if value.startswith("node:") else None)
    mundo.hub = hub
    yield mundo
    for tarefa in list(mundo.service._tasks):
        tarefa.cancel()
    await asyncio.gather(*mundo.service._tasks, return_exceptions=True)


async def yes(ref):
    return True


async def test_the_erasure_removes_what_the_api_stores_wrote_for_all_guilds_and_nothing_else(mundo):
    hub = mundo.hub
    for user in (ALVO, PREFIXO_DO_ALVO, SUFIXO_DO_ALVO):  # o alvo e dois ids que o contêm como prefixo e sufixo
        await hub.user_favorites.add_station(user, uuid(1))
        await hub.user_favorites.add_filter(user, "node:a")
        await hub.custom_favorites.add(SERVIDOR, user, CUSTOM, yes)
        await hub.custom_favorites.add(SERVIDOR_2, user, GENRE, yes)
    await gravar(
        mundo,
        GLOBAL_DB,
        FAVORITES_DB,
        str(SERVIDOR),
        {"_id": str(SERVIDOR), "rev": 1, "stations": [], "refs": [CUSTOM]},
    )
    mundo.pool.i18n = get_i18n()

    await mundo.service.erase_user(ALVO, source="slash")

    assert not await existe(mundo, GLOBAL_DB, USER_FAVORITES_DB, str(ALVO))
    for guild in (SERVIDOR, SERVIDOR_2):
        assert not await existe(mundo, GLOBAL_DB, CUSTOM_FAVORITES_DB, f"{guild}:{ALVO}")
        for other in (PREFIXO_DO_ALVO, SUFIXO_DO_ALVO):
            assert await existe(mundo, GLOBAL_DB, CUSTOM_FAVORITES_DB, f"{guild}:{other}")
    for other in (PREFIXO_DO_ALVO, SUFIXO_DO_ALVO):
        assert await existe(mundo, GLOBAL_DB, USER_FAVORITES_DB, str(other))
    assert await existe(mundo, GLOBAL_DB, FAVORITES_DB, str(SERVIDOR))  # a lista do servidor não tem usuário: fica


async def test_an_account_write_that_started_before_the_erasure_never_recreates_the_document(mundo):
    hub = mundo.hub

    async def erased_while_asking_the_directory(value):
        mundo.pool.erasure_tombstones.mark(ALVO)  # o apagamento termina no meio da consulta ao diretório
        return station(1)

    hub.directory.by_uuid_strict = AsyncMock(side_effect=erased_while_asking_the_directory)

    with pytest.raises(RadioUnavailable):
        await hub.user_favorites.add_station(ALVO, uuid(1))

    assert not await existe(mundo, GLOBAL_DB, USER_FAVORITES_DB, str(ALVO))
    assert await hub.user_favorites.add_filter(ALVO + 1, "node:a")  # outro usuário segue normal


async def test_a_member_write_that_started_before_the_erasure_never_recreates_the_document(mundo):
    async def erased_while_checking(ref):
        mundo.pool.erasure_tombstones.mark(ALVO)
        return True

    with pytest.raises(RadioUnavailable):
        await mundo.hub.custom_favorites.add(SERVIDOR, ALVO, CUSTOM, erased_while_checking)

    assert not await existe(mundo, GLOBAL_DB, CUSTOM_FAVORITES_DB, f"{SERVIDOR}:{ALVO}")
    mundo.relogio.avancar(1)
    assert await mundo.hub.custom_favorites.add(
        SERVIDOR, ALVO, CUSTOM, yes
    )  # uso NOVO, com leitura depois do apagamento: grava
