"""Subchaves de assinatura por finalidade (contrato T-456 §6): uma chave derivada para cada serviço de token.

A chave mestra (``EXTERNAL_PANEL_SIGNING_SECRET``) nunca assina direto: cada serviço (Jukebox, reset de rádio, apagar dados)
recebe a subchave da finalidade dele, então um token emitido para uma finalidade não vale em outra mesmo se o escopo for
confundido. Nada aqui usa a chave de serviço nem a ``cookie_secret`` do painel genérico.
"""

from __future__ import annotations

import hashlib
import hmac

PURPOSE_JUKEBOX = "jukebox"
PURPOSE_RADIO_RESET = "radio_reset"
PURPOSE_ERASURE = "erasure"


def derive_key(master: str, purpose: str) -> str:
    """A subchave (64 caracteres hex, acima do piso de 32 do ``SignedTokens``) da finalidade ``purpose``."""
    return hmac.new(
        master.encode("utf-8"), f"empiremuse-panel-api/{purpose}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
