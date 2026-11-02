"""Respostas efêmeras ("Só você pode ver isto") que não se acumulam: cada tipo some sozinho depois de um prazo (R8, decisão do dono).

- **Confirmação** (feito, cancelado): some em ``CONFIRMATION_TTL_S``;
- **Erro**: some em ``ERROR_TTL_S`` (tempo de ler e agir);
- **Menu aberto** (seletor de estação, confirmação pendente): some quando termina ou depois de ``MENU_IDLE_S`` sem uso.

A limpeza é por interação (``delete_original_response``): nunca apaga mensagem pública, e falha de apagar (já sumiu, token vencido) é silêncio de propósito.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import disnake

logger = logging.getLogger(__name__)

CONFIRMATION_TTL_S = 10.0
ERROR_TTL_S = 30.0
MENU_IDLE_S = 60.0

_pending: set[asyncio.Task] = set()  # referência forte: o laço só guarda referência fraca das tarefas


async def _delete_later(interaction: Any, seconds: float) -> None:
    await asyncio.sleep(seconds)
    try:
        await interaction.delete_original_response()
    except disnake.HTTPException, AttributeError:
        logger.debug("Resposta efêmera já não existe", exc_info=True)


def cleanup_later(interaction: Any, seconds: float) -> asyncio.Task | None:
    """Agenda o apagamento da resposta original de ``interaction``. Quem chama só passa interação cuja resposta é efêmera (``defer(ephemeral=True)``):
    uma resposta pública nunca é agendada por aqui. Sem laço de eventos, não faz nada."""
    try:
        task = asyncio.get_running_loop().create_task(_delete_later(interaction, seconds))
    except RuntimeError:
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
