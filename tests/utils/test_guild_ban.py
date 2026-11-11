"""``GuildBans``: regressões m-E1 a m-E4 (``utils/guild_ban.py``), com banco TinyMongo real; sem relógio real e sem ``sleep``."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from empiremuse.utils.db import LocalDatabase
from empiremuse.utils.guild_ban import AlreadyBanned, GuildBans, GuildBansUnavailable

pytestmark = pytest.mark.regression

ALVO = 800000000000000001
LOGGER = "empiremuse.utils.guild_ban"


class BancoControlado(LocalDatabase):
    """TinyMongo real com falhas e pausas injetáveis."""

    def __init__(self, caminho):
        super().__init__(caminho)
        self.falha_query = False
        self.query_falhas_restantes = 0
        self.leitura_solta: asyncio.Event | None = None  # a PRÓXIMA leitura devolve o retrato e espera
        self.update_solto: asyncio.Event | None = None  # o PRÓXIMO update espera

    async def query_data(self, *args, **kwargs):
        if self.falha_query or self.query_falhas_restantes > 0:
            self.query_falhas_restantes = max(0, self.query_falhas_restantes - 1)
            raise ConnectionError("banco fora")
        retrato = await super().query_data(*args, **kwargs)
        espera, self.leitura_solta = self.leitura_solta, None
        if espera is not None:
            await espera.wait()
        return retrato

    async def update_data(self, *args, **kwargs):
        espera, self.update_solto = self.update_solto, None
        if espera is not None:
            await espera.wait()
        return await super().update_data(*args, **kwargs)


@pytest.fixture
def mundo(tmp_path):
    banco = BancoControlado(str(tmp_path / "db"))
    return SimpleNamespace(banco=banco, bans=GuildBans(SimpleNamespace(database=banco)))


async def doc(banco, guild_id):
    return await banco.find_doc(guild_id, db_name="banned_guilds", collection="global")


async def ceder():
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_m_e1_a_ban_during_a_reload_never_disappears_from_memory(mundo):
    mundo.banco.query_falhas_restantes = 1  # a carga do ban falha
    mundo.banco.update_solto = asyncio.Event()
    ban = asyncio.ensure_future(mundo.bans.ban(ALVO, "Servidor", "x"))
    await ceder()
    leitura = mundo.banco.leitura_solta = asyncio.Event()  # nova carga lê o banco antes de o ban ser gravado
    recarga = asyncio.ensure_future(mundo.bans.ensure_loaded())
    await ceder()
    mundo.banco.update_solto.set()
    await ceder()
    leitura.set()
    await asyncio.gather(ban, recarga, return_exceptions=True)

    assert (await doc(mundo.banco, ALVO) is not None) == mundo.bans.is_banned(ALVO)  # banco e memória concordam


@pytest.mark.asyncio
async def test_m_e2_unban_with_the_list_not_loaded_is_an_error_never_false(mundo):
    mundo.banco.falha_query = True

    with pytest.raises(GuildBansUnavailable):
        await mundo.bans.unban(ALVO)
    with pytest.raises(GuildBansUnavailable):
        await mundo.bans.ban(ALVO, "Servidor", "x")  # sem a lista, nem o reban é detectável
    assert await doc(mundo.banco, ALVO) is None


@pytest.mark.asyncio
async def test_m_e3_reban_keeps_the_original_record_and_writes_nothing(mundo):
    original = await mundo.bans.ban(ALVO, "Servidor", "primeiro")
    antes = await doc(mundo.banco, ALVO)

    with pytest.raises(AlreadyBanned):
        await mundo.bans.ban(ALVO, "Outro nome", "segundo")

    assert await doc(mundo.banco, ALVO) == antes
    assert mundo.bans.records() == [original]


@pytest.mark.asyncio
async def test_m_e4_malformed_documents_are_only_counted_in_the_log(mundo, caplog):
    await mundo.bans.ban(ALVO, "Servidor", "x")
    await mundo.banco.update_data("lixo-id", {"name": "x"}, db_name="banned_guilds", collection="global")
    caplog.set_level(logging.WARNING, logger=LOGGER)

    reinicio = GuildBans(mundo.bans.pool)
    await reinicio.ensure_loaded()

    assert reinicio.is_banned(ALVO)
    assert "1 documento(s) malformado(s)" in caplog.text and "lixo-id" not in caplog.text
