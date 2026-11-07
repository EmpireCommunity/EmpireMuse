"""Guarda de boot do fluxo git: se o código novo não chega a ficar pronto em poucas subidas, volta ao commit anterior.

Roda em ``__main__.main()`` ANTES de importar o ``BotPool`` (um erro de import no código novo derrubaria o bot antes de
qualquer outra proteção). Só stdlib no import. Só age quando existe um pendente do fluxo git: cada subida soma um ``boots``;
quando o pool fica saudável o pendente vira ``ready`` (e o aviso pós-boot o apaga), então só quem nunca fica pronto chega ao limite. Um
pendente ``applying`` (o processo morreu ou a reversão falhou no meio do apply) é revertido já no primeiro boot, sem contar.
O fluxo Docker não é tocado (quem desfaz é o auxiliar, com ``--wait``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys
from pathlib import Path

from empiremuse.updater.git_flow import EXIT_RESTART, Runner, UpdateError, restore_checkout, run_exec, safe_failure
from empiremuse.updater.state import (
    GAVE_UP_ATTEMPTS,
    MAX_RESTORE_ATTEMPTS,
    PendingUpdate,
    read_pending,
    write_pending,
)

logger = logging.getLogger(__name__)

MAX_BOOTS_WITHOUT_READY = 2


def run(
    runtime_dir: Path, repo_dir: Path, now: float, *, python: str = sys.executable, runner: Runner = run_exec
) -> None:
    """Conta a subida e, no terceiro boot sem o bot ficar pronto, reverte o código e sai com ``EXIT_RESTART`` (75).

    ``rolled_back`` só é gravado DEPOIS de um restore bem-sucedido. Restore falho deixa o pendente como está (com o
    contador ``restore_attempts`` já incrementado, gravado antes do restore para contar também um restore que derruba o
    processo) e sai com 75: o próximo boot tenta de novo, até ``MAX_RESTORE_ATTEMPTS``; depois disso desiste e grava
    ``rolled_back`` com ``restore_attempts = GAVE_UP_ATTEMPTS`` (o aviso pós-boot diz que a reversão falhou). Sem como
    gravar o contador (disco cheio ou só leitura) NÃO restaura nem sai com 75: sem o teto seria um laço; a subida segue.
    """
    pending = read_pending(runtime_dir, now)
    if pending is None or pending.flow != "git" or pending.status not in ("applying", "pending"):
        return
    # ``applying``: o processo morreu (ou a reversão falhou) no meio do apply, com a árvore possivelmente meio aplicada.
    # Reverte já, sem contar subidas: não há o que esperar de um código que nem terminou de ser instalado.
    boots = pending.boots + 1 if pending.status == "pending" else 0
    if pending.status == "pending" and boots <= MAX_BOOTS_WITHOUT_READY:
        _save(runtime_dir, dataclasses.replace(pending, boots=boots))
        return
    pending = dataclasses.replace(pending, boots=boots)
    if pending.restore_attempts >= MAX_RESTORE_ATTEMPTS:
        logger.error(
            "A reversão automática falhou depois de %d tentativas; desisti, confira o servidor: o clone pode estar "
            "meio restaurado.",
            MAX_RESTORE_ATTEMPTS,
        )
        if not _save(
            runtime_dir, dataclasses.replace(pending, status="rolled_back", restore_attempts=GAVE_UP_ATTEMPTS)
        ):
            return  # sem como gravar a desistência, todo boot releria o teto e sairia com 75: um laço; a subida segue
        raise SystemExit(EXIT_RESTART)
    attempt = dataclasses.replace(pending, restore_attempts=pending.restore_attempts + 1)
    if not _save(runtime_dir, attempt):  # antes do restore: um restore que derruba o processo também conta
        logger.error("O bot sobe como está, sem reverter a atualização: confira o servidor.")
        return
    if pending.status == "applying":
        logger.error("A atualização anterior não terminou de ser aplicada; voltando ao commit anterior.")
    else:
        logger.error(
            "O código novo não ficou pronto em %d subidas; voltando ao commit anterior.", MAX_BOOTS_WITHOUT_READY
        )
    try:
        if pending.prev_sha is None or pending.target_commit is None:
            raise UpdateError("sem commit anterior")
        asyncio.run(
            restore_checkout(Path(repo_dir), pending.prev_sha, pending.target_commit, python=python, runner=runner)
        )
    except Exception as failure:
        # O pendente fica como está (applying/pending): o próximo boot tenta de novo, sem carregar um rolled_back falso.
        logger.error(
            "A reversão automática para o commit anterior falhou (%s); confira o servidor.", safe_failure(failure)
        )
        raise SystemExit(EXIT_RESTART) from None
    _save(runtime_dir, dataclasses.replace(attempt, status="rolled_back"))
    raise SystemExit(EXIT_RESTART)


def _save(runtime_dir: Path, pending: PendingUpdate) -> bool:
    """Grava o pendente. ``False`` (com um erro no log) se o disco falhar: o boot_guard nunca derruba a subida por isso."""
    try:
        write_pending(runtime_dir, pending)
    except OSError:
        logger.error(
            "Reversão não executada: não consegui gravar o estado da atualização (%s); confira o servidor.",
            pending.status,
        )
        return False
    return True
