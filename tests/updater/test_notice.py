"""A15/A14b (item 18, P2) — ``deliver_update_notice`` e ``on_pool_ready`` (decisão F3): o aviso pós-boot do update.

Entregue uma única vez, pelo bot certo (``bot_id`` pronto), no canal de origem, com o DM a quem pediu como fallback, sem menções e sem
sobrar pendente; pendente com mais de 24 h é descartado. Bots, canais e usuários são fakes; o relógio e a versão detectada são injetados.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse import paths
from empiremuse.rules.release import Version
import json

from empiremuse.updater import boot_guard
from empiremuse.updater.notice import deliver_update_notice, on_pool_ready
from tests.support.alvo_git import ALVO_GIT
from empiremuse.updater.state import PendingUpdate, pending_path, write_pending

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

AGORA = 1_800_000_000.0
BOT_ID, CANAL, AUTOR = 111, 222, 333
DE, PARA = Version(2, 0, 0), Version(2, 1, 0)
SHA = "a" * 40
CHANGELOG = "# Changelog\n\n## [2.1.0] - 2026-11-01\n- novidade da 2.1.0\n\n## [2.0.0]\n- base\n"


def pendente(**campos) -> PendingUpdate:
    base = {
        "flow": "git",
        "from_version": DE,
        "to_version": PARA,
        "bot_id": BOT_ID,
        "channel_id": CANAL,
        "requested_by": AUTOR,
        "created_at": AGORA - 120,
        "prev_sha": SHA,
        "boots": 1,
        "target_commit": ALVO_GIT,
    }
    return PendingUpdate(**{**base, **campos})


def erro_http(status=404):
    return disnake.HTTPException(SimpleNamespace(status=status, reason="x"), "falhou")


def bot_falso(identificador=BOT_ID, pronto=True, canal=True, usuario=True):
    bot = MagicMock()
    bot.user = SimpleNamespace(id=identificador)
    bot.bot_ready = pronto
    destino_canal, destino_usuario = MagicMock(), MagicMock()
    destino_canal.send = AsyncMock()
    destino_usuario.send = AsyncMock()
    bot.get_channel = MagicMock(return_value=destino_canal if canal else None)
    bot.fetch_channel = AsyncMock(return_value=destino_canal) if canal else AsyncMock(side_effect=erro_http())
    bot.get_user = MagicMock(return_value=destino_usuario if usuario else None)
    bot.fetch_user = AsyncMock(return_value=destino_usuario) if usuario else AsyncMock(side_effect=erro_http())
    bot.canal, bot.usuario = destino_canal, destino_usuario
    return bot


def pool_com(*bots, saudavel=True):
    return SimpleNamespace(
        bots=list(bots),
        get_all_bots=lambda: list(bots),
        secret_values=lambda: frozenset(),
        pool_is_healthy=lambda: saudavel,
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    pasta = tmp_path / "runtime"
    pasta.mkdir()
    projeto = tmp_path / "projeto"
    projeto.mkdir()
    (projeto / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(paths, "PROJECT_DIR", projeto)
    return pasta


def entregar(pool, runtime, versao):
    async def detectar(pendente_lido):
        return versao

    return deliver_update_notice(pool, runtime_dir=runtime, now=lambda: AGORA, detect_version=detectar)


def sem_mencoes(chamada) -> bool:
    permitido = chamada.kwargs["allowed_mentions"]
    return (
        permitido.everyone is False
        and permitido.users is False
        and permitido.roles is False
        and permitido.replied_user is False
    )


async def test_success_is_delivered_once_by_the_right_bot_in_the_right_channel(runtime):
    certo, outro = bot_falso(), bot_falso(identificador=999)
    write_pending(runtime, pendente())

    await entregar(pool_com(outro, certo), runtime, PARA)
    await entregar(pool_com(outro, certo), runtime, PARA)

    certo.canal.send.assert_awaited_once()
    outro.canal.send.assert_not_awaited()
    outro.get_channel.assert_not_called()
    chamada = certo.canal.send.await_args
    descricao = chamada.kwargs["embed"].description
    assert (
        "Atualizado de 2.0.0 para 2.1.0" in descricao
        and "novidade da 2.1.0" in descricao
        and "v2.1.0/CHANGELOG.md" in descricao
    )
    assert sem_mencoes(chamada)
    certo.get_channel.assert_called_once_with(CANAL)
    assert not pending_path(runtime).exists()


@pytest.mark.parametrize(
    ("campos", "versao"),
    [({"status": "rolled_back"}, DE), ({}, DE)],
    ids=["rollback-pela-guarda", "versao-igual-a-origem"],
)
async def test_a_failed_update_says_it_went_back(runtime, campos, versao):
    bot = bot_falso()
    write_pending(runtime, pendente(**campos))

    await entregar(pool_com(bot), runtime, versao)

    descricao = bot.canal.send.await_args.kwargs["embed"].description
    assert "falhou" in descricao and "voltou para a versão 2.0.0" in descricao and "novidade" not in descricao
    assert not pending_path(runtime).exists()


@pytest.mark.parametrize("versao", [PARA, None], ids=["subiu-a-versao-nova", "versao-nao-detectada"])
async def test_a_rollback_that_is_not_confirmed_by_the_running_version_gets_the_uncertain_notice(runtime, versao):
    """O aviso decide pela versão que SUBIU: o ``rolled_back`` da guarda de boot pode ter falhado em reverter o código."""
    bot = bot_falso()
    write_pending(runtime, pendente(status="rolled_back"))

    await entregar(pool_com(bot), runtime, versao)

    descricao = bot.canal.send.await_args.kwargs["embed"].description
    assert "tentei voltar" in descricao and "não consegui confirmar a versão instalada" in descricao
    assert "Atualizado" not in descricao and "novidade" not in descricao


async def test_an_unconfirmed_version_gets_the_uncertain_notice(runtime):
    bot = bot_falso()
    write_pending(runtime, pendente())

    await entregar(pool_com(bot), runtime, None)

    assert "não consegui confirmar" in bot.canal.send.await_args.kwargs["embed"].description


async def test_without_the_channel_the_notice_goes_by_dm_to_who_asked(runtime):
    bot = bot_falso(canal=False)
    write_pending(runtime, pendente())

    await entregar(pool_com(bot), runtime, PARA)

    bot.canal.send.assert_not_awaited()
    bot.usuario.send.assert_awaited_once()
    assert sem_mencoes(bot.usuario.send.await_args)
    bot.get_user.assert_called_once_with(AUTOR)
    assert not pending_path(runtime).exists()


async def test_without_channel_and_dm_it_only_logs_and_does_not_keep_the_pending(runtime, caplog):
    bot = bot_falso(canal=False, usuario=False)
    write_pending(runtime, pendente())

    with caplog.at_level(logging.INFO):
        await entregar(pool_com(bot), runtime, PARA)

    assert not pending_path(runtime).exists()  # uma única entrega, mesmo que o envio falhe
    assert "novidade" not in caplog.text and "Atualizado" not in caplog.text


async def test_when_the_requesting_bot_is_gone_another_ready_bot_delivers(runtime):
    """F3: o bot que pediu pode ter falhado na subida (token inválido); outro bot pronto do pool entrega no mesmo canal."""
    outro = bot_falso(identificador=999)
    write_pending(runtime, pendente())

    await entregar(pool_com(outro), runtime, PARA)

    outro.canal.send.assert_awaited_once()
    outro.get_channel.assert_called_once_with(CANAL)
    assert not pending_path(runtime).exists()


async def test_a_pending_update_older_than_24_hours_is_dropped_without_a_notice(runtime):
    bot = bot_falso()
    write_pending(runtime, pendente(created_at=AGORA - 25 * 3600))

    await entregar(pool_com(bot), runtime, PARA)

    bot.canal.send.assert_not_awaited()
    assert not pending_path(runtime).exists()


async def test_without_a_pending_update_nothing_happens(runtime):
    bot = bot_falso()

    await entregar(pool_com(bot), runtime, PARA)

    bot.canal.send.assert_not_awaited()


# ============================================================================ A14b — on_pool_ready (F3)


def ao_ficar_pronto(pool, runtime, versao=PARA):
    async def detectar(pendente_lido):
        return versao

    return on_pool_ready(pool, runtime_dir=runtime, now=lambda: AGORA, detect_version=detectar)


async def test_the_pending_is_marked_ready_before_any_send(runtime):
    outro = bot_falso(identificador=999)
    write_pending(runtime, pendente(boots=2))
    visto = []

    async def enviar(*args, **kwargs):
        visto.append(
            json.loads(pending_path(runtime).read_text())["status"] if pending_path(runtime).exists() else "apagado"
        )

    outro.canal.send = enviar

    await ao_ficar_pronto(pool_com(outro), runtime)

    assert visto and visto[0] in ("ready", "apagado")


async def test_the_guard_after_a_ready_pool_neither_counts_nor_reverts(runtime):
    outro = bot_falso(identificador=999)
    write_pending(runtime, pendente(boots=2))
    await ao_ficar_pronto(pool_com(outro), runtime)
    chamadas = []

    async def runner(*args, **kwargs):
        chamadas.append(args)

    boot_guard.run(runtime, runtime, AGORA, runner=runner)  # não levanta SystemExit

    assert chamadas == []


async def test_an_unhealthy_pool_changes_nothing_and_sends_nothing(runtime):
    outro = bot_falso(identificador=999)
    write_pending(runtime, pendente(boots=1))
    antes = pending_path(runtime).read_bytes()

    await ao_ficar_pronto(pool_com(outro, saudavel=False), runtime)

    assert pending_path(runtime).read_bytes() == antes
    assert outro.canal.send.await_count == 0 and outro.usuario.send.await_count == 0


async def test_calling_on_pool_ready_twice_sends_only_once(runtime):
    outro = bot_falso(identificador=999)
    write_pending(runtime, pendente())
    pool = pool_com(outro)

    await ao_ficar_pronto(pool, runtime)
    await ao_ficar_pronto(pool, runtime)

    outro.canal.send.assert_awaited_once()


# ============================================================================ D9: ``applying`` não é subida concluída


async def test_deliver_update_notice_ignores_an_applying_mark_and_keeps_the_file(runtime):
    bot = bot_falso()
    write_pending(runtime, pendente(boots=0, status="applying"))
    antes = pending_path(runtime).read_bytes()

    await entregar(pool_com(bot), runtime, PARA)

    bot.canal.send.assert_not_awaited()
    assert pending_path(runtime).read_bytes() == antes


# ============================================================================ D11: aviso próprio de desistência


@pytest.mark.parametrize("versao", [DE, PARA, None], ids=["head-no-anterior", "head-no-alvo", "versao-ilegivel"])
async def test_a_given_up_restore_has_its_own_notice_whatever_the_head_is(runtime, versao):
    """``rolled_back`` com 4 tentativas = o ``boot_guard`` desistiu: o aviso diz isso, mesmo que o HEAD esteja na versão anterior."""
    bot = bot_falso()
    write_pending(runtime, pendente(status="rolled_back", restore_attempts=4))

    await entregar(pool_com(bot), runtime, versao)

    descricao = bot.canal.send.await_args.kwargs["embed"].description
    assert "desisti" in descricao and "3 tentativas" in descricao and "Confira o servidor" in descricao
    assert "2.0.0" in descricao and "2.1.0" in descricao
    assert "voltou para a versão" not in descricao  # nada do texto de rollback bem-sucedido
    assert not pending_path(runtime).exists()
