"""Rotas da conta (B3, contrato T-456 §4 "Conta"): sempre os dados do usuário da sessão; ``user_id`` de corpo ou caminho nunca
é lido. Os serviços e as regras são os do painel genérico; a saída passa só por ``rules/panel_serialize``."""

from __future__ import annotations

from typing import Any

from empiremuse.api.data import DataHandler, form_reply
from empiremuse.api.errors import BOT_UNAVAILABLE, ApiError
from empiremuse.api.idempotency import Reply
from empiremuse.api.writes import Write, parse_account_write, parse_favorite_removal, parse_radio_removal
from empiremuse.panel.services.account import AccountOutcome
from empiremuse.rules.panel_serialize import ACCOUNT, new_rpc_token, serialize_favorites_export, serialize_form

FAVORITES = "favoritos"
INTEGRATIONS = "integracoes"
RADIOS = "radios"


class AccountHandler(DataHandler):
    """``GET /account``: todas as seções da conta, uma por chave de formulário."""

    async def get(self) -> None:
        forms = await self.ctx.account_service.read_all(self.account_context())
        self.write({"forms": {key: serialize_form(form, ACCOUNT) for key, form in forms.items()}})


class AccountSectionHandler(DataHandler):
    """Uma seção da conta: leitura da própria seção e escrita com ``expected_version`` (CAS) e ``Idempotency-Key``."""

    section_key = ""
    actions: tuple[str, ...] = ()  # ações aceitas no corpo (``action``); vazio = a rota só faz uma coisa
    replayable = True

    def account_reply(self, outcome: AccountOutcome) -> Reply:
        return form_reply(outcome.status, outcome.form, ACCOUNT)

    async def read_section(self) -> None:
        service = self.ctx.account_service
        form = await service.read(service.section(self.section_key), self.account_context())
        self.write(serialize_form(form, ACCOUNT))

    async def submit(self) -> None:
        body = self.json_body()
        await self.submit_write(parse_account_write(self.section_key, body, actions=self.actions), body)

    async def submit_write(self, write: Write, request_input: Any) -> None:
        """``request_input`` (o corpo ou a query) é o que a ``Idempotency-Key`` compara."""
        ctx = self.account_context()
        service = self.ctx.account_service
        section = service.section(self.section_key)

        async def work() -> Reply:
            return self.account_reply(await service.submit(section, ctx, write.action, write.service_args()))

        await self.run_write(request_input, work, replayable=self.replayable)


class PrefixHandler(AccountSectionHandler):
    section_key = "prefixo"

    async def patch(self) -> None:
        await self.submit()


class LastFmHandler(AccountSectionHandler):
    """Só liga e desliga o scrobble; o vínculo com o Last.fm fica fora da v1 (contrato m6)."""

    section_key = "lastfm"

    async def patch(self) -> None:
        await self.submit()


class RpcTokenHandler(AccountSectionHandler):
    """Gera o token do app de RPC. O token novo sai **uma só vez**, nesta resposta (``no-store``); por isso a rota não
    guarda resposta para repetir (``Idempotency-Key`` é ignorada aqui) e o bot nunca o guarda nem o registra."""

    section_key = "rpc"
    replayable = False

    def account_reply(self, outcome: AccountOutcome) -> Reply:
        reply = super().account_reply(outcome)
        issued = new_rpc_token(outcome.form)
        if issued is None or reply.body is None:
            return reply
        body, headers = issued
        for name, value in headers.items():
            self.set_header(name, value)
        return Reply(reply.status, {**reply.body, **body})

    async def patch(self) -> None:
        await self.submit()


class FavoritesHandler(AccountSectionHandler):
    section_key = FAVORITES
    actions = ("add", "edit", "import")

    async def get(self) -> None:
        await self.read_section()

    async def put(self) -> None:
        await self.submit()

    async def delete(self) -> None:
        query = self.query_arguments("name", "expected_version")
        await self.submit_write(parse_favorite_removal(query), query)


class FavoritesExportHandler(DataHandler):
    """``GET /account/favorites/export``: o JSON no formato de importação, só do usuário da sessão."""

    async def get(self) -> None:
        doc = await self.ctx.account_service.load(self.account_context())
        self.write(serialize_favorites_export(doc.get("fav_links") or {}))


class IntegrationsHandler(AccountSectionHandler):
    section_key = INTEGRATIONS
    actions = ("remove", "import")

    async def patch(self) -> None:
        await self.submit()


class RadioFavoritesHandler(AccountSectionHandler):
    """Estações e locais de rádio favoritos do usuário (J3): listar e remover; remover é idempotente e não leva versão."""

    section_key = RADIOS

    async def get(self) -> None:
        await self.read_section()

    async def delete(self) -> None:
        query = self.query_arguments("kind", "id")
        removal = parse_radio_removal(query)
        ctx = self.account_context()
        if getattr(getattr(ctx.pool, "radio", None), "user_favorites", None) is None:
            raise ApiError(503, BOT_UNAVAILABLE)  # a rádio não está ligada neste bot
        service = self.ctx.account_service
        section = service.section(RADIOS)

        async def work() -> Reply:
            outcome = await service.submit(section, ctx, removal.action, {removal.field: [removal.target]})
            return self.account_reply(outcome)

        await self.run_write(query, work)


def account_routes() -> list[tuple[str, Any, str]]:
    """(caminho sob o prefixo, handler, caminho do OpenAPI)."""
    return [
        ("/account", AccountHandler, "/account"),
        ("/account/prefix", PrefixHandler, "/account/prefix"),
        ("/account/rpc", RpcTokenHandler, "/account/rpc"),
        ("/account/integrations", IntegrationsHandler, "/account/integrations"),
        ("/account/lastfm", LastFmHandler, "/account/lastfm"),
        ("/account/favorites", FavoritesHandler, "/account/favorites"),
        ("/account/favorites/export", FavoritesExportHandler, "/account/favorites/export"),
        ("/account/radio-favorites", RadioFavoritesHandler, "/account/radio-favorites"),
    ]
