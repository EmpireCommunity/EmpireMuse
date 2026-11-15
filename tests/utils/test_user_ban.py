"""E5, ``UserBans`` (contrato §7): carga, ban e unban com banco TinyMongo real; R8.8 (D2, m-E1 a m-E5) e R8.9 (auditoria sem motivo).

Banco, sessões do painel e conexão RPC são falsos onde a falha precisa ser injetada; sem relógio real e sem ``sleep``.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from empiremuse.rules.user_ban import RPC_BLOCKED_REASON
from empiremuse.rules.user_cleanup import UserCleanupError
from empiremuse.utils import user_ban as user_ban_module
from empiremuse.utils.db import LocalDatabase
from empiremuse.utils.user_ban import AlreadyBanned, UserBans, UserBansUnavailable, user_bans_of

pytestmark = pytest.mark.critical

DONO, ALVO, OUTRO = 100000000000000009, 100000000000000001, 100000000000000002
SEGREDO = "MOTIVO-COM-DADO-PESSOAL"
LOGGER = "empiremuse.utils.user_ban"


class BancoQueFalha(LocalDatabase):
    """TinyMongo real com falhas injetáveis (um erro de banco não pode ir à memória)."""

    def __init__(self, caminho):
        super().__init__(caminho)
        self.falha: set[str] = set()
        self.pausa_primeira: asyncio.Event | None = None  # só a PRIMEIRA leitura espera

    async def query_data(self, *args, **kwargs):
        pausa, self.pausa_primeira = self.pausa_primeira, None
        if pausa is not None:
            await pausa.wait()
        if "query" in self.falha:
            raise ConnectionError("banco fora")
        return await super().query_data(*args, **kwargs)

    async def update_data(self, *args, **kwargs):
        if "update" in self.falha:
            raise ConnectionError("banco fora")
        return await super().update_data(*args, **kwargs)

    async def delete_data(self, *args, **kwargs):
        if "delete" in self.falha:
            raise ConnectionError("banco fora")
        return await super().delete_data(*args, **kwargs)


@pytest.fixture
def mundo(tmp_path):
    banco = BancoQueFalha(str(tmp_path / "db"))
    sessoes = MagicMock()
    pool = SimpleNamespace(database=banco, panel_sessions=sessoes)
    rpc: list[tuple[int, str]] = []
    relogio = iter(range(1700000000, 1800000000, 10))

    def fechar(user_id, motivo):
        rpc.append((user_id, motivo))
        return True

    def novo(**kwargs):
        return UserBans(pool, owners=frozenset({DONO}), clock=lambda: next(relogio), rpc_closer=fechar, **kwargs)

    return SimpleNamespace(banco=banco, sessoes=sessoes, pool=pool, rpc=rpc, novo=novo, bans=novo())


async def doc(banco, user_id):
    return await banco.find_doc(user_id, db_name="banned_users", collection="global")


@pytest.mark.asyncio
async def test_ban_persists_blocks_and_survives_a_restart(mundo):
    resultado = await mundo.bans.ban(DONO, ALVO, "spam")

    assert resultado.status == "OK" and mundo.bans.is_blocked(ALVO) and mundo.bans.is_blocked(str(ALVO))
    assert not mundo.bans.is_blocked(OUTRO)
    gravado = await doc(mundo.banco, ALVO)
    assert (gravado["by"], gravado["reason"]) == (DONO, "spam") and set(gravado) == {"_id", "by", "reason", "banned_at"}
    reinicio = mundo.novo()
    assert await reinicio.ensure_loaded() and reinicio.is_blocked(ALVO)  # o registro vem do banco


@pytest.mark.asyncio
async def test_ban_closes_panel_sessions_and_rpc_with_the_new_reason(mundo):
    await mundo.bans.ban(DONO, ALVO, "")

    mundo.sessoes.destroy_user.assert_called_once_with(str(ALVO))
    assert mundo.rpc == [(ALVO, RPC_BLOCKED_REASON)]


@pytest.mark.asyncio
async def test_a_cleanup_listener_failure_after_the_sessions_fell_is_not_a_failed_panel_cut(mundo, caplog):
    mundo.sessoes.destroy_user.side_effect = UserCleanupError(2, 1)  # as sessões já caíram; só um ouvinte falhou

    with caplog.at_level("WARNING"):
        resultado = await mundo.bans.ban(DONO, ALVO, "")

    assert any(
        "1 ouvinte(s) de limpeza falharam" in record.getMessage() for record in caplog.records
    )  # o log traz quantos falharam

    assert (resultado.status, resultado.failed) == ("OK", ())  # o acesso foi cortado: nada de ``panel_sessions``
    assert mundo.bans.is_blocked(ALVO) and mundo.rpc == [(ALVO, RPC_BLOCKED_REASON)]  # e o RPC segue sendo cortado


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quebra", "esperado"),
    [("sessoes", ("panel_sessions",)), ("rpc", ("rpc",)), ("ambos", ("panel_sessions", "rpc"))],
)
async def test_a_failed_consequence_is_partial_but_the_ban_stands(mundo, quebra, esperado):
    if quebra in ("sessoes", "ambos"):
        mundo.sessoes.destroy_user.side_effect = RuntimeError("falhou")
    if quebra in ("rpc", "ambos"):
        mundo.bans._rpc_closer = MagicMock(side_effect=RuntimeError("falhou"))

    resultado = await mundo.bans.ban(DONO, ALVO, "")

    assert (resultado.status, resultado.failed) == ("PARTIAL", esperado)
    assert mundo.bans.is_blocked(ALVO) and await doc(mundo.banco, ALVO)  # gravado e na memória (m-E5)
    if quebra == "rpc":
        mundo.sessoes.destroy_user.assert_called_once()  # uma falha não impede a outra consequência


@pytest.mark.asyncio
async def test_a_database_error_never_reaches_memory_nor_the_consequences(mundo, caplog):
    await mundo.bans.ensure_loaded()
    mundo.banco.falha.add("update")
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    with pytest.raises(ConnectionError):
        await mundo.bans.ban(DONO, ALVO, "x")

    assert not mundo.bans.is_blocked(ALVO) and await doc(mundo.banco, ALVO) is None
    mundo.sessoes.destroy_user.assert_not_called()
    assert mundo.rpc == [] and "banimento de usuário" not in caplog.text  # sem auditoria de ban que não valeu


@pytest.mark.asyncio
async def test_reban_is_refused_and_keeps_the_original_record(mundo):
    await mundo.bans.ban(DONO, ALVO, "primeiro")
    antes = await doc(mundo.banco, ALVO)

    with pytest.raises(AlreadyBanned):
        await mundo.bans.ban(OUTRO, ALVO, "segundo")

    assert await doc(mundo.banco, ALVO) == antes  # by e banned_at intactos (m-E3)
    assert mundo.bans.records()[0].by == DONO


@pytest.mark.asyncio
async def test_unban_with_the_list_not_loaded_is_an_error_never_false(mundo):
    mundo.banco.falha.add("query")

    with pytest.raises(UserBansUnavailable):
        await mundo.bans.unban(DONO, ALVO)
    with pytest.raises(UserBansUnavailable):
        await mundo.bans.ban(DONO, ALVO, "")  # ban também nunca vale só em memória
    assert await doc(mundo.banco, ALVO) is None


@pytest.mark.asyncio
async def test_unban_deletes_the_document_then_memory_and_false_when_not_banned(mundo):
    await mundo.bans.ban(DONO, ALVO, "")

    assert await mundo.bans.unban(DONO, ALVO) is True
    assert not mundo.bans.is_blocked(ALVO) and await doc(mundo.banco, ALVO) is None
    assert await mundo.bans.unban(DONO, ALVO) is False


@pytest.mark.asyncio
async def test_a_failed_unban_keeps_the_user_blocked(mundo):
    await mundo.bans.ban(DONO, ALVO, "")
    mundo.banco.falha.add("delete")

    with pytest.raises(ConnectionError):
        await mundo.bans.unban(DONO, ALVO)

    assert mundo.bans.is_blocked(ALVO) and await doc(mundo.banco, ALVO)


@pytest.mark.asyncio
async def test_a_ban_and_a_reban_during_the_load_are_serialized_and_not_lost(mundo):
    """m-E1/m-E3: a primeira leitura espera; ban e reban concorrentes aguardam o lock; solta-se a carga e só um ban vale, nada some."""
    pausa = mundo.banco.pausa_primeira = asyncio.Event()
    carga = asyncio.ensure_future(mundo.bans.ensure_loaded())
    await asyncio.sleep(0)  # a carga segura o lock e espera o banco
    ban = asyncio.ensure_future(mundo.bans.ban(DONO, ALVO, "primeiro"))
    reban = asyncio.ensure_future(mundo.bans.ban(OUTRO, ALVO, "segundo"))
    await asyncio.sleep(0)
    assert not ban.done() and not reban.done() and not mundo.bans.is_blocked(ALVO)

    pausa.set()
    await carga
    resultados = await asyncio.gather(ban, reban, return_exceptions=True)

    assert [type(r).__name__ for r in resultados] == ["BanResult", "AlreadyBanned"]
    assert mundo.bans.is_blocked(ALVO) and [r.by for r in mundo.bans.records()] == [DONO]
    assert (await doc(mundo.banco, ALVO))["by"] == DONO
    reinicio = mundo.novo()
    await reinicio.ensure_loaded()
    assert reinicio.is_blocked(ALVO)  # a recarga do banco também enxerga o ban


@pytest.mark.asyncio
async def test_a_load_that_started_before_the_ban_never_overwrites_it(mundo):
    """Leitura antiga solta DEPOIS do ban terminar: o ban gravado antes não pode sumir da memória (cenário do molde sem lock)."""
    await mundo.bans.ban(DONO, ALVO, "")
    outra = mundo.novo()
    pausa = mundo.banco.pausa_primeira = asyncio.Event()
    carga = asyncio.ensure_future(outra.ensure_loaded())
    await asyncio.sleep(0)
    ban = asyncio.ensure_future(outra.ban(DONO, OUTRO, ""))
    await asyncio.sleep(0)
    pausa.set()
    await carga
    await ban

    assert outra.is_blocked(ALVO) and outra.is_blocked(OUTRO)


@pytest.mark.asyncio
async def test_malformed_documents_are_only_counted_in_the_log(mundo, caplog):
    await mundo.bans.ban(DONO, ALVO, "")
    await mundo.banco.update_data("lixo-id", {"banned_at": 1}, db_name="banned_users", collection="global")
    caplog.set_level(logging.WARNING, logger=LOGGER)

    reinicio = mundo.novo()
    await reinicio.ensure_loaded()

    assert reinicio.is_blocked(ALVO) and len(reinicio.records()) == 1
    assert "1 documento(s) malformado(s)" in caplog.text and "lixo-id" not in caplog.text  # m-E4


@pytest.mark.asyncio
async def test_boot_without_database_opens_without_list_and_retries_until_it_loads(mundo, caplog):
    """D2: bot aberto sem lista (nenhum ID válido é bloqueado), login fechado (``loaded`` falso) e nova tentativa até carregar."""
    semente = mundo.novo()
    await semente.ban(DONO, ALVO, "")
    mundo.banco.falha.add("query")
    pausas = []
    caplog.set_level(logging.WARNING, logger=LOGGER)

    async def dormir(segundos):
        pausas.append(segundos)
        if len(pausas) >= 2:
            mundo.banco.falha.discard("query")  # o banco só volta na segunda espera: a primeira tentativa também falha

    bans = mundo.novo(sleep=dormir)
    assert await bans.boot() is False
    assert not bans.loaded and not bans.is_blocked(ALVO) and bans.is_blocked(None)
    assert caplog.text.count("não carregada") == 1  # WARNING uma vez

    await bans._retry_task

    assert bans.loaded and bans.is_blocked(ALVO) and pausas == [60.0, 60.0]  # tentou mais de uma vez
    bans.stop()


@pytest.mark.asyncio
async def test_boot_timeout_also_opens_and_schedules_the_retry(mundo, monkeypatch):
    async def estourar(coro, timeout):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(user_ban_module.asyncio, "wait_for", estourar)
    bans = mundo.novo(sleep=lambda s: asyncio.sleep(0))

    assert await bans.boot() is False
    assert bans._retry_task is not None
    bans.stop()
    monkeypatch.undo()
    await asyncio.gather(bans._retry_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_boot_with_a_healthy_database_loads_and_needs_no_retry(mundo):
    assert await mundo.bans.boot() is True and mundo.bans._retry_task is None


@pytest.mark.asyncio
async def test_audit_has_one_line_per_action_without_the_reason(mundo, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)

    await mundo.bans.ban(DONO, ALVO, SEGREDO)
    await mundo.bans.unban(DONO, ALVO)

    linhas = [r for r in caplog.records if r.name == LOGGER and "banimento de usuário" in r.getMessage()]
    assert [r.getMessage().split("ação=")[1].split()[0] for r in linhas] == ["ban", "unban"]
    for linha in linhas:
        assert f"dono={DONO}" in linha.getMessage() and f"alvo={ALVO}" in linha.getMessage()
        assert linha.empiremuse_emit is True
    assert SEGREDO not in caplog.text


@pytest.mark.asyncio
async def test_owner_is_never_blocked_even_if_a_record_exists(mundo):
    await mundo.bans.ban(OUTRO, DONO, "")  # o chamador deveria ter recusado; a regra protege mesmo assim
    assert not mundo.bans.is_blocked(DONO)


def test_user_bans_of_finds_the_list_from_a_bot_or_the_pool_and_ignores_fakes(mundo):
    assert user_bans_of(mundo.pool) is None
    mundo.pool.user_bans = mundo.bans
    assert user_bans_of(mundo.pool) is mundo.bans and user_bans_of(SimpleNamespace(pool=mundo.pool)) is mundo.bans
    mundo.pool.user_bans = object()
    assert user_bans_of(mundo.pool) is None
