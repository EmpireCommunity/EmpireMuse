"""Aviso pós-boot do update: quando o pool fica saudável, conta no canal de origem se deu certo ou se voltou.

O sucesso da subida é do pool (``on_pool_ready`` marca o pendente como ``ready``), não de um bot específico; o aviso sai pelo
bot que pediu ou, se ele não está mais no pool, pelo primeiro bot pronto. Uma única entrega: o pendente é apagado antes do
envio (quem chegar depois não encontra nada). Sem canal alcançável o aviso vai por DM a quem pediu, pelo mesmo bot; sem os
dois, só o log. Nenhuma menção é permitida e nada do conteúdo vai para o log.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import disnake

from empiremuse import paths
from empiremuse.rules.release import Version, changelog_sections
from empiremuse.updater.docker_flow import (
    UNKNOWN_EXIT,
    WATCH_INTERVAL,
    WATCH_TIMEOUT,
    describe_exit,
    send_helper_log,
)
from empiremuse.updater.git_flow import UpdateError, installed_version
from empiremuse.updater.proxy_client import HelperState, ProxyClient
from empiremuse.updater.state import (
    GAVE_UP_ATTEMPTS,
    MAX_RESTORE_ATTEMPTS,
    PendingUpdate,
    clear_pending,
    read_pending,
    write_pending,
)
from empiremuse.utils import branding
from empiremuse.version import image_version

logger = logging.getLogger(__name__)

CHANGELOG_MAX_CHARS = 3500
COLOR_OK = 0x2ECC71
COLOR_FAIL = 0xE74C3C
COLOR_UNKNOWN = 0xF1C40F
COLOR_INFO = 0x95A5A6

VersionDetector = Callable[[PendingUpdate], Awaitable[Version | None]]


async def detect_running_version(pending: PendingUpdate) -> Version | None:
    """Versão que de fato subiu: a tag do clone (fluxo git)."""
    if pending.flow == "git":
        return await installed_version(paths.PROJECT_DIR)
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")[: 1024 * 1024]
    except OSError, ValueError:
        return ""


def build_notice(pending: PendingUpdate, running: Version | None, changelog: str) -> disnake.Embed:
    """Embed do resultado, decidido pela versão que de fato subiu (``running``), não só pelo estado gravado.

    ``running == from`` → voltou; ``running == to`` com o pendente ainda ``pending`` → sucesso; qualquer outra combinação
    (inclusive ``rolled_back`` cuja reversão não se confirmou) → resultado incerto.
    """
    link = f"{branding.REPO_URL}/blob/v{pending.to_version}/CHANGELOG.md"
    if pending.status == "rolled_back" and pending.restore_attempts >= GAVE_UP_ATTEMPTS:
        # O boot_guard desistiu depois de várias tentativas: o HEAD pode até estar na versão anterior com a árvore mista.
        text = (
            f"⚠️ **A atualização para {pending.to_version} falhou e eu desisti de voltar para a versão "
            f"{pending.from_version} depois de {MAX_RESTORE_ATTEMPTS} tentativas.**\n"
            "Confira o servidor: o código instalado pode estar incompleto."
        )
        return disnake.Embed(description=text, colour=COLOR_FAIL)
    if running == pending.from_version:
        text = (
            f"⚠️ **A atualização para {pending.to_version} falhou e o bot voltou para a versão {pending.from_version}.**"
            f"\nVeja o log do servidor para o motivo."
        )
        return disnake.Embed(description=text, colour=COLOR_FAIL)
    if pending.status in ("pending", "ready") and running == pending.to_version:
        text = f"✅ **Atualizado de {pending.from_version} para {pending.to_version}.**"
        if changelog:
            text += f"\n\n{changelog}"
        return disnake.Embed(description=f"{text}\n\n[Histórico completo]({link})", colour=COLOR_OK)
    if pending.status == "rolled_back":
        text = (
            f"❔ **A atualização para {pending.to_version} falhou e tentei voltar para a versão {pending.from_version}, "
            "mas não consegui confirmar a versão instalada.**\nConfira o servidor."
        )
    else:
        text = (
            f"❔ **A atualização de {pending.from_version} para {pending.to_version} terminou, mas não consegui confirmar a "
            "versão instalada.**\nConfira o servidor."
        )
    return disnake.Embed(description=text, colour=COLOR_UNKNOWN)


async def _send(bot: Any, pending: PendingUpdate, embed: disnake.Embed) -> bool:
    """Canal de origem; se não der, DM a quem pediu. ``True`` se alguma entrega funcionou."""
    allowed = disnake.AllowedMentions.none()
    try:
        channel = bot.get_channel(pending.channel_id) or await bot.fetch_channel(pending.channel_id)
        await channel.send(embed=embed, allowed_mentions=allowed)
        return True
    except disnake.HTTPException, AttributeError:
        logger.info("update: canal %s inalcançável; tentando DM a quem pediu", pending.channel_id)
    try:
        user = bot.get_user(pending.requested_by) or await bot.fetch_user(pending.requested_by)
        await user.send(embed=embed, allowed_mentions=allowed)
        return True
    except disnake.HTTPException, AttributeError:
        logger.warning("update: não consegui entregar o aviso nem por DM")
        return False


def _sender(pool: Any, pending: PendingUpdate) -> Any | None:
    """O bot que pediu, se está pronto (inclusive um bot de guild); senão o primeiro bot pronto de ``pool.bots`` (ou ``None``)."""
    for bot in pool.get_all_bots():
        if bot.bot_ready and getattr(bot.user, "id", None) == pending.bot_id:
            return bot
    ready = [bot for bot in pool.bots if bot.bot_ready]
    return ready[0] if ready else None


async def deliver_update_notice(
    pool: Any,
    *,
    runtime_dir: Path | None = None,
    now: Callable[[], float] = time.time,
    detect_version: VersionDetector = detect_running_version,
) -> None:
    """Entrega o aviso do pendente por um bot pronto do pool (o que pediu, ou outro); sem bot pronto não faz nada."""
    runtime_dir = runtime_dir or paths.get_runtime_dir()
    pending = read_pending(runtime_dir, now())
    if pending is None or pending.status == "applying":  # ``applying`` é do boot_guard: nem avisa nem apaga
        return
    bot = _sender(pool, pending)
    if bot is None:
        return
    clear_pending(runtime_dir)  # uma única entrega, mesmo que o envio falhe
    running = await detect_version(pending)
    changelog = ""
    if running == pending.to_version and pending.status in ("pending", "ready"):
        text = await asyncio.get_running_loop().run_in_executor(None, _read_text, paths.PROJECT_DIR / "CHANGELOG.md")
        changelog = changelog_sections(text, pending.from_version, pending.to_version, CHANGELOG_MAX_CHARS)
    await _send(bot, pending, build_notice(pending, running, changelog))


async def _wait_for_helper(
    client: ProxyClient, sleep: Callable[[float], Awaitable[Any]], timeout: float, interval: float
) -> HelperState | None:
    """Estado final do auxiliar; ``None`` se ele não existe, o proxy não responde ou ele ainda roda no ``timeout``."""
    waited = 0.0
    while True:
        try:
            state = await client.helper_state()
        except UpdateError:
            return None
        if state is None or not state.running:
            return state
        if waited >= timeout:
            return None
        await sleep(interval)
        waited += interval


def build_docker_notice(
    pending: PendingUpdate, state: HelperState | None, running: Version | None, changelog: str
) -> disnake.Embed:
    """Embed do resultado do fluxo Docker, decidido pelo auxiliar (código de saída) E pela versão que de fato subiu.

    Saiu com 0 e a imagem é a nova → sucesso; saiu com ≠ 0 e a imagem é a antiga → falhou/voltou, com o significado do
    código; qualquer outra combinação (auxiliar ausente, ainda rodando, versão ilegível ou inesperada) → desconhecido.
    """
    link = f"{branding.REPO_URL}/blob/v{pending.to_version}/CHANGELOG.md"
    if state is not None and state.exit_code == 0 and running == pending.to_version:
        text = f"✅ **Atualizado de {pending.from_version} para {pending.to_version}.**"
        if changelog:
            text += f"\n\n{changelog}"
        return disnake.Embed(description=f"{text}\n\n[Histórico completo]({link})", colour=COLOR_OK)
    if state is not None and state.exit_code != 0 and running == pending.from_version:
        text = (
            f"⚠️ **A atualização para {pending.to_version} falhou e o bot continua na versão {pending.from_version}.**"
            f"\nMotivo: {describe_exit(state.exit_code)}."
        )
        return disnake.Embed(description=text, colour=COLOR_FAIL)
    text = f"❔ **Atualização de {pending.from_version} para {pending.to_version}: {UNKNOWN_EXIT}.**"
    return disnake.Embed(description=text, colour=COLOR_UNKNOWN)


async def deliver_docker_notice(
    pool: Any,
    client: ProxyClient | None,
    *,
    runtime_dir: Path | None = None,
    now: Callable[[], float] = time.time,
    image_version_fn: Callable[[], Version | None] = image_version,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    timeout: float = WATCH_TIMEOUT,
    interval: float = WATCH_INTERVAL,
) -> None:
    """Aviso pós-boot do fluxo Docker: lê o estado do auxiliar (espera se ainda roda), confere a versão e avisa uma vez.

    Com saída ≠ 0 o log do auxiliar (mascarado) vai por DM a quem pediu ANTES de o auxiliar ser removido: nos casos 70 e
    71 o processo que acompanhava morreu na troca do container, e este aviso é a única chance de o dono ver o detalhe.
    Sem ``client`` (proxy não configurado) o estado do auxiliar é desconhecido. O auxiliar é removido no fim (erro
    ignorado) e o pendente é apagado ANTES do envio. Sem bot pronto não faz nada.
    """
    runtime_dir = runtime_dir or paths.get_runtime_dir()
    pending = read_pending(runtime_dir, now())
    if pending is None or pending.flow != "docker" or pending.status not in ("pending", "ready"):
        return
    bot = _sender(pool, pending)
    if bot is None:
        return
    state = await _wait_for_helper(client, sleep, timeout, interval) if client is not None else None
    if client is not None:
        if state is not None and state.exit_code != 0:
            secrets = pool.secret_values()
            await send_helper_log(client, DiscordReport(bot, pending), pending, secrets)
        try:
            await client.remove_helper()
        except UpdateError:
            logger.warning("update: não consegui remover o auxiliar depois do aviso pós-boot.")
    clear_pending(runtime_dir)  # uma única entrega, mesmo que o envio falhe
    running = image_version_fn()
    changelog = ""
    if state is not None and state.exit_code == 0 and running == pending.to_version:
        text = await asyncio.get_running_loop().run_in_executor(None, _read_text, paths.PROJECT_DIR / "CHANGELOG.md")
        changelog = changelog_sections(text, pending.from_version, pending.to_version, CHANGELOG_MAX_CHARS)
    await _send(bot, pending, build_docker_notice(pending, state, running, changelog))


class DiscordReport:
    """Mensagens do acompanhamento do auxiliar: o canal de origem (com DM de reserva) e a DM de quem pediu."""

    def __init__(self, bot: Any, pending: PendingUpdate) -> None:
        self._bot = bot
        self._pending = pending

    async def channel(self, text: str) -> None:
        await _send(self._bot, self._pending, disnake.Embed(description=text, colour=COLOR_INFO))

    async def dm(self, text: str) -> None:
        try:
            user = self._bot.get_user(self._pending.requested_by) or await self._bot.fetch_user(
                self._pending.requested_by
            )
            await user.send(text, allowed_mentions=disnake.AllowedMentions.none())
        except disnake.HTTPException, AttributeError:
            logger.warning("update: não consegui mandar o log do auxiliar por DM")


async def on_pool_ready(
    pool: Any,
    *,
    runtime_dir: Path | None = None,
    now: Callable[[], float] = time.time,
    detect_version: VersionDetector = detect_running_version,
) -> None:
    """Chamado quando o pool fica saudável: um pendente ``pending`` vira ``ready`` (a subida deu certo) e o aviso sai.

    O ``ready`` é gravado ANTES de qualquer envio e zera a contagem do ``boot_guard``: um token que não loga mais, ou um
    envio que falha, não pode levar o próximo boot a reverter uma atualização boa. Sem pendente ou com o pool doente não
    faz nada; chamado duas vezes, entrega uma vez só (a primeira apaga o pendente).
    """
    if not pool.pool_is_healthy():
        return
    runtime_dir = runtime_dir or paths.get_runtime_dir()
    pending = read_pending(runtime_dir, now())
    if pending is None:
        return
    if pending.status == "pending":
        write_pending(runtime_dir, dataclasses.replace(pending, status="ready", boots=0))
    if pending.flow == "docker":
        socket = pool.config.get("UPDATE_PROXY_SOCKET")
        if not socket:
            logger.warning("update: pendente do fluxo Docker sem UPDATE_PROXY_SOCKET; o aviso sai como desconhecido.")
        client = ProxyClient(socket) if socket else None
        await deliver_docker_notice(pool, client, runtime_dir=runtime_dir, now=now)
        return
    await deliver_update_notice(pool, runtime_dir=runtime_dir, now=now, detect_version=detect_version)
