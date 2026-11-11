"""Rotas das rádios (B4, contrato T-456 §4 "Rádios"; ameaças T-436 e T-458 m4).

Leitura: ``GET /guilds/{g}/radios`` e ``…/{r}``; busca de estações e cascata de local; logo (só aninhado, depois de
servidor, rádio e sessão). Escrita (toda com ``expected_version`` = ``rev`` e CAS na store, ``fresh=True`` e
``Idempotency-Key``): configurar, regras do chat, estação principal, reserva. Criar é naturalmente idempotente (uma rádio
por bot e por servidor). Redefinir apaga **sem** recusar versão (T-422b/R1) e por isso pede um token de confirmação preso a
usuário, servidor, rádio e finalidade, de 60 s e uso único.

O ``{r}`` e a estação são sempre conferidos contra o ``{g}`` (``radio_base.py``). Os serviços e as regras são os do painel
genérico (``panel/services``); a saída passa só por ``radio_data.py``.
"""

from __future__ import annotations

from typing import Any

from empiremuse.api.errors import FORBIDDEN, ApiError, not_found, validation_failed
from empiremuse.api.idempotency import Reply
from empiremuse.api.radio_base import (
    GUILD_ID,
    RADIO_ID,
    RadioContext,
    RadioRoute,
    radio_endpoint,
    raise_for_status,
)
from empiremuse.api.radio_data import (
    local_entry,
    paging_entry,
    radio_entry,
    setup_options_entry,
    station_view_entry,
)
from empiremuse.api.writes_radio import (
    CHAT_SPEC,
    RESERVE_SPEC,
    SETUP_SPEC,
    STATION_SPEC,
    parse_create,
    parse_reset_token,
    parse_versioned,
)
from empiremuse.panel.services.radio import belongs_to
from empiremuse.panel.services.radio_live import SEARCH_RAW_LIMIT
from empiremuse.panel.services.radio_local import parse_page, resolve_node, tree_of
from empiremuse.panel.services.radio_logo import lookup_logo
from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import KIND_CUSTOM, KIND_LIVE, MAX_PER_GUILD
from empiremuse.rules.radio_stations import check_search_term
from empiremuse.rules.panel_serialize import error_fields
from empiremuse.utils.usage import record_panel_action

CATEGORY = "radios"
STATUS_CREATED, STATUS_NO_CONTENT = 201, 204
CONFIRM_TTL_SECONDS = 60


class RadiosHandler(RadioRoute):
    """``GET /guilds/{g}/radios`` lista; ``POST`` cria uma rádio (tipo, canal e bot reserva)."""

    @radio_endpoint
    async def get(self, guild_id: str) -> None:
        ctx, premium = await self.guild_context(guild_id, write=False)
        radios = await self.hub(ctx).store.list_for_guild(ctx.guild_id)
        page = self.ctx.radio_page
        entries = [
            await radio_entry(ctx, page, radio, premium=premium, stations=premium)
            for radio in sorted(radios, key=lambda item: int(item.bot_id))
            if belongs_to(radio, ctx.guild_id)  # defesa em profundidade: a store já filtra pelo servidor
        ]
        body: dict[str, Any] = {"premium": premium, "max_radios": MAX_PER_GUILD, "radios": entries}
        if premium:  # sem premium nada vai ao diretório nem se oferece criar
            body["setup"] = setup_options_entry(await self.ctx.radio_setup_service.options(ctx))
        self.write(body)

    @radio_endpoint
    async def post(self, guild_id: str) -> None:
        ctx, premium = await self.guild_context(guild_id, write=True)
        if not premium:
            raise ApiError(403, FORBIDDEN)
        body = self.json_body()
        write = parse_create(body, SETUP_SPEC)

        async def work() -> Reply:
            outcome = await self.ctx.radio_setup_service.configure(ctx, None, write.service_args(), "", self.tr)
            record_panel_action(ctx, f"{CATEGORY}/nova", outcome.status)
            if outcome.form is None:
                raise not_found()
            raise_for_status(outcome.status, error_fields(outcome.form))
            entry = await radio_entry(ctx, self.ctx.radio_page, outcome.radio, premium=True, stations=True)
            return Reply(STATUS_CREATED, entry)

        await self.run_write(body, work)


class RadioHandler(RadioRoute):
    """``GET /guilds/{g}/radios/{r}``; ``PUT`` configura tipo, canal e bot reserva (CAS em ``rev``)."""

    @radio_endpoint
    async def get(self, guild_id: str, radio_id: str) -> None:
        found = await self.radio_context(guild_id, radio_id, write=False)
        self.write(
            await radio_entry(
                found.ctx, self.ctx.radio_page, found.radio, premium=found.premium, stations=found.premium
            )
        )

    @radio_endpoint
    async def put(self, guild_id: str, radio_id: str) -> None:
        found = await self.radio_context(guild_id, radio_id, write=True)
        if not found.premium:
            raise ApiError(403, FORBIDDEN)
        body = self.json_body()
        write = parse_versioned(body, SETUP_SPEC)
        ctx, radio = found.ctx, found.radio

        async def work() -> Reply:
            bot = self.ctx.radio_page.bot_of(ctx, radio)
            current = (int(radio.bot_id), str(bot.user.name) if bot is not None else str(radio.bot_id))
            outcome = await self.ctx.radio_setup_service.configure(
                ctx, radio, write.service_args(), "", self.tr, current_bot=current
            )
            record_panel_action(ctx, f"{CATEGORY}/config", outcome.status)
            if outcome.form is None:
                raise not_found()
            await self.fail_unless_ok(outcome.status, ctx, radio, error_fields(outcome.form))
            return Reply(200, await radio_entry(ctx, self.ctx.radio_page, outcome.radio, premium=True, stations=True))

        await self.run_write(body, work)


class RadioWriteHandler(RadioRoute):
    """Base das escritas que exigem premium e ``manage_messages`` no canal: ``PUT`` com ``expected_version``."""

    spec: Any = None
    label = ""

    async def decide(self, found: RadioContext, args: dict[str, list[str]]) -> tuple[int, Any, list[str]]:
        """``(status, rádio depois, campos com erro)``."""
        raise NotImplementedError

    @radio_endpoint
    async def put(self, guild_id: str, radio_id: str) -> None:
        found = await self.editable_radio(guild_id, radio_id, write=True)
        body = self.json_body()
        write = parse_versioned(body, self.spec)

        async def work() -> Reply:
            status, saved, fields = await self.decide(found, write.service_args())
            record_panel_action(found.ctx, f"{CATEGORY}/{self.label}", status)
            await self.fail_unless_ok(status, found.ctx, found.radio, fields)
            entry = await radio_entry(found.ctx, self.ctx.radio_page, saved, premium=True, stations=True)
            return Reply(200, entry)

        await self.run_write(body, work)


class RadioChatHandler(RadioWriteHandler):
    spec = CHAT_SPEC
    label = "chat"

    async def decide(self, found: RadioContext, args: dict[str, list[str]]) -> tuple[int, Any, list[str]]:
        outcome = await self.ctx.radio_page.submit(found.ctx, found.radio, args, "", self.tr)
        saved = await self.hub(found.ctx).store.get(found.radio.id) or found.radio
        return outcome.status, saved, error_fields(outcome.form)


class RadioStationHandler(RadioWriteHandler):
    """A estação principal de uma rádio ao vivo (uuid do diretório) ou a de uma rádio personalizada (id do servidor)."""

    spec = STATION_SPEC
    label = "estacao"

    async def decide(self, found: RadioContext, args: dict[str, list[str]]) -> tuple[int, Any, list[str]]:
        ctx, radio = found.ctx, found.radio
        if str(radio.kind) == KIND_CUSTOM:
            status, _message, _kind, saved = await self.ctx.stations_page.set_custom_station(ctx, radio, args, self.tr)
        else:
            outcome = await self.ctx.radio_page.live.set_station(ctx, radio, args, self.tr)
            status, saved = outcome.status, outcome.radio
        return status, saved, ["estacao"]


class RadioReserveHandler(RadioWriteHandler):
    """A estação reserva (só das rádios ao vivo); ``null`` tira a reserva."""

    spec = RESERVE_SPEC
    label = "reserva"

    async def decide(self, found: RadioContext, args: dict[str, list[str]]) -> tuple[int, Any, list[str]]:
        if found.radio.kind != KIND_LIVE:
            raise validation_failed("kind")
        outcome = await self.ctx.radio_page.live.set_reserve(found.ctx, found.radio, args, self.tr)
        return outcome.status, outcome.radio, ["reserva"]


class RadioSearchHandler(RadioRoute):
    """``GET …/radios/{r}/stations?q=``: busca no diretório (só rádio ao vivo, só quem pode editar; R-7)."""

    @radio_endpoint
    async def get(self, guild_id: str, radio_id: str) -> None:
        found = await self.editable_radio(guild_id, radio_id, write=False)
        if found.radio.kind != KIND_LIVE:
            raise not_found()
        raw = self.query_arguments("q").get("q", "")[:SEARCH_RAW_LIMIT]
        if isinstance(check_search_term(raw), RuleError):  # termo fora da regra nunca chega ao diretório
            raise validation_failed("q")
        self.spend_directory_budget()
        results, _hint = await self.ctx.radio_page.live.search(found.ctx, found.radio, raw, self.tr)
        record_panel_action(found.ctx, f"{CATEGORY}/estacoes", 200)
        self.write({"stations": [station_view_entry(view) for view in results]})


class RadioLocalHandler(RadioRoute):
    """``GET …/radios/{r}/local?node=&pagina=``: a cascata de local; só o nó final vai ao diretório (R4 §5)."""

    @radio_endpoint
    async def get(self, guild_id: str, radio_id: str) -> None:
        found = await self.editable_radio(guild_id, radio_id, write=False)
        ctx, radio, live = found.ctx, found.radio, self.ctx.radio_page.live
        tree = tree_of(ctx)
        if radio.kind != KIND_LIVE or tree is None:
            raise not_found()
        query = self.query_arguments("node", "pagina")
        wanted = query.get("node", "")
        node = resolve_node(tree, wanted) if wanted else None
        if wanted and node is None:  # id forjado ou de uma árvore antiga
            raise validation_failed("node")
        results: tuple = ()
        paging = None
        if node is not None and getattr(node, "final", False):
            self.spend_directory_budget()
            results, _hint, paging = await live.stations(ctx, radio, node, parse_page(query.get("pagina")), self.tr)
        record_panel_action(ctx, f"{CATEGORY}/local", 200)
        self.write(
            {
                **local_entry(live.local_view(ctx, node)),
                "stations": [station_view_entry(view) for view in results],
                "paging": paging_entry(paging),
            }
        )


class RadioResetPrepareHandler(RadioRoute):
    """``POST …/radios/{r}/reset/prepare``: passo 1, só emite o token (nada é apagado). Não pede premium: um servidor que o
    perdeu apaga a própria configuração; quem faz precisa só de Gerenciar Servidor, conferido agora."""

    @radio_endpoint
    async def post(self, guild_id: str, radio_id: str) -> None:
        found = await self.radio_context(guild_id, radio_id, write=True)
        self.json_body()
        record_panel_action(found.ctx, f"{CATEGORY}/reset", 200)
        self.write(
            {
                "confirm_token": self.ctx.radio_setup_service.reset_token(found.ctx, found.radio),
                "expires_in": CONFIRM_TTL_SECONDS,
                "radio": {
                    "id": str(found.radio.id),
                    "kind": str(found.radio.kind),
                    "enabled": bool(found.radio.enabled),
                },
            }
        )


class RadioResetHandler(RadioRoute):
    """``POST …/radios/{r}/reset``: passo 2, apaga a rádio com o token do passo 1 (``204``); sem versão (T-422b/R1).

    A rádio é procurada **dentro** da escrita: com ``Idempotency-Key``, quem repete um reset que já apagou recebe o ``204``
    guardado (e não um ``404``). O servidor já foi reautorizado antes de consultar a chave."""

    @radio_endpoint
    async def post(self, guild_id: str, radio_id: str) -> None:
        ctx, _premium = await self.guild_context(guild_id, write=True)
        body = self.json_body()
        token = parse_reset_token(body)

        async def work() -> Reply:
            radio = await self.radio_of(ctx, radio_id)
            status = await self.ctx.radio_setup_service.reset_with_token(ctx, radio, token)
            record_panel_action(ctx, f"{CATEGORY}/reset", status)
            raise_for_status(status, ["confirm_token"])
            return Reply(STATUS_NO_CONTENT, None)

        await self.run_write(body, work)


class RadioLogoHandler(RadioRoute):
    """``GET …/radios/{r}/logo``: o logo (PNG) da estação principal da rádio, ou o ícone padrão. Só depois da sessão, do
    servidor e da rádio; a URL do logo nunca sai e o motivo de uma falha também não."""

    @radio_endpoint
    async def get(self, guild_id: str, radio_id: str) -> None:
        found = await self.radio_context(guild_id, radio_id, write=False)
        uuid = found.radio.source.get("station") if found.radio.kind == KIND_LIVE else None
        png, _max_age = await lookup_logo(self.ctx.pool, uuid)
        self.set_header("Content-Type", "image/png")
        self.finish(png)


def radio_routes() -> list[tuple[str, Any, str]]:
    """(expressão do caminho sob o prefixo, handler, caminho do OpenAPI)."""
    base = f"/guilds/{GUILD_ID}/radios"
    spec = "/guilds/{guild_id}/radios"
    one = f"{base}/{RADIO_ID}"
    spec_one = f"{spec}/{{radio_id}}"
    return [
        (base, RadiosHandler, spec),
        (one, RadioHandler, spec_one),
        (f"{one}/chat", RadioChatHandler, f"{spec_one}/chat"),
        (f"{one}/station", RadioStationHandler, f"{spec_one}/station"),
        (f"{one}/stations", RadioSearchHandler, f"{spec_one}/stations"),
        (f"{one}/local", RadioLocalHandler, f"{spec_one}/local"),
        (f"{one}/reserve", RadioReserveHandler, f"{spec_one}/reserve"),
        (f"{one}/reset/prepare", RadioResetPrepareHandler, f"{spec_one}/reset/prepare"),
        (f"{one}/reset", RadioResetHandler, f"{spec_one}/reset"),
        (f"{one}/logo", RadioLogoHandler, f"{spec_one}/logo"),
    ]
