"""TRACE TEMPORÁRIO (T-C-BUILD): uma linha INFO por decisão de skin (criação, retomada e cada mudança no ``invoke_np``). Sai antes do PR.

Só ids de servidor, nomes de skin e a origem da escolha: nada de faixa, canal, nome de membro ou texto de usuário.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any

logger = logging.getLogger(__name__)

_LAST: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def trace(where: str, **fields: Any) -> None:
    logger.info("skin-trace %s %s", where, " ".join(f"{name}={value!r}" for name, value in fields.items()))


def trace_render(player: Any, where: str, skin_object: Any) -> None:
    """No ``invoke_np``: só quando algo mudou desde a última linha deste player (sem repetir a cada 15 s)."""
    state = (
        bool(getattr(player, "static", False)),
        getattr(player, "skin", None),
        getattr(player, "skin_static", None),
        getattr(skin_object, "name", None),
        type(skin_object).__name__,
    )
    try:
        if _LAST.get(player) == state:
            return
        _LAST[player] = state
    except TypeError:
        pass
    trace(
        where,
        guild=getattr(player, "guild_id", None),
        static=state[0],
        skin=state[1],
        skin_static=state[2],
        rendered_by=f"{state[3]}:{state[4]}",
        v2=bool(getattr(skin_object, "v2", False)),
    )
