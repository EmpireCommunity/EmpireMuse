"""Nome do bot no Discord para as páginas públicas (painel e status), com "Music Bot" de reserva (só biblioteca padrão)."""

from __future__ import annotations

from typing import Any, Optional

FALLBACK_BOT_NAME = "Music Bot"


def _bot_name(bot: Any) -> Optional[str]:
    name = getattr(getattr(bot, "user", None), "name", None)
    return name.strip() if isinstance(name, str) and name.strip() else None


def bot_name_of(bot: Any) -> str:
    """Nome do bot no Discord, sem espaços nas pontas; ``FALLBACK_BOT_NAME`` se ele ainda não logou ou o nome é vazio."""
    return _bot_name(bot) or FALLBACK_BOT_NAME


def bot_display_name(pool: Any) -> str:
    """Nome do bot no Discord para o título e o cabeçalho do painel: o do principal, senão o do primeiro bot logado.

    Sem pool, sem bot logado ou com nome vazio, devolve ``FALLBACK_BOT_NAME``.
    """
    names = (_bot_name(bot) for bot in [getattr(pool, "main_bot", None), *getattr(pool, "bots", ())])
    return next((name for name in names if name), FALLBACK_BOT_NAME)
