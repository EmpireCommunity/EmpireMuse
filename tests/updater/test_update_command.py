"""A16 (item 18, P2) — o comando ``update`` (``Owner.update.callback(cog, ctx)``), ``ConfirmView`` e a trava ``pool.update_lock``.

Cog, pool, contexto e interações são fakes; ``plan_git_update``/``apply_git_update`` e as consultas de versão são trocados no módulo do
comando. Sem relógio real: a expiração da janela é ``view.on_timeout()`` e a corrida entre dois acionamentos usa um ``Event``.
"""

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
import pytest_asyncio

from empiremuse.rules.owner_gate import OwnerGateMode
from empiremuse import paths
from empiremuse.cogs import legacy_cmds
from empiremuse.cogs.legacy_cmds import Owner
from empiremuse.rules.release import Version
from empiremuse.updater import releases
from empiremuse.updater.git_flow import EXIT_RESTART, GitPlan, UpdateError
from tests.support.alvo_git import ALVO_GIT
from empiremuse.updater.state import PendingUpdate, UpdateRequest, read_pending, write_pending
from empiremuse.updater.views import ConfirmView
from empiremuse.utils import client
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.others import CustomContext

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

DONO, BOT_ID, CANAL = 42, 111, 555
DE, PARA = Version(2, 0, 0), Version(2, 1, 0)
SHA = "a" * 40
PLANO = GitPlan(
    current=DE,
    target=PARA,
    tag_object_sha="b" * 40,
    target_commit="c" * 40,
    prev_sha=SHA,
    changelog="- novidade da 2.1.0",
)


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    projeto = tmp_path / "projeto"
    projeto.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(paths, "PROJECT_DIR", projeto)
    monkeypatch.setenv("EMPIREMUSE_HOME", str(runtime))
    monkeypatch.delenv("EMPIREMUSE_VERSION", raising=False)
    monkeypatch.setenv("INVOCATION_ID", "systemd")
    return SimpleNamespace(projeto=projeto, runtime=runtime)


@pytest.fixture
def cog():
    pool = SimpleNamespace(
        update_lock=asyncio.Lock(),
        shutdown=AsyncMock(),
        secret_values=lambda: frozenset(),
        config={"RUN_RPC_SERVER": False},
        update_ready_checked=False,
        update_watch_task=None,
    )
    bot = SimpleNamespace(
        pool=pool,
        config={},
        user=SimpleNamespace(id=BOT_ID),
        owner_gate=OwnerGateMode.ENV,
        env_owner_ids={DONO},
        get_color=lambda *args: 0,
    )
    cog = Owner.__new__(Owner)
    cog.bot = bot
    cog.log_owner_command = Mock()
    return cog


def ctx_prefixo(autor=DONO):
    ctx = MagicMock(spec=CustomContext)
    ctx.author = SimpleNamespace(id=autor)
    ctx.channel = SimpleNamespace(id=CANAL)
    ctx.send = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    return ctx


def ctx_painel(autor=DONO):
    ctx = MagicMock()
    ctx.author = SimpleNamespace(id=autor)
    ctx.channel_id = CANAL
    ctx.response.defer = AsyncMock()
    ctx.followup.send = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    return ctx


def modo_git(ambiente, monkeypatch, plano=PLANO):
    (ambiente.projeto / ".git").mkdir()
    planejar = AsyncMock(return_value=plano)
    monkeypatch.setattr(legacy_cmds, "plan_git_update", planejar)
    return planejar


def clique(autor=DONO):
    interacao = MagicMock()
    interacao.author = SimpleNamespace(id=autor)
    interacao.send = AsyncMock()
    interacao.response.edit_message = AsyncMock()
    interacao.edit_original_message = AsyncMock()
    return interacao


async def oferecer(cog, ctx):
    await Owner.update.callback(cog, ctx)
    return ctx.send.await_args.kwargs["view"]


def pendente_em_disco(runtime):
    write_pending(
        runtime,
        PendingUpdate(
            flow="git",
            from_version=DE,
            to_version=PARA,
            bot_id=1,
            channel_id=2,
            requested_by=3,
            created_at=time.time(),
            prev_sha=SHA,
            target_commit=ALVO_GIT,
        ),
    )


# ============================================================================ recusas


async def test_a_non_owner_is_refused_even_through_the_panel(cog, ambiente, monkeypatch):
    planejar = modo_git(ambiente, monkeypatch)

    with pytest.raises(GenericError):
        await Owner.update.callback(cog, ctx_painel(autor=7))

    planejar.assert_not_awaited()
    assert not cog.bot.pool.update_lock.locked()


async def test_an_existing_pending_update_refuses_a_second_one(cog, ambiente, monkeypatch):
    planejar = modo_git(ambiente, monkeypatch)
    pendente_em_disco(ambiente.runtime)

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "andamento" in erro.value.text
    planejar.assert_not_awaited()
    assert not cog.bot.pool.update_lock.locked()


async def test_an_applying_mark_refuses_a_second_update_even_after_a_day(cog, ambiente, monkeypatch):
    """D9: o ``applying`` (árvore meio aplicada) conta como atualização em andamento e não expira por idade."""
    planejar = modo_git(ambiente, monkeypatch)
    write_pending(
        ambiente.runtime,
        PendingUpdate(
            flow="git",
            from_version=DE,
            to_version=PARA,
            bot_id=1,
            channel_id=2,
            requested_by=3,
            created_at=time.time() - 3 * 24 * 3600,
            prev_sha=SHA,
            status="applying",
            target_commit=ALVO_GIT,
        ),
    )

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "andamento" in erro.value.text
    planejar.assert_not_awaited()


async def test_a_held_lock_refuses_a_second_update_and_is_not_released_by_the_refusal(cog, ambiente, monkeypatch):
    planejar = modo_git(ambiente, monkeypatch)
    await cog.bot.pool.update_lock.acquire()

    with pytest.raises(GenericError):
        await Owner.update.callback(cog, ctx_prefixo())

    planejar.assert_not_awaited()
    assert cog.bot.pool.update_lock.locked()


async def test_two_updates_at_the_same_time_only_one_proceeds(cog, ambiente, monkeypatch):
    liberar, planejando = asyncio.Event(), asyncio.Event()

    async def planejar_devagar(*args, **kwargs):
        planejando.set()
        await liberar.wait()
        return None

    (ambiente.projeto / ".git").mkdir()
    monkeypatch.setattr(legacy_cmds, "plan_git_update", planejar_devagar)
    primeiro = asyncio.create_task(Owner.update.callback(cog, ctx_prefixo()))
    try:
        await asyncio.wait_for(planejando.wait(), timeout=5)  # o 1º já tem a trava e espera o plano
        with pytest.raises(GenericError):
            await Owner.update.callback(cog, ctx_prefixo())
    finally:
        liberar.set()
        await asyncio.wait_for(primeiro, timeout=5)

    assert not cog.bot.pool.update_lock.locked()


# ============================================================================ confirmação: git


async def test_the_confirmation_is_offered_and_keeps_the_lock(cog, ambiente, monkeypatch):
    modo_git(ambiente, monkeypatch)
    ctx = ctx_prefixo()

    view = await oferecer(cog, ctx)

    assert isinstance(view, ConfirmView) and view.requested_by == DONO and view.timeout == 60
    descricao = ctx.send.await_args.kwargs["embed"].description
    assert (
        "2.0.0" in descricao
        and "2.1.0" in descricao
        and "novidade da 2.1.0" in descricao
        and "indisponível" in descricao
    )
    assert cog.bot.pool.update_lock.locked()
    cog.log_owner_command.assert_called_once_with(ctx, "update", "git 2.0.0->2.1.0")


async def test_without_a_supervisor_the_confirmation_warns_the_bot_will_stop(cog, ambiente, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID")
    modo_git(ambiente, monkeypatch)
    ctx = ctx_prefixo()

    await oferecer(cog, ctx)

    assert "vai parar" in ctx.send.await_args.kwargs["embed"].description


async def test_through_the_panel_the_confirmation_is_ephemeral_and_the_command_returns_text(cog, ambiente, monkeypatch):
    modo_git(ambiente, monkeypatch)
    ctx = ctx_painel()

    texto = await Owner.update.callback(cog, ctx)

    assert "só para você" in texto
    assert ctx.followup.send.await_args.kwargs["ephemeral"] is True
    assert isinstance(ctx.followup.send.await_args.kwargs["view"], ConfirmView)
    assert cog.bot.pool.update_lock.locked()


async def test_already_on_the_latest_version_says_so_and_frees_the_lock(cog, ambiente, monkeypatch):
    modo_git(ambiente, monkeypatch, plano=None)
    ctx = ctx_prefixo()

    await Owner.update.callback(cog, ctx)

    assert "mais recente" in ctx.send.await_args.kwargs["embed"].description
    assert not cog.bot.pool.update_lock.locked()


async def test_a_planning_error_is_shown_and_frees_the_lock(cog, ambiente, monkeypatch):
    (ambiente.projeto / ".git").mkdir()
    monkeypatch.setattr(
        legacy_cmds, "plan_git_update", AsyncMock(side_effect=UpdateError("Há alterações locais: app.txt"))
    )

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "app.txt" in erro.value.text and not cog.bot.pool.update_lock.locked()


async def test_only_the_requester_can_click(cog, ambiente, monkeypatch):
    modo_git(ambiente, monkeypatch)
    view = await oferecer(cog, ctx_prefixo())
    intruso = clique(autor=7)

    assert await view.interaction_check(intruso) is False
    intruso.send.assert_awaited_once()
    assert intruso.send.await_args.kwargs["ephemeral"] is True
    assert await view.interaction_check(clique()) is True
    assert cog.bot.pool.update_lock.locked()


async def test_cancel_releases_the_lock_and_allows_a_new_update(cog, ambiente, monkeypatch):
    planejar = modo_git(ambiente, monkeypatch)
    view = await oferecer(cog, ctx_prefixo())
    interacao = clique()

    await view.cancel.callback(interacao)

    assert not cog.bot.pool.update_lock.locked()
    assert "cancelada" in interacao.response.edit_message.await_args.kwargs["content"]
    await oferecer(cog, ctx_prefixo())
    assert planejar.await_count == 2


async def test_expiry_releases_the_lock_once_and_edits_the_message(cog, ambiente, monkeypatch):
    modo_git(ambiente, monkeypatch)
    ctx = ctx_prefixo()
    view = await oferecer(cog, ctx)

    await view.on_timeout()
    await view.on_timeout()

    assert not cog.bot.pool.update_lock.locked()
    mensagem = ctx.send.return_value
    assert "expirou" in mensagem.edit.await_args.kwargs["content"]


async def test_confirm_applies_the_update_and_restarts_with_75(cog, ambiente, monkeypatch):
    modo_git(ambiente, monkeypatch)
    aplicar = AsyncMock()
    monkeypatch.setattr(legacy_cmds, "apply_git_update", aplicar)
    view = await oferecer(cog, ctx_prefixo())
    interacao = clique()

    await view.confirm.callback(interacao)

    aplicar.assert_awaited_once()
    plano, pedido, repo, runtime = aplicar.await_args.args
    assert plano is PLANO and pedido == UpdateRequest(bot_id=BOT_ID, channel_id=CANAL, requested_by=DONO)
    assert Path(repo) == paths.PROJECT_DIR and Path(runtime) == ambiente.runtime
    cog.bot.pool.shutdown.assert_awaited_once_with(EXIT_RESTART)
    assert "Reiniciando" in interacao.edit_original_message.await_args.kwargs["embed"].description
    assert not cog.bot.pool.update_lock.locked()


async def test_the_old_process_stops_its_ready_check_before_the_update_is_applied(cog, ambiente, monkeypatch):
    """I1: o pendente que o apply grava é do processo novo; no instante do apply o velho já não pode marcá-lo como pronto."""
    modo_git(ambiente, monkeypatch)
    pool = cog.bot.pool
    pool.update_watch_task = Mock()
    vistos = {}

    async def aplicar(*args):
        vistos["checado"] = pool.update_ready_checked
        vistos["cancelamentos"] = pool.update_watch_task.cancel.call_count

    monkeypatch.setattr(legacy_cmds, "apply_git_update", aplicar)
    view = await oferecer(cog, ctx_prefixo())

    await view.confirm.callback(clique())

    assert vistos == {"checado": True, "cancelamentos": 1}


async def test_the_old_process_does_not_mark_the_new_pending_update_as_ready(cog, ambiente, monkeypatch):
    """I1 (corrida): pendente novo gravado pelo apply + pool saudável depois dele: quem o promove é o processo novo."""
    modo_git(ambiente, monkeypatch)
    pool = client.BotPool.__new__(client.BotPool)
    pool.update_lock, pool.shutdown = asyncio.Lock(), AsyncMock()
    pool.config, pool.bots, pool.pool_is_healthy = {"RUN_RPC_SERVER": False}, [], lambda: True
    cog.bot.pool = pool

    async def aplicar(*args):
        pendente_em_disco(ambiente.runtime)

    monkeypatch.setattr(legacy_cmds, "apply_git_update", aplicar)
    view = await oferecer(cog, ctx_prefixo())
    await view.confirm.callback(clique())

    await pool.check_pool_ready()

    assert read_pending(ambiente.runtime, time.time()).status == "pending"


async def test_a_second_confirm_click_does_not_apply_twice(cog, ambiente, monkeypatch):
    modo_git(ambiente, monkeypatch)
    aplicar = AsyncMock()
    monkeypatch.setattr(legacy_cmds, "apply_git_update", aplicar)
    view = await oferecer(cog, ctx_prefixo())

    await view.confirm.callback(clique())
    await view.confirm.callback(clique())

    aplicar.assert_awaited_once()


@pytest.mark.parametrize(
    ("erro", "mostra"),
    [
        (UpdateError("O teste de fumaça do código novo falhou; voltei ao anterior."), "fumaça"),
        (RuntimeError("boom"), "Falha inesperada"),
    ],
    ids=["update-error", "erro-inesperado"],
)
async def test_a_failed_update_does_not_restart_and_frees_the_lock(cog, ambiente, monkeypatch, erro, mostra):
    modo_git(ambiente, monkeypatch)
    monkeypatch.setattr(legacy_cmds, "apply_git_update", AsyncMock(side_effect=erro))
    view = await oferecer(cog, ctx_prefixo())
    interacao = clique()

    await view.confirm.callback(interacao)

    cog.bot.pool.shutdown.assert_not_awaited()
    descricao = interacao.edit_original_message.await_args.kwargs["embed"].description
    assert descricao.startswith("❌") and mostra in descricao and "boom" not in descricao
    assert not cog.bot.pool.update_lock.locked()


# ============================================================================ modos sem atualização automática


def releases_do_github(monkeypatch, *, versao=PARA, falha=None, antes=None):
    """O modo ``panel`` (imagem sem git) consulta as releases do GitHub; a lista e o CHANGELOG são trocados nos dois lugares possíveis."""

    async def lista(session):
        if antes:
            antes()
        if falha:
            raise falha
        return [releases.Release(versao, f"v{versao}", "https://exemplo.invalido/image-digest.txt")]

    async def changelog(session, tag):
        return "## [2.1.0]\n- novidade da 2.1.0\n"

    for nome, fn in (("list_releases", lista), ("fetch_changelog", changelog)):
        monkeypatch.setattr(releases, nome, fn)
        if hasattr(legacy_cmds, nome):
            monkeypatch.setattr(legacy_cmds, nome, fn)

    def sem_ls_remote(*args, **kwargs):
        raise AssertionError("a imagem não tem git: o modo painel não pode usar o ls-remote")

    monkeypatch.setattr(legacy_cmds, "latest_official_version", sem_ls_remote)
    monkeypatch.setattr(legacy_cmds, "fetch_remote_changelog", sem_ls_remote)


@pytest.fixture
def versoes(monkeypatch):
    monkeypatch.setattr(legacy_cmds, "latest_official_version", AsyncMock(return_value=PARA))
    monkeypatch.setattr(legacy_cmds, "fetch_remote_changelog", lambda tag: "## [2.1.0]\n- novidade da 2.1.0\n")


async def test_the_panel_mode_shows_the_version_and_points_to_the_host_panel(cog, ambiente, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_VERSION", "2.0.2")
    releases_do_github(monkeypatch)

    texto = await Owner.update.callback(cog, ctx_painel())

    assert "2.1.0" in texto and "2.0.2" in texto and "novidade da 2.1.0" in texto and "painel da hospedagem" in texto
    assert not cog.bot.pool.update_lock.locked()


async def test_the_panel_mode_on_the_latest_version_says_so(cog, ambiente, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_VERSION", "2.1.0")
    releases_do_github(monkeypatch)

    texto = await Owner.update.callback(cog, ctx_painel())

    assert "mais recente" in texto


# ============================================================================ defer do painel antes de qualquer I/O de rede


def modo_com_consulta_registrada(modo: str, ambiente, monkeypatch, ctx) -> None:
    """Prepara o modo e troca a consulta de rede por uma que anota quantos ``defer`` já tinham sido aguardados."""
    vistos = ctx.defers_na_rede = []

    async def consulta(*args, **kwargs):
        vistos.append(ctx.response.defer.await_count)
        return PLANO if modo == "git" else PARA

    if modo == "git":
        (ambiente.projeto / ".git").mkdir()
        monkeypatch.setattr(legacy_cmds, "plan_git_update", consulta)
        return
    if modo == "painel":
        monkeypatch.setenv("EMPIREMUSE_VERSION", "2.0.2")
        releases_do_github(monkeypatch, antes=lambda: vistos.append(ctx.response.defer.await_count))
        return
    monkeypatch.setattr(legacy_cmds, "latest_official_version", consulta)
    monkeypatch.setattr(legacy_cmds, "fetch_remote_changelog", lambda tag: "## [2.1.0]\n- novidade\n")


@pytest.mark.parametrize("modo", ["git", "manual", "painel"])
async def test_the_panel_interaction_is_deferred_once_before_any_network_io(cog, ambiente, monkeypatch, modo):
    """O painel só tem 3 s para responder: o defer vem antes da consulta de versão/plano (git ls-remote, API)."""
    ctx = ctx_painel()
    modo_com_consulta_registrada(modo, ambiente, monkeypatch, ctx)

    await Owner.update.callback(cog, ctx)

    assert ctx.defers_na_rede == [1]
    ctx.response.defer.assert_awaited_once()


@pytest.mark.parametrize("modo", ["git", "manual"])
async def test_a_prefix_command_is_never_deferred(cog, ambiente, monkeypatch, modo):
    ctx = ctx_prefixo()
    ctx.response = MagicMock(defer=AsyncMock())
    ctx.defer = AsyncMock()
    modo_com_consulta_registrada(modo, ambiente, monkeypatch, ctx)

    await Owner.update.callback(cog, ctx)

    ctx.response.defer.assert_not_awaited()
    ctx.defer.assert_not_awaited()
    assert ctx.defers_na_rede == [0]


# ============================================================================ P4: modo docker (proxy configurado em UPDATE_PROXY_SOCKET)

SOCKET = "/run/empiremuse/updater.sock"
PLANO_DOCKER = SimpleNamespace(current=DE, target=PARA, digest="sha256:" + "ab" * 32, changelog="- novidade da 2.1.0")


class Cronologia:
    """Anota a ordem entre a parada da checagem de ready, o início do auxiliar e o acompanhamento."""

    def __init__(self, pool) -> None:
        self.pool = pool
        self.eventos: list[str] = []
        self.ready_checked_no_inicio: bool | None = None
        self.args_inicio: tuple[Any, ...] = ()
        self.args_vigia: tuple[Any, ...] = ()
        self.socket: str | None = None


@pytest.fixture
def docker(cog, ambiente, monkeypatch):
    """Modo docker: socket configurado, imagem 2.0.0 e o fluxo (plano, início, vigia, cliente) trocado por mocks no módulo do comando."""
    cog.bot.config = {"UPDATE_PROXY_SOCKET": SOCKET}
    monkeypatch.setenv("EMPIREMUSE_VERSION", "2.0.0")
    cron = Cronologia(cog.bot.pool)
    pendente = SimpleNamespace(marca="pendente-docker", channel_id=CANAL, requested_by=DONO)

    async def planejar(*args, **kwargs):
        cron.eventos.append("plano")
        cron.args_plano = (args, kwargs)
        return PLANO_DOCKER

    async def iniciar(*args, **kwargs):
        cron.eventos.append("inicio")
        cron.ready_checked_no_inicio = cog.bot.pool.update_ready_checked
        cron.args_inicio = args
        return pendente

    async def vigiar(*args, **kwargs):
        cron.eventos.append("vigia")
        cron.args_vigia = args

    def fabrica(caminho):
        cron.socket = caminho
        return SimpleNamespace(socket=caminho)

    monkeypatch.setattr(legacy_cmds, "plan_docker_update", planejar)
    monkeypatch.setattr(legacy_cmds, "start_docker_update", iniciar)
    monkeypatch.setattr(legacy_cmds, "watch_helper", vigiar)
    monkeypatch.setattr(legacy_cmds, "ProxyClient", fabrica)
    cron.pendente = pendente
    cron.planejar, cron.iniciar = planejar, iniciar
    return cron


async def esperar_tarefas() -> None:
    atual = asyncio.current_task()
    await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not atual), return_exceptions=True)


async def test_the_docker_mode_offers_the_update_planned_from_the_image_version(cog, docker):
    ctx = ctx_prefixo()

    view = await oferecer(cog, ctx)

    args, kwargs = docker.args_plano
    assert (args[1] if len(args) > 1 else kwargs["current"]) == DE  # a versão da imagem (EMPIREMUSE_VERSION)
    descricao = ctx.send.await_args.kwargs["embed"].description
    assert (
        "2.0.0" in descricao
        and "2.1.0" in descricao
        and "novidade da 2.1.0" in descricao
        and "indisponível" in descricao
    )
    assert view is not None and cog.bot.pool.update_lock.locked()  # a trava passou para a janela


async def test_without_a_newer_release_the_docker_mode_says_so_and_frees_the_lock(cog, docker, monkeypatch):
    monkeypatch.setattr(legacy_cmds, "plan_docker_update", AsyncMock(return_value=None))
    ctx = ctx_prefixo()

    await Owner.update.callback(cog, ctx)

    assert "mais recente" in ctx.send.await_args.kwargs["embed"].description
    assert not cog.bot.pool.update_lock.locked()


async def test_a_planning_error_becomes_a_friendly_message_and_frees_the_lock(cog, docker, monkeypatch):
    monkeypatch.setattr(
        legacy_cmds,
        "plan_docker_update",
        AsyncMock(side_effect=UpdateError("Não consegui consultar as versões publicadas agora.")),
    )

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "Não consegui consultar as versões publicadas agora." in erro.value.text
    assert not cog.bot.pool.update_lock.locked()


async def test_confirming_stops_the_ready_check_then_starts_the_helper_then_watches_it(cog, docker):
    view = await oferecer(cog, ctx_prefixo())
    interacao = clique()

    await view.confirm.callback(interacao)
    await esperar_tarefas()

    assert docker.eventos == ["plano", "inicio", "vigia"]
    assert docker.ready_checked_no_inicio is True  # a checagem de ready do processo velho já estava encerrada
    assert docker.socket == SOCKET
    cliente, plano, pedido = docker.args_inicio[0], docker.args_inicio[1], docker.args_inicio[2]
    assert cliente.socket == SOCKET and plano is PLANO_DOCKER
    assert pedido == UpdateRequest(bot_id=BOT_ID, channel_id=CANAL, requested_by=DONO)
    assert docker.args_vigia[1] is docker.pendente  # o acompanhamento recebe o pendente devolvido
    interacao.edit_original_message.assert_awaited()
    assert not cog.bot.pool.update_lock.locked()


async def test_a_start_error_is_shown_in_the_channel_without_watching_and_frees_the_lock(cog, docker, monkeypatch):
    monkeypatch.setattr(
        legacy_cmds, "start_docker_update", AsyncMock(side_effect=UpdateError("proxy de atualização inacessível"))
    )
    view = await oferecer(cog, ctx_prefixo())
    interacao = clique()

    await view.confirm.callback(interacao)
    await esperar_tarefas()

    descricao = interacao.edit_original_message.await_args.kwargs["embed"].description
    assert descricao.startswith("❌") and "proxy de atualização inacessível" in descricao
    assert "vigia" not in docker.eventos and not cog.bot.pool.update_lock.locked()
    cog.bot.pool.shutdown.assert_not_awaited()


async def test_the_docker_mode_refuses_a_second_update_while_one_is_pending(cog, docker, ambiente):
    pendente_em_disco(ambiente.runtime)

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "andamento" in erro.value.text and "plano" not in docker.eventos


async def test_a_failed_rollback_tells_the_channel_and_still_restarts_so_the_boot_guard_reverts(
    cog, ambiente, monkeypatch
):
    """D9: o restore falhou, a marca ``applying`` ficou no disco; o processo precisa reiniciar para o boot_guard reverter limpo."""
    from empiremuse.updater.git_flow import UpdateRollbackFailed

    modo_git(ambiente, monkeypatch)
    erro = UpdateRollbackFailed("A reversão falhou; o bot volta para a versão anterior no próximo início.")
    monkeypatch.setattr(legacy_cmds, "apply_git_update", AsyncMock(side_effect=erro))
    view = await oferecer(cog, ctx_prefixo())
    interacao = clique()

    await view.confirm.callback(interacao)

    descricao = interacao.edit_original_message.await_args.kwargs["embed"].description
    assert descricao.startswith("❌") and "próximo início" in descricao
    cog.bot.pool.shutdown.assert_awaited_once_with(EXIT_RESTART)
    assert not cog.bot.pool.update_lock.locked()


async def test_a_failed_rollback_is_recorded_as_an_error_in_the_log(cog, ambiente, monkeypatch, caplog):
    from empiremuse.updater.git_flow import UpdateRollbackFailed

    modo_git(ambiente, monkeypatch)
    erro = UpdateRollbackFailed("A reversão falhou; o bot volta para a versão anterior no próximo início.")
    monkeypatch.setattr(legacy_cmds, "apply_git_update", AsyncMock(side_effect=erro))
    view = await oferecer(cog, ctx_prefixo())

    with caplog.at_level(logging.ERROR):
        await view.confirm.callback(clique())

    assert [r for r in caplog.records if r.levelno >= logging.ERROR and r.name == legacy_cmds.logger.name]
    cog.bot.pool.shutdown.assert_awaited_once_with(EXIT_RESTART)  # o reinício continua


# ---------------------------------------------------------------------------- P4: critérios do contrato (A16, A24, A26) que o modo docker também cumpre


async def test_a_non_owner_is_refused_in_the_docker_mode_even_through_the_panel(cog, docker):

    with pytest.raises(GenericError):
        await Owner.update.callback(cog, ctx_painel(autor=7))

    assert docker.eventos == [] and not cog.bot.pool.update_lock.locked()


async def test_a_held_pool_lock_refuses_a_second_docker_update_without_planning(cog, docker):
    await cog.bot.pool.update_lock.acquire()

    with pytest.raises(GenericError):
        await Owner.update.callback(cog, ctx_prefixo())

    assert docker.eventos == [] and cog.bot.pool.update_lock.locked()  # a recusa não solta a trava de quem a tem


async def test_only_the_author_can_confirm_the_docker_update(cog, docker):
    """O framework chama ``interaction_check`` antes do botão: um clique de outro usuário não passa e nada é iniciado."""
    view = await oferecer(cog, ctx_prefixo())

    assert await view.interaction_check(clique(autor=999)) is False
    assert await view.interaction_check(clique(autor=DONO)) is True
    await esperar_tarefas()

    assert docker.eventos == ["plano"] and cog.bot.pool.update_lock.locked()


async def test_an_expired_docker_confirmation_frees_the_lock_and_starts_nothing(cog, docker):
    ctx = ctx_prefixo()
    view = await oferecer(cog, ctx)

    await view.on_timeout()

    assert docker.eventos == ["plano"] and not cog.bot.pool.update_lock.locked()
    assert "expirou" in ctx.send.return_value.edit.await_args.kwargs["content"]


async def test_confirming_cancels_the_old_ready_watch_task(cog, docker):
    cog.bot.pool.update_watch_task = Mock()
    tarefa = cog.bot.pool.update_watch_task
    view = await oferecer(cog, ctx_prefixo())

    await view.confirm.callback(clique())
    await esperar_tarefas()

    tarefa.cancel.assert_called_once()


async def test_an_unreachable_proxy_at_the_start_is_friendly_and_leaves_no_pending_file(
    cog, docker, ambiente, monkeypatch
):
    """A26: com o início REAL (não o mock), um proxy inacessível não deixa pendente nem vigia."""
    from empiremuse.updater import docker_flow

    class ProxyMorto:
        def __init__(self, caminho) -> None:
            pass

        async def helper_state(self):
            raise UpdateError("proxy de atualização inacessível")

    monkeypatch.setattr(legacy_cmds, "start_docker_update", docker_flow.start_docker_update)
    monkeypatch.setattr(legacy_cmds, "ProxyClient", ProxyMorto)
    view = await oferecer(cog, ctx_prefixo())
    interacao = clique()

    await view.confirm.callback(interacao)
    await esperar_tarefas()

    descricao = interacao.edit_original_message.await_args.kwargs["embed"].description
    assert "proxy de atualização inacessível" in descricao and SOCKET not in descricao
    assert "vigia" not in docker.eventos and not (ambiente.runtime / "update-pending.json").exists()
    assert not cog.bot.pool.update_lock.locked()


async def test_the_report_given_to_the_watcher_writes_to_the_origin_channel_and_dms_the_requester(cog, docker):
    canal, usuario = SimpleNamespace(send=AsyncMock()), SimpleNamespace(send=AsyncMock())
    cog.bot.get_channel = Mock(return_value=canal)
    cog.bot.get_user = Mock(return_value=usuario)
    view = await oferecer(cog, ctx_prefixo())
    await view.confirm.callback(clique())
    await esperar_tarefas()
    relatorio = docker.args_vigia[3]

    await relatorio.channel("significado do código")
    await relatorio.dm("linha 1\nlinha 2")

    cog.bot.get_channel.assert_called_with(CANAL)
    cog.bot.get_user.assert_called_with(DONO)

    def texto(mock) -> str:
        chamada = mock.send.await_args
        embed = chamada.kwargs.get("embed")
        return (chamada.args[0] if chamada.args else chamada.kwargs.get("content") or "") + (
            embed.description if embed else ""
        )

    assert "significado do código" in texto(canal) and "linha 2" in texto(usuario)
    for destino in (canal, usuario):
        permitido = destino.send.await_args.kwargs["allowed_mentions"]
        assert permitido.everyone is False and permitido.users is False and permitido.roles is False


# ---------------------------------------------------------------------------- D12: pendente docker já existente que o comando consegue resolver


def pendente_docker_em_disco(runtime, *, status: str = "pending") -> PendingUpdate:
    pendente = PendingUpdate(
        flow="docker",
        from_version=DE,
        to_version=PARA,
        bot_id=1,
        channel_id=CANAL,
        requested_by=777,
        created_at=time.time(),
        digest="sha256:" + "ab" * 32,
        status=status,
    )
    write_pending(runtime, pendente)
    return pendente


class AuxiliarFalso:
    """Cliente do proxy para o D12: ``estado`` (ou ``None``), falhas por chamada e o registro da ordem."""

    def __init__(self, ctx=None) -> None:
        from empiremuse.updater.proxy_client import HelperState

        self.HelperState = HelperState
        self.estado: Any = None
        self.falhas: dict[str, Exception] = {}
        self.eventos: list[str] = []
        self.defers_no_estado: int | None = None
        self.ctx = ctx

    def parado(self, codigo: int) -> "AuxiliarFalso":
        self.estado = self.HelperState("exited", False, codigo, "ini", "fim")
        return self

    def rodando(self) -> "AuxiliarFalso":
        self.estado = self.HelperState("running", True, 0, "ini", "")
        return self

    async def helper_state(self):
        self.eventos.append("state")
        if self.ctx is not None:
            self.defers_no_estado = self.ctx.response.defer.await_count
        if "state" in self.falhas:
            raise self.falhas["state"]
        return self.estado

    async def remove_helper(self):
        self.eventos.append("remove")
        if "remove" in self.falhas:
            raise self.falhas["remove"]


@pytest.fixture
def d12(cog, docker, ambiente, monkeypatch):
    """Modo docker com o cliente do proxy, o envio do log e o relatório do Discord trocados no módulo do comando."""
    auxiliar = AuxiliarFalso()
    monkeypatch.setattr(legacy_cmds, "ProxyClient", lambda caminho: auxiliar)
    enviar_log = AsyncMock()
    monkeypatch.setattr(legacy_cmds, "send_helper_log", enviar_log, raising=False)
    relatorios: list[tuple[Any, Any]] = []

    def relatorio(bot, pendente):
        relatorios.append((bot, pendente))
        return SimpleNamespace(bot=bot, pendente=pendente)

    monkeypatch.setattr(legacy_cmds, "DiscordReport", relatorio, raising=False)
    cog.secret_values = Mock(return_value=frozenset({"SEGREDO-1"}))
    return SimpleNamespace(
        auxiliar=auxiliar, enviar_log=enviar_log, relatorios=relatorios, cron=docker, runtime=ambiente.runtime
    )


def texto_do_primeiro_envio(ctx) -> str:
    chamada = ctx.send.await_args_list[0]
    embed = chamada.kwargs.get("embed")
    return (chamada.args[0] if chamada.args else chamada.kwargs.get("content") or "") + (
        embed.description if embed else ""
    )


@pytest.mark.parametrize("codigo", [66, 70, 71])
async def test_a_stopped_helper_with_a_failure_is_reported_logged_by_dm_removed_and_the_new_update_goes_on(
    cog, d12, codigo
):
    from empiremuse.updater import docker_flow

    pendente = pendente_docker_em_disco(d12.runtime)
    d12.auxiliar.parado(codigo)
    ctx = ctx_prefixo()

    await Owner.update.callback(cog, ctx)

    assert docker_flow.describe_exit(codigo) in texto_do_primeiro_envio(
        ctx
    )  # o resultado da anterior, no canal do pedido
    d12.enviar_log.assert_awaited_once()
    cliente, relatorio, pend, segredos = d12.enviar_log.await_args.args
    assert cliente is d12.auxiliar and relatorio.pendente.requested_by == 777 == pend.requested_by
    assert segredos == frozenset({"SEGREDO-1"}) and pend.flow == "docker"
    assert d12.auxiliar.eventos == ["state", "remove"] and not (d12.runtime / "update-pending.json").exists()
    assert d12.cron.eventos == ["plano"] and ctx.send.await_count == 2  # o plano novo foi oferecido
    assert cog.bot.pool.update_lock.locked()  # a trava passou para a janela de confirmação
    assert pendente.status == "pending"


@pytest.mark.parametrize(("estado", "codigo"), [("parado", 0), ("ausente", None)], ids=["saiu-com-0", "ausente"])
async def test_a_helper_that_finished_well_or_is_gone_is_reported_without_dm_and_the_update_goes_on(
    cog, d12, estado, codigo
):
    from empiremuse.updater import docker_flow

    pendente_docker_em_disco(d12.runtime)
    if estado == "parado":
        d12.auxiliar.parado(0)
    ctx = ctx_prefixo()

    await Owner.update.callback(cog, ctx)

    assert docker_flow.describe_exit(codigo) in texto_do_primeiro_envio(ctx)
    d12.enviar_log.assert_not_awaited()
    assert "remove" in d12.auxiliar.eventos and not (d12.runtime / "update-pending.json").exists()
    assert d12.cron.eventos == ["plano"]


async def test_a_running_helper_refuses_without_removing_anything(cog, d12):
    pendente_docker_em_disco(d12.runtime)
    d12.auxiliar.rodando()

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "andamento" in erro.value.text and d12.auxiliar.eventos == ["state"]
    assert (d12.runtime / "update-pending.json").exists() and d12.cron.eventos == []
    assert not cog.bot.pool.update_lock.locked()


async def test_an_unreachable_proxy_refuses_and_keeps_the_pending_update(cog, d12):
    pendente_docker_em_disco(d12.runtime)
    d12.auxiliar.falhas["state"] = UpdateError("proxy de atualização inacessível")

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "andamento" in erro.value.text and (d12.runtime / "update-pending.json").exists()
    assert d12.cron.eventos == [] and not cog.bot.pool.update_lock.locked()


async def test_a_failing_removal_refuses_and_keeps_the_pending_update(cog, d12):
    pendente_docker_em_disco(d12.runtime)
    d12.auxiliar.parado(66)
    d12.auxiliar.falhas["remove"] = UpdateError("não removeu")

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "andamento" in erro.value.text and (d12.runtime / "update-pending.json").exists()
    assert d12.cron.eventos == [] and not cog.bot.pool.update_lock.locked()


async def test_a_ready_docker_pending_update_is_left_to_the_post_boot_notice_without_touching_the_proxy(
    cog, d12, monkeypatch
):
    pendente_docker_em_disco(d12.runtime, status="ready")

    def nunca(caminho):
        raise AssertionError("o proxy não pode ser tocado com o pendente em 'ready'")

    monkeypatch.setattr(legacy_cmds, "ProxyClient", nunca)

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "andamento" in erro.value.text and (d12.runtime / "update-pending.json").exists()


async def test_a_live_watcher_of_this_process_refuses_without_touching_the_proxy(cog, d12, monkeypatch):
    pendente_docker_em_disco(d12.runtime)
    viva = asyncio.get_running_loop().create_future()  # nunca concluída: faz o papel da tarefa do watch_helper
    monkeypatch.setattr(cog.bot.pool, "helper_watch_tasks", {viva}, raising=False)  # o registro vive no pool

    def nunca(caminho):
        raise AssertionError("o proxy não pode ser tocado com o vigia vivo")

    monkeypatch.setattr(legacy_cmds, "ProxyClient", nunca)

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    viva.cancel()
    assert "andamento" in erro.value.text and (d12.runtime / "update-pending.json").exists()


async def test_the_panel_defers_before_the_proxy_is_asked_about_the_old_helper(cog, d12):
    pendente_docker_em_disco(d12.runtime)
    ctx = ctx_painel()
    d12.auxiliar.ctx = ctx
    d12.auxiliar.parado(66)

    await Owner.update.callback(cog, ctx)

    assert d12.auxiliar.defers_no_estado == 1


async def test_a_held_pool_lock_still_refuses_before_any_proxy_access_with_a_docker_pending_update(
    cog, d12, monkeypatch
):
    pendente_docker_em_disco(d12.runtime)
    await cog.bot.pool.update_lock.acquire()

    def nunca(caminho):
        raise AssertionError("a trava ocupada recusa antes de qualquer acesso ao proxy")

    monkeypatch.setattr(legacy_cmds, "ProxyClient", nunca)

    with pytest.raises(GenericError):
        await Owner.update.callback(cog, ctx_prefixo())

    assert cog.bot.pool.update_lock.locked()


@pytest.mark.parametrize("situacao", ["created", "dead", "removing"])
async def test_a_helper_that_never_ran_or_is_in_another_non_running_state_is_an_unknown_result(cog, d12, situacao):
    """Um auxiliar CRIADO e nunca iniciado também tem ``Running=False`` e ``ExitCode=0``: só ``exited`` conta como terminado."""
    from empiremuse.updater import docker_flow
    from empiremuse.updater.proxy_client import HelperState

    pendente_docker_em_disco(d12.runtime)
    d12.auxiliar.estado = HelperState(situacao, False, 0, "", "")
    ctx = ctx_prefixo()

    await Owner.update.callback(cog, ctx)

    assert docker_flow.describe_exit(None) in texto_do_primeiro_envio(ctx)
    assert docker_flow.describe_exit(0) not in texto_do_primeiro_envio(ctx)
    d12.enviar_log.assert_not_awaited()
    assert d12.auxiliar.eventos == ["state", "remove"] and not (d12.runtime / "update-pending.json").exists()
    assert d12.cron.eventos == ["plano"] and ctx.send.await_count == 2


# ---------------------------------------------------------------------------- vigia do auxiliar: o registro vive no pool, não no módulo do cog


def outro_cog(cog, *, bot_id: int | None = None) -> Owner:
    """Um segundo ``Owner`` sobre o MESMO pool: o cog recarregado, ou o cog de outro bot do mesmo processo."""
    bot = SimpleNamespace(**vars(cog.bot))
    if bot_id is not None:
        bot.user = SimpleNamespace(id=bot_id)
    novo = Owner.__new__(Owner)
    novo.bot = bot
    novo.log_owner_command = Mock()
    novo.secret_values = Mock(return_value=frozenset())
    return novo


@pytest_asyncio.fixture
async def vigia_viva(cog, d12, monkeypatch):
    """Confirma uma atualização docker cujo ``watch_helper`` NÃO termina: o vigia fica vivo até o fim do teste."""
    solto = asyncio.Event()

    async def vigia_bloqueado(*args, **kwargs):
        await solto.wait()

    monkeypatch.setattr(legacy_cmds, "watch_helper", vigia_bloqueado)
    view = await oferecer(cog, ctx_prefixo())
    await view.confirm.callback(clique())
    for _ in range(5):  # deixa a tarefa do vigia começar
        await asyncio.sleep(0)
    pendente_docker_em_disco(d12.runtime)  # o que o início real teria gravado
    d12.auxiliar.eventos.clear()
    yield
    solto.set()
    await esperar_tarefas()


async def test_two_pools_do_not_share_watchers(cog, d12, vigia_viva):
    """O registro é por pool: um pool sem vigia vivo (outro processo simulado) não é bloqueado pelo vigia de outro."""
    cog_de_outro_pool = outro_cog(cog)
    cog_de_outro_pool.bot.pool = SimpleNamespace(
        update_lock=asyncio.Lock(),
        shutdown=AsyncMock(),
        config={"RUN_RPC_SERVER": False},
        update_ready_checked=False,
        update_watch_task=None,
    )
    d12.auxiliar.parado(66)

    await Owner.update.callback(cog_de_outro_pool, ctx_prefixo())

    assert d12.auxiliar.eventos == ["state", "remove"]  # sem vigia vivo NESTE pool, o comando resolveu o pendente


# ============================================================================ D13: imagem em versão de teste ``X.Y.Z-rc.N`` (A29 g)

RC2 = "2.1.0-rc.2"


@pytest.fixture
def docker_rc(docker, monkeypatch):
    """Modo docker com a imagem na rc.2; o plano devolvido tem a versão atual vinda do ``plan_docker_update`` chamado."""
    from tests.support.versao_rc import atual_rc

    monkeypatch.setenv("EMPIREMUSE_VERSION", RC2)
    rc = atual_rc(RC2)
    plano = SimpleNamespace(current=rc, target=PARA, digest="sha256:" + "ab" * 32, changelog="- novidade da 2.1.0")
    chamadas: list[Any] = []

    async def planejar(*args, **kwargs):
        chamadas.append((args, kwargs))
        return plano

    monkeypatch.setattr(legacy_cmds, "plan_docker_update", planejar)
    return SimpleNamespace(rc=rc, plano=plano, chamadas=chamadas)


async def test_the_docker_mode_plans_from_an_rc_image_version_instead_of_refusing(cog, docker_rc):
    ctx = ctx_prefixo()

    await oferecer(cog, ctx)

    ((args, kwargs),) = docker_rc.chamadas
    assert (args[1] if len(args) > 1 else kwargs["current"]) == docker_rc.rc
    assert str(docker_rc.rc) == RC2


async def test_confirming_from_the_rc_starts_the_helper_for_the_stable_target(cog, docker_rc, monkeypatch):
    iniciados: list[Any] = []

    async def iniciar(client, plan, request, runtime_dir, **kwargs):
        iniciados.append(plan)
        return SimpleNamespace(marca="pendente-rc", channel_id=CANAL, requested_by=DONO)

    monkeypatch.setattr(legacy_cmds, "start_docker_update", iniciar)
    monkeypatch.setattr(legacy_cmds, "watch_helper", AsyncMock())
    monkeypatch.setattr(legacy_cmds, "ProxyClient", lambda caminho: SimpleNamespace(socket=caminho))
    view = await oferecer(cog, ctx_prefixo())
    interacao = clique()

    await view.confirm.callback(interacao)
    await esperar_tarefas()

    (plano,) = iniciados
    assert str(plano.current) == RC2 and str(plano.target) == "2.1.0"
    assert "2.1.0" in interacao.response.edit_message.await_args.kwargs["embed"].description


@pytest.mark.parametrize("valor", ["2.1.0-rc.02", "2.1.0-beta.1", "2.1.0-RC.2", "dev"])
async def test_an_invalid_pre_release_image_version_still_cannot_be_identified(cog, docker, monkeypatch, valor):
    monkeypatch.setenv("EMPIREMUSE_VERSION", valor)

    with pytest.raises(GenericError) as erro:
        await Owner.update.callback(cog, ctx_prefixo())

    assert "Não consegui identificar a versão desta imagem" in erro.value.text
    assert not cog.bot.pool.update_lock.locked()


async def test_the_panel_mode_from_an_rc_offers_the_stable_and_shows_the_rc_as_installed(cog, ambiente, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_VERSION", RC2)
    releases_do_github(monkeypatch, versao=Version(2, 1, 0))

    texto = await Owner.update.callback(cog, ctx_painel())

    assert "Versão disponível: 2.1.0" in texto and "(instalada: 2.1.0-rc.2)" in texto
    assert "novidade da 2.1.0" in texto and "blob/v2.1.0/CHANGELOG.md" in texto
    assert "mais recente" not in texto
