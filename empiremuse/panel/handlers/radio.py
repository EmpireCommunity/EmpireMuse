"""Página Rádios (item 8): ``GET /painel/servidores/<gid>/radios`` lista as rádios do servidor;
``POST /painel/servidores/<gid>/radios/<bot:guild>`` grava as regras do chat da call (só ``chat.*``);
``/reserva`` e ``/estacao`` (POST) trocam a reserva e a principal de uma rádio ao vivo (ou a estação de uma rádio
personalizada); ``/estacoes?q=`` (busca) e ``/local?node=&pagina=`` (cascata de local, R4) devolvem a lista de estações
para o seletor; ``POST /radios/estacoes`` e ``/radios/estacoes/<id>`` criam, editam e apagam as estações
personalizadas do servidor (R5); ``POST /radios/nova``, ``/radios/<bot:guild>/config`` e ``/radios/<bot:guild>/reset``
criam, configuram (tipo, canal, bot reserva) e redefinem uma rádio pela função única do bot (contrato-nomes-radios §4.3).

Só HTTP: autorização da R-6, allowlist, validação e compare-and-set ficam em ``panel/services/radio.py``. Banco das
rádios fora do ar (``RadioUnavailable``) responde 503 com mensagem própria, como as outras páginas.
"""

from __future__ import annotations

from typing import Any, Optional

import tornado.web

from empiremuse.panel.authz import GuildAccess, GuildAccessDenied
from empiremuse.panel.handlers.servers import SERVERS_PATH, initial_of
from empiremuse.panel.handlers.settings import (
    MAX_BODY_BYTES,
    RADIOS_CATEGORY,
    UNAVAILABLE_TITLE,
    SettingsHandler,
    decode_args,
)
from empiremuse.panel.services.base import KIND_ERROR, FormContext, Formulario, panel_translator
from empiremuse.panel.services.radio import (
    FORBIDDEN_KEY,
    STATUS_FORBIDDEN,
    RadioPage,
    belongs_to,
    can_write,
    radio_id_of,
)
from empiremuse.panel.services.radio_live import (
    SEARCH_FIELD,
    SEARCH_RAW_LIMIT,
    ListState,
    LiveOutcome,
    LiveView,
    list_state,
)
from empiremuse.panel.services.radio_local import NODE_FIELD, PAGE_FIELD, NODE_LIMIT, parse_page, resolve_node, tree_of
from empiremuse.panel.services.radio_stations import (
    ACTION_DELETE,
    ACTION_SAVE,
    StationsOutcome,
    StationsPage,
    StationsView,
    station_id_of,
)
from empiremuse.panel.services.radio_setup import (
    CONFIG_PATH,
    NEW_PATH,
    RESET_PATH,
    RadioSetupService,
    ResetView,
    SetupOutcome,
)
from empiremuse.panel.services.radio_stations import belongs_to as station_belongs_to
from empiremuse.panel.services.service import Outcome
from empiremuse.rules.radio import KIND_CUSTOM, KIND_LIVE, MAX_PER_GUILD
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.usage import record_panel_action

PAGE_TEMPLATE = "config_radios.html"
FORM_TEMPLATE = "form_radio_chat.html"
LIVE_TEMPLATE = "form_radio_reserva.html"
STATIONS_TEMPLATE = "_radio_estacoes.html"
LOCAL_TEMPLATE = "_radio_local_fragmento.html"
STATIONS_SECTION_TEMPLATE = "config_radio_stations.html"
CUSTOM_TEMPLATE = "form_radio_estacao.html"
NEW_TEMPLATE = "_radio_nova.html"
CONFIG_TEMPLATE = "_radio_config.html"
STATIONS_PATH = "estacoes"
UNAVAILABLE_KEY = "panel.radio.unavailable"
_MSG_RATE = "Muitas alterações em pouco tempo. Aguarde um minuto e tente novamente."
_MSG_TOO_LARGE = ("Envio grande demais", "Os dados enviados excedem o limite permitido.")
_RATE_TITLE = "Muitas requisições"


class RadiosHandler(SettingsHandler):
    """``GET /painel/servidores/<gid>/radios``."""

    page = RadioPage()
    stations = StationsPage()

    async def get(self, guild_id: str) -> None:  # type: ignore[override]
        try:
            await self._get(guild_id)
        except RadioUnavailable:
            self._unavailable()

    async def post(self, guild_id: str) -> None:  # type: ignore[override]
        """A lista não recebe POST: cada rádio tem a própria URL."""
        raise tornado.web.HTTPError(405)

    async def _get(self, guild_id: str) -> None:
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        await self._render_page(200, access, ctx, action)

    def _unavailable(self) -> None:
        text = panel_translator(self.application.settings.get("pool"))(UNAVAILABLE_KEY)
        self.render_error(503, UNAVAILABLE_TITLE, text)

    async def _render_page(
        self,
        status: int,
        access: GuildAccess,
        ctx: FormContext,
        action: str,
        *,
        form: Optional[Formulario] = None,
        live: Optional[LiveView] = None,
        stations: Optional[StationsView] = None,
        custom: Any = None,
        nova: Optional[Formulario] = None,
        config: Optional[Formulario] = None,
        redefinir: Optional[ResetView] = None,
    ) -> None:
        tr = panel_translator(ctx.pool)
        premium = await ctx.pool.radio.premium.get(ctx.guild_id)
        editable = can_write(premium)
        rows = await self.page.rows(ctx, action, editable, tr)
        if redefinir is not None and redefinir.removida:  # a rádio acabou de ser redefinida: já não está na lista
            rows = rows + (self._removed_row(redefinir),) if not any(r.id == redefinir.radio_id for r in rows) else rows
        if form is not None:  # o resultado do POST entra no lugar do formulário daquela rádio
            rows = tuple(row._replace(form=form) if row.id == form.radio_id else row for row in rows)
        if live is not None:
            rows = tuple(row._replace(ao_vivo=live) if row.id == live.radio_id else row for row in rows)
        if editable:
            if stations is None:
                stations = await self.stations.view(ctx, f"{action}/{STATIONS_PATH}", tr)
            rows = await self._with_custom(ctx, action, rows, tr, custom)
            rows, nova = await self._with_setup(ctx, action, rows, nova, config, redefinir)
        self.set_status(status)
        self.render_page(
            PAGE_TEMPLATE,
            **self._common(access),
            mostrar_seletor=False,
            premium=premium,
            pode_escrever=editable,
            radios=rows,
            max_radios=MAX_PER_GUILD,
            estacoes=stations,
            nova=nova,
            mostrar_nova=editable,
        )

    @property
    def setup(self) -> RadioSetupService:
        return self.application.settings["radio_setup_service"]

    @staticmethod
    def names_of(row: Any) -> tuple[str, str]:
        return str(row.bot_nome), str(row.canal_nome or "—")

    @staticmethod
    def _removed_row(view: ResetView) -> Any:
        """Só o bloco "Rádio redefinida." no lugar da rádio que saiu (página inteira, sem htmx)."""
        from empiremuse.panel.services.radio import RadioRow

        return RadioRow(
            id=view.radio_id,
            slug=view.slug,
            bot_nome=view.bot_nome,
            canal_nome=view.canal_nome,
            tipo="",
            estado="",
            titulo=None,
            form=None,
            config=Formulario(key="radio_setup", form_id="", template="", acao="", versao="", valores={}),
            redefinir=view,
        )

    async def _with_setup(
        self,
        ctx: FormContext,
        action: str,
        rows: tuple,
        nova: Optional[Formulario],
        config: Optional[Formulario],
        redefinir: Optional[ResetView],
    ) -> tuple[tuple, Optional[Formulario]]:
        """Com premium: o formulário de criar (ou o aviso de máximo) e, em cada rádio, configurar + redefinir (§4.3);
        o resultado de um POST entra no lugar do bloco daquela rádio."""
        options = await self.setup.options(ctx)
        done = []
        for row in rows:
            if row.redefinir is not None:  # a linha sintética da rádio redefinida
                done.append(row)
                continue
            radio = row.radio
            form = config if config is not None and config.radio_id == row.id else None
            if form is None:
                current = (int(radio.bot_id), str(row.bot_nome))
                form = self.setup.form(radio, f"{action}/{row.id}/{CONFIG_PATH}", options, current_bot=current)
            view = redefinir if redefinir is not None and redefinir.radio_id == row.id else None
            if view is None:
                view = self.setup.reset_view(radio, f"{action}/{row.id}/{RESET_PATH}", self.names_of(row))
            done.append(row._replace(config=form, redefinir=view))
        if nova is None and len([r for r in done if r.redefinir is None or not r.redefinir.removida]) < MAX_PER_GUILD:
            nova = self.setup.form(None, f"{action}/{NEW_PATH}", options)
        return tuple(done), nova

    async def _with_custom(self, ctx: FormContext, action: str, rows: tuple, tr: Any, custom: Any) -> tuple:
        """O seletor de estação em cada rádio personalizada editável (o resultado do POST entra na rádio dele)."""
        if not any(row.form is not None and str(row.radio.kind) == KIND_CUSTOM for row in rows):
            return rows
        stations = await ctx.pool.radio.stations.list_for_guild(ctx.guild_id)
        done = []
        for row in rows:
            if row.form is None or str(row.radio.kind) != KIND_CUSTOM:
                done.append(row)
                continue
            if custom is not None and custom.radio_id == row.id:
                done.append(row._replace(personalizada=custom))
                continue
            view = self.stations.custom_view(ctx, row.radio, f"{action}/{row.id}", stations, tr)
            done.append(row._replace(personalizada=view))
        return tuple(done)

    @staticmethod
    def _common(access: GuildAccess) -> dict[str, Any]:
        return {"acesso": access, "inicial": initial_of(access.name), "bot_atual": None}

    async def _radio_for_write(self, guild_id: str, radio_id: str, *, fresh: bool) -> Optional[tuple]:
        """``(ctx, access, action, radio, pode)`` ou ``None`` depois de responder 404; ``pode`` = premium e ``manage_messages``."""
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=fresh)
        except GuildAccessDenied:
            self._not_found()
            return None
        wanted = radio_id_of(ctx.guild_id, radio_id)
        radio = await ctx.pool.radio.store.get(wanted) if wanted is not None else None
        if radio is None or not belongs_to(radio, ctx.guild_id):  # rádio de outro servidor: 404, como um id inexistente
            self._not_found()
            return None
        premium = await ctx.pool.radio.premium.get(ctx.guild_id)
        allowed = can_write(premium) and await self.page.can_edit(ctx, radio)
        return ctx, access, action, radio, allowed


class RadioChatHandler(RadiosHandler):
    """``POST /painel/servidores/<gid>/radios/<bot:guild>``: regras do chat de uma rádio (R1 §3, R-6)."""

    async def get(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        self.redirect(f"{SERVERS_PATH}/{guild_id}/{RADIOS_CATEGORY}")

    async def post(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        try:
            await self._post(guild_id, radio_id)
        except RadioUnavailable:
            self._unavailable()

    async def _post(self, guild_id: str, radio_id: str) -> None:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        found = await self._radio_for_write(guild_id, radio_id, fresh=True)
        if found is None:
            return
        ctx, access, action, radio, allowed = found
        tr = panel_translator(ctx.pool)
        form_action = f"{action}/{radio.id}"
        if not allowed:
            form = self.page.form(radio, form_action, message=tr(FORBIDDEN_KEY), kind=KIND_ERROR)
            outcome = Outcome(STATUS_FORBIDDEN, form)
        else:
            outcome = await self.page.submit(ctx, radio, decode_args(self), form_action, tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/chat", outcome.status)
        if self._is_htmx():
            self.set_status(outcome.status)
            self.render_page(FORM_TEMPLATE, form=outcome.form, **self._common(access))
            return
        await self._render_page(outcome.status, access, ctx, action, form=outcome.form)


class RadioStationSearchHandler(RadiosHandler):
    """``GET /painel/servidores/<gid>/radios/<bot:guild>/estacoes?q=``: busca no diretório para o seletor da reserva (R3, R-7)."""

    async def get(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        try:
            await self._search(guild_id, radio_id)
        except RadioUnavailable:
            self._unavailable()

    async def post(self, *args: str) -> None:  # type: ignore[override]
        raise tornado.web.HTTPError(405)

    async def _search(self, guild_id: str, radio_id: str) -> None:
        if not self._write_allowed():  # cada busca gasta o bucket global do diretório: vale o limitador de escrita
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        found = await self._radio_for_write(guild_id, radio_id, fresh=False)
        if found is None:
            return
        ctx, access, action, radio, allowed = found
        if radio.kind != KIND_LIVE:
            self._not_found()
            return
        tr = panel_translator(ctx.pool)
        if not allowed:  # sem premium ou sem manage_messages: nenhuma chamada ao diretório (R-7)
            self._reject_plain(STATUS_FORBIDDEN, UNAVAILABLE_TITLE, tr(FORBIDDEN_KEY))
            return
        term = self.get_query_argument(SEARCH_FIELD, "", strip=False)[:SEARCH_RAW_LIMIT]
        results, hint = await self.page.live.search(ctx, radio, term, tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/estacoes", 200)
        view = await self.page.live.view(
            ctx, radio, f"{action}/{radio.id}", True, results=results, hint=hint, term=term
        )
        if self._is_htmx():
            self.render_page(STATIONS_TEMPLATE, vivo=view, form=view.form, **self._common(access))
            return
        await self._render_page(200, access, ctx, action, live=view)


class RadioSourceHandler(RadiosHandler):
    """Base dos POSTs que trocam ``source.*`` de uma rádio ao vivo (R3 §4, R4 §5, R-6): a página volta com a mesma lista."""

    field = ""
    action_name = ""

    async def get(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        self.redirect(f"{SERVERS_PATH}/{guild_id}/{RADIOS_CATEGORY}")

    async def post(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        try:
            await self._post(guild_id, radio_id)
        except RadioUnavailable:
            self._unavailable()

    async def _decide(self, ctx: FormContext, radio: Any, args: dict, tr: Any) -> LiveOutcome:
        raise NotImplementedError

    async def _post(self, guild_id: str, radio_id: str) -> None:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        found = await self._radio_for_write(guild_id, radio_id, fresh=True)
        if found is None:
            return
        await self._post_found(found)

    async def _post_found(self, found: tuple) -> None:
        ctx, access, action, radio, allowed = found
        tr = panel_translator(ctx.pool)
        radio_action = f"{action}/{radio.id}"
        args = decode_args(self)
        if not allowed:
            outcome = LiveOutcome(STATUS_FORBIDDEN, tr(FORBIDDEN_KEY), KIND_ERROR, radio)
            state = ListState()  # sem permissão a lista não volta: nada vai ao diretório (R-7)
        else:
            outcome = await self._decide(ctx, radio, args, tr)
            state = list_state(args)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/{self.action_name}", outcome.status)
        view = await self.page.live.reload(ctx, outcome.radio, radio_action, state, tr, outcome)
        self.set_status(outcome.status)
        if self._is_htmx():
            self.render_page(LIVE_TEMPLATE, vivo=view, **self._common(access))
            return
        await self._render_page(outcome.status, access, ctx, action, live=view)


class RadioReserveHandler(RadioSourceHandler):
    """``POST /painel/servidores/<gid>/radios/<bot:guild>/reserva``: a estação reserva de uma rádio ao vivo (R3, R-6)."""

    action_name = "reserva"

    async def _decide(self, ctx: FormContext, radio: Any, args: dict, tr: Any) -> LiveOutcome:
        return await self.page.live.set_reserve(ctx, radio, args, tr)


class RadioStationHandler(RadioSourceHandler):
    """``POST /painel/servidores/<gid>/radios/<bot:guild>/estacao``: a estação principal de uma rádio ao vivo (R4 §5) ou a
    estação de uma rádio personalizada (R5 §5); a rádio ligada troca na hora."""

    action_name = "estacao"

    async def _decide(self, ctx: FormContext, radio: Any, args: dict, tr: Any) -> LiveOutcome:
        return await self.page.live.set_station(ctx, radio, args, tr)

    async def _post(self, guild_id: str, radio_id: str) -> None:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        found = await self._radio_for_write(guild_id, radio_id, fresh=True)
        if found is None:
            return
        ctx, access, action, radio, allowed = found
        if str(radio.kind) != KIND_CUSTOM:
            await self._post_found(found)
            return
        tr = panel_translator(ctx.pool)
        args = decode_args(self)
        if not allowed:
            status, message, kind, after = STATUS_FORBIDDEN, tr(FORBIDDEN_KEY), KIND_ERROR, radio
        else:
            status, message, kind, after = await self.stations.set_custom_station(ctx, radio, args, tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/estacao", status)
        stations = await ctx.pool.radio.stations.list_for_guild(ctx.guild_id)
        view = self.stations.custom_view(ctx, after, f"{action}/{radio.id}", stations, tr, message=message, kind=kind)
        self.set_status(status)
        if self._is_htmx():
            self.render_page(CUSTOM_TEMPLATE, personalizada=view, form=view.form, **self._common(access))
            return
        await self._render_page(status, access, ctx, action, custom=view)


class RadioLocalHandler(RadiosHandler):
    """``GET /painel/servidores/<gid>/radios/<bot:guild>/local?node=&pagina=``: a cascata de local (R4 §5).

    Só o nó final vai ao diretório (``region``, até 4 pedidos, balde por servidor), e só depois do limitador de escrita;
    navegar pelos níveis é leitura em memória. O ``node`` do usuário só vale se ``tree.node`` o conhece.
    """

    async def get(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        try:
            await self._local(guild_id, radio_id)
        except RadioUnavailable:
            self._unavailable()

    async def post(self, *args: str) -> None:  # type: ignore[override]
        raise tornado.web.HTTPError(405)

    def _wanted_node(self) -> str:
        """O último ``node`` não vazio: sem JavaScript o formulário manda todos os selects da cascata."""
        values = [v for v in self.get_query_arguments(NODE_FIELD, strip=False) if v]
        return values[-1][: NODE_LIMIT + 1] if values else ""

    async def _local(self, guild_id: str, radio_id: str) -> None:
        found = await self._radio_for_write(guild_id, radio_id, fresh=False)
        if found is None:
            return
        ctx, access, action, radio, allowed = found
        tree = tree_of(ctx)
        if radio.kind != KIND_LIVE or tree is None:
            self._not_found()
            return
        tr = panel_translator(ctx.pool)
        if not allowed:  # sem premium ou sem manage_messages: nem a árvore, nem o diretório (R-7)
            self._reject_plain(STATUS_FORBIDDEN, UNAVAILABLE_TITLE, tr(FORBIDDEN_KEY))
            return
        wanted = self._wanted_node()
        node = resolve_node(tree, wanted) if wanted else None
        status = 200
        results: tuple = ()
        hint = None
        paging = None
        if wanted and node is None:  # id forjado ou de uma árvore antiga: volta ao topo e avisa
            status, hint = 422, tr("panel.radio.local.invalid")
        elif node is not None and getattr(node, "final", False):
            if not self._write_allowed():  # a lista gasta o bucket global do diretório: vale o limitador de escrita
                self.set_header("Retry-After", "60")
                self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
                return
            page = parse_page(self.get_query_argument(PAGE_FIELD, "", strip=False))
            results, hint, paging = await self.page.live.stations(ctx, radio, node, page, tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/local", status)
        view = await self.page.live.view(
            ctx,
            radio,
            f"{action}/{radio.id}",
            True,
            results=results,
            hint=hint,
            local=self.page.live.local_view(ctx, node),
            paging=paging,
        )
        self.set_status(status)
        if self._is_htmx():
            self.render_page(LOCAL_TEMPLATE, vivo=view, form=view.form, **self._common(access))
            return
        await self._render_page(status, access, ctx, action, live=view)


class RadioStationsHandler(RadiosHandler):
    """``POST /painel/servidores/<gid>/radios/estacoes``: cria uma estação personalizada do servidor (R5 §5)."""

    async def get(self, guild_id: str, *rest: str) -> None:  # type: ignore[override]
        self.redirect(f"{SERVERS_PATH}/{guild_id}/{RADIOS_CATEGORY}")

    async def post(self, guild_id: str, *rest: str) -> None:  # type: ignore[override]
        try:
            await self._post(guild_id, *rest)
        except RadioUnavailable:
            self._unavailable()

    async def _gate(self, guild_id: str) -> Optional[tuple]:
        """``(ctx, access, action)`` ou ``None`` depois de responder (413, 429, 404 ou 403 sem premium)."""
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return None
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return None
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=True)
        except GuildAccessDenied:
            self._not_found()
            return None
        if not can_write(await ctx.pool.radio.premium.get(ctx.guild_id)):  # sem premium: nada a gerenciar
            tr = panel_translator(ctx.pool)
            self._reject_plain(STATUS_FORBIDDEN, UNAVAILABLE_TITLE, tr(FORBIDDEN_KEY))
            return None
        return ctx, access, action

    async def _post(self, guild_id: str, *rest: str) -> None:
        gated = await self._gate(guild_id)
        if gated is None:
            return
        ctx, access, action = gated
        tr = panel_translator(ctx.pool)
        outcome = await self.stations.create(ctx, decode_args(self), f"{action}/{STATIONS_PATH}", tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/{STATIONS_PATH}", outcome.status)
        await self._respond(outcome, access, ctx, action)

    async def _respond(self, outcome: StationsOutcome, access: GuildAccess, ctx: FormContext, action: str) -> None:
        self.set_status(outcome.status)
        if self._is_htmx():
            self.render_page(STATIONS_SECTION_TEMPLATE, estacoes=outcome.view, **self._common(access))
            return
        await self._render_page(outcome.status, access, ctx, action, stations=outcome.view)


class RadioStationItemHandler(RadioStationsHandler):
    """``POST /painel/servidores/<gid>/radios/estacoes/<id>`` com ``acao=salvar|apagar`` (R5 §5); outro servidor é 404."""

    async def _post(self, guild_id: str, *rest: str) -> None:
        gated = await self._gate(guild_id)
        if gated is None:
            return
        ctx, access, action = gated
        wanted = station_id_of(rest[0]) if rest else None
        station = await ctx.pool.radio.stations.get(wanted) if wanted is not None else None
        if station is None or not station_belongs_to(station, ctx.guild_id):
            self._not_found()
            return
        tr = panel_translator(ctx.pool)
        args = decode_args(self)
        what = (args.get("acao") or [""])[0]
        list_action = f"{action}/{STATIONS_PATH}"
        if what == ACTION_DELETE:
            outcome = await self.stations.delete(ctx, station, args, list_action, tr)
        elif what == ACTION_SAVE:
            outcome = await self.stations.update(ctx, station, args, list_action, tr)
        else:
            self._reject_plain(422, UNAVAILABLE_TITLE, tr("panel.radio.station.error.action"))
            return
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/{STATIONS_PATH}/{what}", outcome.status)
        await self._respond(outcome, access, ctx, action)


class RadioSetupHandler(RadiosHandler):
    """Base dos POSTs do §4.3: 413, 429, 404 (servidor ou rádio de outro servidor) e 403 sem premium antes de qualquer
    chamada ao bot; ``manage_guild`` de quem faz é conferido pelo ``GuildAccess`` e de novo pelo bot."""

    async def get(self, guild_id: str, *rest: str) -> None:  # type: ignore[override]
        self.redirect(f"{SERVERS_PATH}/{guild_id}/{RADIOS_CATEGORY}")

    async def post(self, guild_id: str, *rest: str) -> None:  # type: ignore[override]
        try:
            await self._post(guild_id, *rest)
        except RadioUnavailable:
            self._unavailable()

    async def _post(self, guild_id: str, *rest: str) -> None:
        raise NotImplementedError

    async def _gate(self, guild_id: str) -> Optional[tuple]:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return None
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return None
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=True)
        except GuildAccessDenied:
            self._not_found()
            return None
        if not can_write(await ctx.pool.radio.premium.get(ctx.guild_id)):
            tr = panel_translator(ctx.pool)
            self._reject_plain(STATUS_FORBIDDEN, UNAVAILABLE_TITLE, tr("panel.radio.setup.forbidden.premium"))
            return None
        return ctx, access, action

    async def _radio_of(self, ctx: FormContext, radio_id: str) -> Any:
        """A rádio do id da rota, só se é deste servidor (R-6); ``None`` depois de responder 404."""
        wanted = radio_id_of(ctx.guild_id, radio_id)
        radio = await ctx.pool.radio.store.get(wanted) if wanted is not None else None
        if radio is None or not belongs_to(radio, ctx.guild_id):
            self._not_found()
            return None
        return radio

    async def _row_of(self, ctx: FormContext, action: str, radio: Any) -> Any:
        tr = panel_translator(ctx.pool)
        rows = await self.page.rows(ctx, action, True, tr)
        return next((row for row in rows if row.id == str(radio.id)), None)


class RadioNewHandler(RadioSetupHandler):
    """``POST /painel/servidores/<gid>/radios/nova``: cria uma rádio (tipo, canal, bot reserva)."""

    async def _post(self, guild_id: str, *rest: str) -> None:
        gated = await self._gate(guild_id)
        if gated is None:
            return
        ctx, access, action = gated
        tr = panel_translator(ctx.pool)
        outcome = await self.setup.configure(ctx, None, decode_args(self), f"{action}/{NEW_PATH}", tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/{NEW_PATH}", outcome.status)
        if outcome.form is None:
            self._not_found()
            return
        if self._is_htmx():
            self.set_status(outcome.status)
            self.render_page(NEW_TEMPLATE, nova=outcome.form, max_radios=MAX_PER_GUILD, **self._common(access))
            return
        await self._render_page(outcome.status, access, ctx, action, nova=outcome.form)


class RadioConfigHandler(RadioSetupHandler):
    """``POST /painel/servidores/<gid>/radios/<bot:guild>/config``: tipo, canal e bot reserva de uma rádio (CAS em rev)."""

    async def _post(self, guild_id: str, *rest: str) -> None:
        gated = await self._gate(guild_id)
        if gated is None:
            return
        ctx, access, action = gated
        radio = await self._radio_of(ctx, rest[0])
        if radio is None:
            return
        row = await self._row_of(ctx, action, radio)
        if row is None:
            self._not_found()
            return
        tr = panel_translator(ctx.pool)
        current = (int(radio.bot_id), str(row.bot_nome))
        outcome: SetupOutcome = await self.setup.configure(
            ctx, radio, decode_args(self), f"{action}/{radio.id}/{CONFIG_PATH}", tr, current_bot=current
        )
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/{CONFIG_PATH}", outcome.status)
        if outcome.form is None:
            self._not_found()
            return
        saved = outcome.radio if outcome.radio is not None else radio
        if self._is_htmx():
            row = await self._row_of(ctx, action, saved) or row
            view = self.setup.reset_view(saved, f"{action}/{saved.id}/{RESET_PATH}", self.names_of(row))
            self.set_status(outcome.status)
            self.render_page(CONFIG_TEMPLATE, config=outcome.form, redefinir=view, **self._common(access))
            return
        await self._render_page(outcome.status, access, ctx, action, config=outcome.form)


class RadioResetHandler(RadioSetupHandler):
    """``POST /painel/servidores/<gid>/radios/<bot:guild>/reset``: redefinir em dois passos (token assinado, 60 s)."""

    async def _post(self, guild_id: str, *rest: str) -> None:
        gated = await self._gate(guild_id)
        if gated is None:
            return
        ctx, access, action = gated
        radio = await self._radio_of(ctx, rest[0])
        if radio is None:
            return
        row = await self._row_of(ctx, action, radio)
        if row is None:
            self._not_found()
            return
        tr = panel_translator(ctx.pool)
        names = self.names_of(row)
        status, view = await self.setup.reset(
            ctx, radio, decode_args(self), f"{action}/{radio.id}/{RESET_PATH}", names, tr
        )
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/{RESET_PATH}", status)
        if view is None:
            self._not_found()
            return
        if self._is_htmx():
            options = await self.setup.options(ctx)
            current = (int(radio.bot_id), str(row.bot_nome))
            form = self.setup.form(radio, f"{action}/{radio.id}/{CONFIG_PATH}", options, current_bot=current)
            self.set_status(status)
            self.render_page(CONFIG_TEMPLATE, config=form, redefinir=view, **self._common(access))
            return
        await self._render_page(status, access, ctx, action, redefinir=view)
