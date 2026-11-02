"""Token assinado das confirmações da Jukebox (configurar e reiniciar): o ``SignedTokens`` de ``services/tokens.py``."""

from empiremuse.panel.services.tokens import TOKEN_TTL_SECONDS, SignedTokens as JukeboxTokens

__all__ = ("JukeboxTokens", "TOKEN_TTL_SECONDS")
