"""Fluxo Docker do ``/update``: planejar pelas releases, iniciar o auxiliar e acompanhar o resultado.

O bot não altera nada do host: quem troca a imagem do compose é o auxiliar (criado pelo proxy), com backup e rollback.
Aqui o bot só decide a versão alvo, grava o pendente ANTES de criar o auxiliar (assim uma falha no meio nunca deixa um
auxiliar sem dono) e traduz o código de saída dele em texto seguro para o canal. Nada de caminho, uid, modo ou nome de
arquivo do host sai pelo canal; a etapa exata do auxiliar só aparece no log mascarado, por DM, a quem pediu.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

import aiohttp

from empiremuse.rules.owner_shell import mask_shell_output
from empiremuse.rules.release import Version, changelog_sections, pick_target
from empiremuse.updater.git_flow import UpdateError
from empiremuse.updater.proxy_client import ProxyClient
from empiremuse.updater.releases import fetch_changelog, fetch_image_digest, list_releases
from empiremuse.updater.state import PendingUpdate, UpdateRequest, clear_pending, write_pending

logger = logging.getLogger(__name__)

CHANGELOG_MAX_CHARS = 3500
WATCH_TIMEOUT = 900  # segundos esperando o auxiliar terminar
WATCH_INTERVAL = 5
DM_LOG_LINES = 20
DM_MAX_CHARS = 1900  # o limite do Discord é 2000; a folga cobre o que o envio acrescentar
MSG_IN_PROGRESS = "Já há uma atualização em andamento."

# Significado de cada código de saída do auxiliar (constante única: o acompanhamento e o aviso pós-boot usam esta tabela).
HELPER_EXIT_MEANINGS: Mapping[int, str] = {
    0: "atualização concluída",
    64: "versão alvo inválida; nada mudou",
    65: (
        "o compose do projeto não é o esperado (formato da linha `image:` sem suporte) ou as permissões do projeto não são "
        "seguras (dono, modo ou link simbólico); nada mudou. Veja o guia de auto-update"
    ),
    66: "não consegui baixar a imagem; nada mudou",
    67: "a versão alvo não é maior que a instalada; nada mudou",
    68: "a imagem baixada não corresponde à versão anunciada; nada mudou",
    70: "a atualização falhou e o bot voltou para a versão anterior",
    71: "a atualização falhou e não consegui voltar; confira o servidor",
}
UNKNOWN_EXIT = "resultado desconhecido; confira o servidor"


def describe_exit(code: int | None) -> str:
    """Texto seguro do código de saída do auxiliar; qualquer outro código (ou nenhum) é ``resultado desconhecido``."""
    if code is None:
        return UNKNOWN_EXIT
    return HELPER_EXIT_MEANINGS.get(code, UNKNOWN_EXIT)


@dataclasses.dataclass(frozen=True)
class DockerPlan:
    current: Version
    target: Version
    digest: str
    changelog: str


class Report(Protocol):
    """Para onde vão as mensagens do acompanhamento: o canal de origem e a DM de quem pediu."""

    async def channel(self, text: str) -> None: ...

    async def dm(self, text: str) -> None: ...


async def plan_docker_update(session: aiohttp.ClientSession, current: Version) -> DockerPlan | None:
    """Maior release elegível acima de ``current`` com o digest e o changelog dela; ``None`` se não há alvo."""
    available = {release.version: release for release in await list_releases(session)}
    target = pick_target(current, available)
    if target is None:
        return None
    release = available[target]
    digest = await fetch_image_digest(session, release)
    try:
        raw = await fetch_changelog(session, release.tag)
    except UpdateError:
        raw = ""  # sem o changelog o plano segue: a confirmação só não mostra "o que mudou"
    return DockerPlan(current, target, digest, changelog_sections(raw, current, target, CHANGELOG_MAX_CHARS))


async def start_docker_update(
    client: ProxyClient,
    plan: DockerPlan,
    request: UpdateRequest,
    runtime_dir: Path,
    *,
    now: Callable[[], float] = time.time,
) -> PendingUpdate:
    """Cria e inicia o auxiliar para ``plan``. O pendente é gravado ANTES do create (escrita falha = nada é criado).

    Falha ou cancelamento em create/start: remove o auxiliar sob ``asyncio.shield`` e só com a remoção confirmada apaga o
    pendente; se a remoção falhar o pendente fica (o aviso pós-boot resolve pelo estado do auxiliar). A exceção original
    sempre sobe.
    """
    state = await client.helper_state()
    if state is not None:
        if state.running:
            raise UpdateError(MSG_IN_PROGRESS)
        await client.remove_helper()
    pending = PendingUpdate(
        flow="docker",
        from_version=plan.current,
        to_version=plan.target,
        bot_id=request.bot_id,
        channel_id=request.channel_id,
        requested_by=request.requested_by,
        created_at=now(),
        digest=plan.digest,
        boots=0,
        status="pending",
    )
    write_pending(runtime_dir, pending)
    try:
        await client.create_helper(f"{plan.target}@{plan.digest}")
        await client.start_helper()
    except Exception, asyncio.CancelledError:
        await _remove_and_clear(client, runtime_dir)
        raise
    return pending


async def _remove_and_clear(client: ProxyClient, runtime_dir: Path) -> None:
    """Remove o auxiliar sob ``asyncio.shield``; só com a remoção confirmada apaga o pendente."""
    try:
        await asyncio.shield(client.remove_helper())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("update: não consegui remover o auxiliar; o pendente fica para o aviso pós-boot.")
        return
    clear_pending(runtime_dir)


async def _finish(client: ProxyClient, runtime_dir: Path) -> None:
    """Fim do acompanhamento: remove o auxiliar (erro só registrado) e apaga o pendente."""
    try:
        await client.remove_helper()
    except UpdateError:
        logger.warning("update: não consegui remover o auxiliar terminado.")
    clear_pending(runtime_dir)


async def watch_helper(
    client: ProxyClient,
    pending: PendingUpdate,
    runtime_dir: Path,
    report: Report,
    *,
    timeout: float = WATCH_TIMEOUT,
    interval: float = WATCH_INTERVAL,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    secrets: frozenset[str] = frozenset(),
) -> int | None:
    """Acompanha o auxiliar enquanto o bot viver. Devolve o código de saída, ou ``None`` se não houve resultado.

    Sucesso (0): canal com o significado, auxiliar removido e pendente apagado. Falha (≠ 0): o canal recebe SÓ o significado
    do código; quem pediu recebe por DM até 20 linhas do log, mascaradas. Auxiliar ausente: resultado desconhecido e
    pendente apagado. Ainda rodando no ``timeout``, ou erro do proxy: avisa e mantém tudo (o aviso pós-boot resolve).
    """
    waited = 0.0
    while True:
        try:
            state = await client.helper_state()
        except UpdateError as error:
            await report.channel(str(error))
            return None
        if state is None:
            await report.channel(describe_exit(None))
            clear_pending(runtime_dir)
            return None
        if not state.running:
            break
        if waited >= timeout:
            await report.channel(describe_exit(None))
            return None
        await sleep(interval)
        waited += interval

    code = state.exit_code
    await report.channel(describe_exit(code))
    if code != 0:
        await send_helper_log(client, report, pending, secrets)
    await _finish(client, runtime_dir)
    return code


async def send_helper_log(client: ProxyClient, report: Report, pending: PendingUpdate, secrets: frozenset[str]) -> None:
    """DM a quem pediu com as últimas linhas do log do auxiliar, já mascaradas (a etapa exata só aparece aqui)."""
    try:
        lines = await client.helper_logs(DM_LOG_LINES)
    except UpdateError:
        return
    # Mascara ANTES de cortar: um segredo na fronteira do corte não pode escapar do mascaramento pela metade.
    body = mask_shell_output("\n".join(lines[:DM_LOG_LINES]), secrets)  # o proxy já devolve só as últimas DM_LOG_LINES
    header = f"Log do auxiliar da atualização para {pending.to_version}:\n"
    room = DM_MAX_CHARS - len(header)
    if len(body) > room:
        body = "…" + body[-(room - 1) :]  # o fim do log é a parte mais útil
    if body.strip():
        await report.dm(f"{header}{body}")
