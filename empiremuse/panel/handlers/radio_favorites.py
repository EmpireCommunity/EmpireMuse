"""Página Rádios · Favoritas do servidor (J3): ``POST /painel/servidores/<gid>/radios/favoritas`` com ``acao=adicionar|remover``
e ``GET .../favoritas/local?node=&pagina=`` (a cascata de local que escolhe a estação a adicionar).

Só HTTP. Sessão e XSRF vêm da base (``xsrf_cookies``); o servidor é o da rota, autorizado por ``GuildAccess`` com
``fresh=True`` em toda escrita, e o ator é o usuário da sessão: ``user_id`` e ``guild_id`` do corpo são ignorados. As
regras (gate ``can_manage_guild``, leitura do nome pelo diretório, estados do resultado) ficam em
``panel/services/radio_favorites.py``. A cascata só chama o diretório para o nó final e depois do limitador de escrita.
"""

from __future__ import annotations

import tornado.web

from empiremuse.panel.authz import GuildAccessDenied
from empiremuse.panel.handlers.radio import RadiosHandler
from empiremuse.panel.handlers.servers import SERVERS_PATH
from empiremuse.panel.handlers.settings import (
    MAX_BODY_BYTES,
    RADIOS_CATEGORY,
    UNAVAILABLE_TITLE,
    decode_args,
)
from empiremuse.panel.services.base import panel_translator
from empiremuse.panel.services.radio_favorites import (
    ACTION_ADD,
    ACTION_REMOVE,
    STATUS_FORBIDDEN,
    ServerFavoritesPage,
    node_of,
    page_of,
)
from empiremuse.panel.services.radio_live import list_state
from empiremuse.panel.services.radio_local import NODE_FIELD, NODE_LIMIT, PAGE_FIELD
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.usage import record_panel_action

FRAGMENT_TEMPLATE = "bloco_radio_favoritas.html"
LOCAL_TEMPLATE = "_radio_favoritas_local_fragmento.html"
_MSG_RATE = "Muitas alterações em pouco tempo. Aguarde um minuto e tente novamente."
_MSG_TOO_LARGE = ("Envio grande demais", "Os dados enviados excedem o limite permitido.")
_RATE_TITLE = "Muitas requisições"
_UNKNOWN_ACTION_KEY = "panel.radio.favorites.invalid"


class RadioFavoritesBase(RadiosHandler):
    favorites = ServerFavoritesPage()

    async def get(self, guild_id: str, *rest: str) -> None:  # type: ignore[override]
        try:
            await self._get(guild_id)
        except RadioUnavailable:
            self._unavailable()

    async def post(self, guild_id: str, *rest: str) -> None:  # type: ignore[override]
        try:
            await self._post(guild_id)
        except RadioUnavailable:
            self._unavailable()

    async def _get(self, guild_id: str) -> None:
        raise tornado.web.HTTPError(405)

    async def _post(self, guild_id: str) -> None:
        raise tornado.web.HTTPError(405)


class RadioFavoritesHandler(RadioFavoritesBase):
    """``POST /painel/servidores/<gid>/radios/favoritas``: adiciona ou remove uma favorita do servidor."""

    async def _get(self, guild_id: str) -> None:
        self.redirect(f"{SERVERS_PATH}/{guild_id}/{RADIOS_CATEGORY}")

    async def _post(self, guild_id: str) -> None:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=True)
        except GuildAccessDenied:
            self._not_found()
            return
        args = decode_args(self)
        what = (args.get("acao") or [""])[0]
        tr = panel_translator(ctx.pool)
        if what == ACTION_ADD:
            outcome = await self.favorites.add(ctx, args, tr)
        elif what == ACTION_REMOVE:
            outcome = await self.favorites.remove(ctx, args, tr)
        else:
            self._reject_plain(422, UNAVAILABLE_TITLE, tr(_UNKNOWN_ACTION_KEY))
            return
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/favoritas/{what}", outcome.status)
        state = list_state(args)  # a mesma lista de estações que o usuário estava vendo (nó e página)
        view = await self.favorites.view(
            ctx,
            action,
            message=outcome.message,
            kind=outcome.kind,
            node=node_of(ctx, state.node),
            page=page_of(state.page),
            tr=tr,
        )
        if view is None:  # sem permissão de gerenciar: nada do bloco volta
            self._reject_plain(STATUS_FORBIDDEN, UNAVAILABLE_TITLE, outcome.message)
            return
        self.set_status(outcome.status)
        if self._is_htmx():
            self.render_page(FRAGMENT_TEMPLATE, favoritas=view, **self._common(access))
            return
        await self._render_page(outcome.status, access, ctx, action, favoritas=view)


class RadioFavoritesLocalHandler(RadioFavoritesBase):
    """``GET /painel/servidores/<gid>/radios/favoritas/local?node=&pagina=``: a cascata de local e a lista do nó final.

    Navegar pelos níveis é leitura em memória; só o nó final vai ao diretório (balde do servidor), depois do limitador
    de escrita. O ``node`` só vale se a árvore o conhece.
    """

    async def _get(self, guild_id: str) -> None:
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        tr = panel_translator(ctx.pool)
        if getattr(ctx.pool.radio, "locations", None) is None or getattr(ctx.pool.radio, "favorites", None) is None:
            self._not_found()
            return
        wanted = self._wanted_node()
        node = node_of(ctx, wanted)
        status = 200
        if wanted and node is None:  # id forjado ou de uma árvore antiga: volta ao topo e avisa
            status = 422
        elif node is not None and getattr(node, "final", False) and not self._write_allowed():
            self.set_header(
                "Retry-After", "60"
            )  # a lista gasta o balde global do diretório: vale o limitador de escrita
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        page = page_of(self.get_query_argument(PAGE_FIELD, "", strip=False))
        view = await self.favorites.view(ctx, action, node=node, page=page, tr=tr)
        if view is None:
            self._reject_plain(STATUS_FORBIDDEN, UNAVAILABLE_TITLE, tr("panel.radio.favorites.forbidden"))
            return
        if status == 422:
            view = view._replace(dica=tr("panel.radio.local.invalid"))
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/favoritas/local", status)
        self.set_status(status)
        if self._is_htmx():
            self.render_page(LOCAL_TEMPLATE, favoritas=view, **self._common(access))
            return
        await self._render_page(status, access, ctx, action, favoritas=view)

    def _wanted_node(self) -> str:
        """O último ``node`` não vazio: sem JavaScript o formulário manda todos os selects da cascata."""
        values = [v for v in self.get_query_arguments(NODE_FIELD, strip=False) if v]
        return values[-1][: NODE_LIMIT + 1] if values else ""
