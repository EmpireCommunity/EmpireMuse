"""Jukebox estrita (item 8, J1): a chave ``settings.request_channel_strict`` do documento único, na página do canal.

Formulário de servidor como os da categoria Geral (versão otimista do próprio campo e gravação por
``pool.servers.patch``), só que exibido na página do canal de pedidos e endereçado por ``acao=estrito``. Documento antigo
não tem a chave: ``rules.servers.request_channel_strict`` lê ausente como desligado. O bot lê a chave a cada mensagem
no canal (cache do store); o painel não aplica nenhum efeito em memória.
"""

from __future__ import annotations

from typing import Any

from empiremuse.panel.services.base import Args, FormContext, ParseResult, SettingsForm, flag
from empiremuse.rules.servers import request_channel_strict

STRICT_ACTION = "estrito"  # valor de ``acao`` que endereça este formulário no POST da página do canal


class StrictRequestsForm(SettingsForm):
    key = STRICT_ACTION
    category = "canal"
    scope_kind = "servidor"
    fields = ("request_channel_strict",)
    markers = fields

    def extract(self, doc: dict) -> dict[str, Any]:
        return {"request_channel_strict": request_channel_strict(doc)}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["request_channel_strict"] = new["request_channel_strict"]

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        return ParseResult({"request_channel_strict": flag(args, "request_channel_strict")}, {}, {})
