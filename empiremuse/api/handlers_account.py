"""Rotas da conta (B3, contrato T-456 §4 "Conta"): sempre os dados do usuário da sessão; ``user_id`` de corpo ou caminho nunca
é lido. Os serviços e as regras são os do painel genérico; a saída passa só por ``rules/panel_serialize``."""

from __future__ import annotations

from typing import Any, Callable, Optional

from empiremuse.api.data import STATUS_SESSION_GONE, DataHandler, form_reply
from empiremuse.api.errors import BOT_UNAVAILABLE, ApiError, not_found, validation_failed
from empiremuse.api.idempotency import Reply
from empiremuse.api.writes import (
    ACCOUNT_KINDS,
    Write,
    parse_account_write,
    parse_favorite_removal,
    parse_radio_favorite,
    parse_radio_removal,
)
from empiremuse.panel.services.account import AccountOutcome
from empiremuse.panel.services.account_base import Refusal
from empiremuse.rules.panel_serialize import ACCOUNT, new_rpc_token, serialize_favorites_export, serialize_form
from empiremuse.utils.radio.favorites import AddResult, FavoriteNotFound
from empiremuse.utils.radio.premium import RadioUnavailable

FAVORITES = "favoritos"
INTEGRATIONS = "integracoes"
RADIOS = "radios"


class AccountHandler(DataHandler):
    """``GET /account``: todas as seções da conta, uma por chave de formulário."""

    async def get(self) -> None:
        forms = await self.ctx.account_service.read_all(self.account_context())
        self.write({"forms": {key: serialize_form(form, ACCOUNT) for key, form in forms.items()}})


class SessionGuardedSection:
    """Uma seção da conta que, no ``guard`` (o serviço o roda **sob o lock do usuário**, o mesmo do apagamento de dados),
    recusa a escrita se a sessão já acabou. Uma escrita que esperava o lock enquanto o apagamento rodava não grava de novo."""

    def __init__(self, section: Any, alive: Callable[[], bool]) -> None:
        self._section = section
        self._alive = alive

    def __getattr__(self, name: str) -> Any:
        return getattr(self._section, name)

    async def guard(self, ctx: Any, doc: dict, action: str) -> Optional[Refusal]:
        if not self._alive():
            return Refusal(STATUS_SESSION_GONE, "")
        return await self._section.guard(ctx, doc, action)


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
        section = SessionGuardedSection(service.section(self.section_key), self.session_alive)

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
    """Estações e locais de rádio favoritos do usuário (J3): listar, ADICIONAR (B9) e remover; os dois últimos são por chave, idempotentes e sem versão.
    Só ``station`` (uuid do diretório) e ``filter`` (nó de local): ``custom``/``genre`` dependem de servidor e ficam em ``/guilds/{g}/radio-favorites/mine``."""

    section_key = RADIOS

    async def get(self) -> None:
        await self.read_section()

    async def post(self) -> None:
        """``POST {kind: station|filter, id}``: o usuário é o da SESSÃO (``user_id`` de corpo é ignorado). Ordem: formato, leitura, já está (``200``), cheia
        (``400`` ``limit``), diretório ou árvore de locais (``404`` se não conhece, ``503`` se fora), CAS. Estação ao vivo não exige premium. O nome vem do diretório."""
        body = self.json_body()
        favorite = parse_radio_favorite(body, ACCOUNT_KINDS)
        ctx = self.account_context()
        service = self.ctx.account_service
        section = service.section(RADIOS)
        favorites = getattr(getattr(ctx.pool, "radio", None), "user_favorites", None)
        if favorites is None or not section.available(ctx):
            raise ApiError(503, BOT_UNAVAILABLE)  # a rádio não está ligada neste bot (o mesmo sinal da seção)

        async def work() -> Reply:
            try:
                if favorite.kind == "station":
                    result = await favorites.add_station(ctx.user_id, favorite.id)
                else:
                    result = await favorites.add_filter(ctx.user_id, favorite.id)
            except FavoriteNotFound:
                raise not_found() from None
            except RadioUnavailable:
                raise ApiError(503, BOT_UNAVAILABLE) from None
            if result is AddResult.FULL:
                raise validation_failed("limit")
            updated = await service.read(section, ctx)
            if updated.extras.get("indisponivel") or not updated.extras.get("disponivel"):
                raise ApiError(
                    503, BOT_UNAVAILABLE
                )  # a escrita pode ter ocorrido; nunca confirma uma lista vazia inventada
            return Reply(200, serialize_form(updated, ACCOUNT))

        await self.run_write(body, work)

    async def delete(self) -> None:
        query = self.query_arguments("kind", "id")
        removal = parse_radio_removal(query)
        ctx = self.account_context()
        service = self.ctx.account_service
        section = service.section(RADIOS)
        if not section.available(ctx):
            raise ApiError(503, BOT_UNAVAILABLE)  # a rádio não está ligada neste bot (o mesmo sinal da seção)

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
