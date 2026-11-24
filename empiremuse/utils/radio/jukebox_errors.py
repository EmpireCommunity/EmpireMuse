"""A recusa do controlador da Jukebox e o formato da resposta, num módulo folha: o controlador e o mixin da estação os usam sem import tardio."""

from __future__ import annotations

from typing import Any

Reply = tuple[str, dict[str, Any]]  # a chave do texto e os campos dela: quem responde traduz


class Refused(Exception):
    """Recusa com a chave de texto e os campos dela; a superfície traduz e responde em mensagem efêmera."""

    def __init__(self, key: str, **fields: Any) -> None:
        super().__init__(key)
        self.key, self.fields = key, fields
