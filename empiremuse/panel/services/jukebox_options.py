"""Opções da Jukebox (spec final do dono): as chaves ``settings.jukebox_auto_delete`` (A) e
``settings.jukebox_text_requests`` (B) do documento único, na página da Jukebox.

Formulário de servidor como os da categoria Geral (versão otimista dos próprios campos e gravação por
``pool.servers.patch``), só que exibido na página da Jukebox e endereçado por ``acao=opcoes``. Documento antigo
não tem as chaves: ``rules.servers`` lê A ausente como desligada e B ausente como ligada (o comportamento de hoje).
O bot lê as chaves a cada mensagem no canal (cache do store); o painel não aplica nenhum efeito em memória.
"""

from __future__ import annotations

from typing import Any

from empiremuse.panel.services.base import Args, FormContext, ParseResult, SettingsForm, flag
from empiremuse.rules.servers import jukebox_auto_delete, jukebox_text_requests

OPTIONS_ACTION = "opcoes"  # valor de ``acao`` que endereça este formulário no POST da página da Jukebox
AUTO_DELETE = "jukebox_auto_delete"
TEXT_REQUESTS = "jukebox_text_requests"


class JukeboxOptionsForm(SettingsForm):
    key = OPTIONS_ACTION
    category = "jukebox"
    scope_kind = "servidor"
    fields = (AUTO_DELETE, TEXT_REQUESTS)
    markers = fields

    def extract(self, doc: dict) -> dict[str, Any]:
        return {AUTO_DELETE: jukebox_auto_delete(doc), TEXT_REQUESTS: jukebox_text_requests(doc)}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc[AUTO_DELETE] = new[AUTO_DELETE]
        doc[TEXT_REQUESTS] = new[TEXT_REQUESTS]

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        return ParseResult({AUTO_DELETE: flag(args, AUTO_DELETE), TEXT_REQUESTS: flag(args, TEXT_REQUESTS)}, {}, {})
