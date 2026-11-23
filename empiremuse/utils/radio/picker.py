"""O seletor de estação por local do Discord (item 8, R4): região → país → estado → cidade → estação, num select por vez, só para quem abriu.

Abre pelo ``/radio ligar`` sem ``playlist`` nem ``estacao`` (liga uma rádio ao vivo num canal), pelo botão "Estações" do painel da rádio ao vivo (troca a
estação principal da rádio que já toca) e pela entrada "Rádios ao Vivo" da Jukebox (T-434 J2a: "Tocar na minha call"). O estado fica numa
``disnake.ui.View`` em memória (nó, página, lista de estações, escolha), com 300 s de vida e só para o autor; NADA dele vai em ``custom_id`` (R-6). O
select só traz ids que a árvore conhece e o que volta é conferido contra as opções mostradas agora: valor fora delas é recusado ("expirou"), nunca
trocado por outro. Premium e ``manage_guild`` são conferidos a cada passo que gasta recurso.

J2a (A2/A3/B1): no topo, 🔎 busca por nome, ⭐ minhas favoritas, ⭐ favoritas do servidor e 📌 meus filtros; na estação escolhida, as estrelas pessoal e
do servidor (esta só para quem gerencia, conferida de novo por quem grava); em cada nível com local, 📌 salvar filtro. As listas pessoais só existem
nesta mensagem efêmera, e o uuid de qualquer ação vem sempre da escolha guardada aqui (``self.chosen``), nunca do componente [m7, s6].
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.radio import LIST_MAX, VOLUME_DEFAULT
from empiremuse.rules.radio_geo import MAX_OPTIONS
from empiremuse.rules.radio_stations import Station, valid_uuid
from empiremuse.rules.radio_quality import quality_dot, station_quality
from empiremuse.rules.text import external_text, stored_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio.custom_favorites import Toggled
from empiremuse.utils.radio.directory import REASON_LIMITED, REASON_UNAVAILABLE, Hit
from empiremuse.utils.radio.favorites import (
    MAX_FAVORITES,
    AddResult,
    FavoriteForbidden,
    FavoriteNotFound,
    FavoriteStation,
)
from empiremuse.utils.radio.locations import LocationTree, LocNode, LocOption
from empiremuse.utils.radio.picker_parts import SearchModal, filter_label, load_filters, load_mine, manages
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.user_favorites import USER_FILTERS_MAX, USER_LIVE_FAVORITES_MAX

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

TIMEOUT_S = 300.0
PAGE_SIZE = MAX_OPTIONS
LABEL_LIMIT = 100
NAME_LIMIT = 80

FAVORITES_ID = "f:fav"  # as estações favoritas do servidor (uma lista como a de uma região)
SEARCH_ID = "f:search"  # abre o modal de busca por nome (A3)
MINE_ID = "f:mine"  # as MINHAS favoritas ao vivo (B1)
FILTERS_ID = "f:filters"  # os MEUS filtros de local (B1)
SEARCH_RESULTS_MAX = 24

LIST_FAV, LIST_MINE, LIST_SEARCH, LIST_FILTERS = "fav", "mine", "search", "filters"
_LIST_OF = {FAVORITES_ID: LIST_FAV, MINE_ID: LIST_MINE, FILTERS_ID: LIST_FILTERS}
_LIST_TITLE = {
    LIST_FAV: "radio.local.favorites",
    LIST_MINE: "radio.favorites.picker.mine",
    LIST_SEARCH: "jukebox.radio.search.results",
    LIST_FILTERS: "radio.favorites.picker.filters",
}

# Aplica a escolha: recebe a interação do clique, a estação e a lista de onde ela veio (para o anterior e a próxima), e devolve o texto final (ou
# levanta ``GenericError`` com o motivo traduzido).
Apply = Callable[[disnake.MessageInteraction, Station, tuple[str, ...]], Awaitable[str]]


def station_option(
    hub: RadioHub, tr: Translator, station: Station, check_ok: bool | None = None
) -> disnake.SelectOption:
    """Uma estação no select: o pontinho de qualidade (R5), o nome (R-11) e codec/taxa/país; indisponível pela nossa saúde diz isso na descrição."""
    technical = f"{station.codec} {station.bitrate}k" if station.bitrate else station.codec
    detail = " · ".join(part for part in (technical, stored_text(station.country, LABEL_LIMIT, markdown=False)) if part)
    unavailable = hub.health.unavailable(station.uuid)
    dot = quality_dot(station_quality(station.bitrate, check_ok=check_ok, unavailable=unavailable))
    label = f"{dot} {stored_text(station.name, NAME_LIMIT, markdown=False)}"
    if unavailable:
        detail = f"{tr('radio.live.unavailable')} · {detail}"
    return disnake.SelectOption(label=fit(label, LABEL_LIMIT), value=station.uuid, description=fit(detail, LABEL_LIMIT))


class RadioPicker(disnake.ui.View):
    def __init__(
        self,
        hub: RadioHub,
        tree: LocationTree,
        tr: Translator,
        *,
        author_id: int,
        guild_id: int,
        locale: str,
        guild_locale: Optional[str],
        apply: Apply,
        confirm_label: str,
        check: Callable[[disnake.MessageInteraction], bool],
        deny_key: str = "radio.refused.no_manage_guild",
        warning: Optional[Callable[[], str]] = None,
        no_results_key: str = "radio.local.no_results",
    ) -> None:
        super().__init__(timeout=TIMEOUT_S)
        self.hub, self.tree, self.tr = hub, tree, tr
        self.author_id, self.guild_id = author_id, guild_id
        self.locale, self.guild_locale = locale, guild_locale
        self._apply, self._confirm_label, self._check, self._deny_key = apply, confirm_label, check, deny_key
        self._warning, self._no_results_key = warning, no_results_key
        self.list_mode: Optional[str] = (
            None  # ``fav``, ``mine``, ``search`` ou ``filters``: uma lista no lugar da árvore
        )
        self._favorites: Optional[list[Any]] = None  # do servidor
        self._mine: Optional[list[FavoriteStation]] = None  # minhas, ao vivo
        self._filters: Optional[list[str]] = None  # meus, ids de nó
        self._manager = False  # o autor gerencia o servidor (último clique): só mostra a estrela do servidor
        self.node_id: Optional[str] = None
        self.page = 0
        self.hits: tuple[Hit, ...] = ()
        self.partial = False
        self.chosen: Optional[Station] = None
        self._offered: set[str] = set()  # os valores que o select de agora aceita
        self.message_text = ""

    @property
    def favorites_mode(self) -> bool:
        """Compatibilidade: a lista das favoritas do servidor está aberta."""
        return self.list_mode == LIST_FAV

    # ------------------------------------------------------------------ a mensagem de agora

    def _path_text(self) -> str:
        if self.node_id is None:
            return ""
        crumbs = [self.tree.label(node, self.locale) for node in self.tree.path(self.node_id)]
        return self.tr("radio.local.path", path=" › ".join(crumbs))

    def _prompt(self, node: Optional[LocNode]) -> str:
        if node is None:
            return self.tr("radio.local.pick_group")
        key = {"region": "pick_country", "country": "pick_state", "state": "pick_city"}.get(node.kind, "pick_station")
        return self.tr(f"radio.local.{key}")

    async def _load_lists(self) -> None:
        """As listas do topo e das estrelas, lidas uma vez por desenho (uma ação que muda uma delas a zera para a próxima leitura)."""
        if self._favorites is None:
            self._favorites = await self._load_favorites()
        if self._mine is None:
            self._mine = await load_mine(self.hub, self.author_id)
        if self._filters is None:
            self._filters = await load_filters(self.hub, self.tree, self.author_id)

    async def render(self) -> dict[str, Any]:
        """Monta ``content`` e a ``view`` do passo de agora (sempre uma nova leva de itens)."""
        self.clear_items()
        self._offered = set()
        node = self.tree.node(self.node_id) if self.node_id else None
        lines = [self.tr("radio.local.title")]
        await self._load_lists()
        if self.list_mode is not None:
            lines.append(self.tr("radio.local.path", path=self.tr(_LIST_TITLE[self.list_mode])))
        elif path := self._path_text():
            lines.append(path)
        if self.chosen is not None:
            lines.append(self.tr("radio.local.chosen", name=stored_text(self.chosen.name, NAME_LIMIT)))
            if self._warning is not None and (warning := self._warning()):
                lines.append(warning)
            self._add_confirm()
            self._add_stars()
        elif self.list_mode == LIST_FILTERS:
            self._add_filter_select()
        elif self.list_mode is not None:
            lines.extend(await self._add_station_select(None))
        elif node is None or not node.final:
            lines.append(self._prompt(node))
            self._add_level_select(self._level_options(node))
            self._add_filter_toggle()
        else:
            lines.extend(await self._add_station_select(node))
            self._add_filter_toggle()
        if self.node_id is not None or self.chosen is not None or self.list_mode is not None:
            self._add_back()
        return {"content": "\n".join(lines), "view": self}

    def _top_options(self) -> list[LocOption]:
        """O topo (A2), nesta ordem e sem lista vazia: 🔎 busca, ⭐ minhas, ⭐ do servidor, 📌 meus filtros."""
        options: list[LocOption] = []
        if getattr(self.hub, "directory", None) is not None:
            options.append(LocOption(SEARCH_ID, self.tr("jukebox.radio.picker.search"), "", 0))
        for value, key, items in (
            (MINE_ID, "radio.favorites.picker.mine", self._mine),
            (FAVORITES_ID, "radio.local.favorites", self._favorites),
            (FILTERS_ID, "radio.favorites.picker.filters", self._filters),
        ):
            if items:
                options.append(LocOption(value, f"{self.tr(key)} · {len(items)}", "", len(items)))
        return options

    def _level_options(self, node: Optional[LocNode]) -> list[LocOption]:
        options = self.tree.children(self.node_id, self.locale, guild_locale=self.guild_locale)
        if node is None:
            options = [*self._top_options(), *options]
        return options[:MAX_OPTIONS]

    async def _load_favorites(self) -> list[Any]:
        try:
            return await self.hub.favorites.list(self.guild_id)
        except RadioUnavailable:
            return []

    def _list_hits(self) -> tuple[Hit, ...]:
        """As favoritas como uma lista de estações; só uuid e nome (o resto é refeito pelo diretório quando a estação é escolhida)."""
        if self.list_mode == LIST_SEARCH:
            return self.hits
        items = self._favorites if self.list_mode == LIST_FAV else self._mine
        return tuple(Hit(Station(f.uuid, f.name, "", "", 0, ""), 0, False, "favorite") for f in (items or ()))

    def _favorite_hits(self) -> tuple[Hit, ...]:
        return self._list_hits()

    def _add_level_select(self, options: list[LocOption]) -> None:
        if not options:
            return
        self._offered = {option.id for option in options}
        select = disnake.ui.Select(
            placeholder=fit(self._prompt(self.tree.node(self.node_id) if self.node_id else None), 150),
            options=[
                disnake.SelectOption(
                    label=fit(option.label, LABEL_LIMIT),
                    value=option.id,
                    description=fit(option.description, LABEL_LIMIT) or None,
                )
                for option in options[:MAX_OPTIONS]
            ],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_level
        self.add_item(select)

    def _add_filter_select(self) -> None:
        nodes = [n for n in (self._filters or ()) if self.tree.node(n) is not None][:MAX_OPTIONS]
        if not nodes:
            return
        self._offered = set(nodes)
        select = disnake.ui.Select(
            placeholder=fit(self.tr("radio.favorites.picker.filters"), 150),
            options=[
                disnake.SelectOption(label=fit(filter_label(self.tree, n, self.locale), LABEL_LIMIT), value=n)
                for n in nodes
            ],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_filter
        self.add_item(select)

    async def _add_station_select(self, node: Optional[LocNode]) -> list[str]:
        """O último nível: a lista de estações do local, das favoritas ou da busca (24 por página) e os botões de página. Devolve as linhas de aviso."""
        notes: list[str] = []
        if self.list_mode is not None:
            self.hits, self.partial = self._list_hits(), False
        elif not self.hits and not self.partial and node is not None:
            await self._load_hits(node)
        if self.partial:
            notes.append(self.tr("radio.local.partial"))
        pages = max(1, -(-len(self.hits) // PAGE_SIZE))
        self.page = min(self.page, pages - 1)
        shown = self.hits[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]
        if not shown:
            notes.append(self.tr(self._no_results_key))
            return notes
        self._offered = {hit.station.uuid for hit in shown}
        select = disnake.ui.Select(
            placeholder=fit(self.tr("radio.local.pick_station"), 150),
            options=[station_option(self.hub, self.tr, hit.station, hit.check_ok) for hit in shown],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_station
        self.add_item(select)
        if pages > 1:
            notes.append(self.tr("radio.local.page", page=self.page + 1, pages=pages))
            previous = disnake.ui.Button(label="◀", disabled=self.page == 0, row=1)
            following = disnake.ui.Button(label="▶", disabled=self.page >= pages - 1, row=1)
            previous.callback, following.callback = self._on_previous, self._on_next
            self.add_item(previous)
            self.add_item(following)
        return notes

    def _add_back(self) -> None:
        back = disnake.ui.Button(label=fit(self.tr("radio.local.back"), 80), emoji="◀️", row=2)
        back.callback = self._on_back
        self.add_item(back)

    def _add_confirm(self) -> None:
        confirm = disnake.ui.Button(label=fit(self._confirm_label, 80), style=disnake.ButtonStyle.success, row=1)
        confirm.callback = self._on_confirm
        self.add_item(confirm)

    def _add_stars(self) -> None:
        """B1: ⭐ minhas (todos) e ⭐ do servidor (só quem gerencia, e o store confere de novo no clique). Já marcada vira "Remover…"."""
        uuid = self.chosen.uuid if self.chosen is not None else None
        mine = any(f.uuid == uuid for f in self._mine or ())
        star = disnake.ui.Button(
            label=fit(self.tr("radio.favorites.picker.mine_remove" if mine else "radio.favorites.picker.mine"), 80),
            style=disnake.ButtonStyle.secondary,
            row=1,
        )
        star.callback = self._on_star_mine
        self.add_item(star)
        if not self._manager:
            return
        server = any(f.uuid == uuid for f in self._favorites or ())
        guild_star = disnake.ui.Button(
            label=fit(self.tr("radio.favorites.picker.server_remove" if server else "radio.local.favorites"), 80),
            style=disnake.ButtonStyle.secondary,
            row=1,
        )
        guild_star.callback = self._on_star_server
        self.add_item(guild_star)

    def _add_filter_toggle(self) -> None:
        """B1: "📌 Salvar filtro" em qualquer nível com local escolhido (só com a árvore e o store pessoal)."""
        if self.node_id is None or getattr(self.hub, "user_favorites", None) is None:
            return
        saved = self.node_id in (self._filters or ())
        button = disnake.ui.Button(
            label=fit(
                self.tr("radio.favorites.picker.filter_remove" if saved else "radio.favorites.picker.filter_save"), 80
            ),
            style=disnake.ButtonStyle.secondary,
            row=2,
        )
        button.callback = self._on_filter_toggle
        self.add_item(button)

    async def _load_hits(self, node: LocNode) -> None:
        try:
            if await self.hub.premium.get(self.guild_id) is None:
                self.hits, self.partial = (), False
                return
            result = await self.hub.directory.region(node, key=self.guild_id)
        except RadioUnavailable:
            self.hits, self.partial = (), True
            return
        self.hits, self.partial = result.hits, result.partial

    # ------------------------------------------------------------------ cliques

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:  # type: ignore[override]
        """Só quem abriu mexe, e a permissão é conferida de novo a cada clique (ela pode ter mudado nos 5 minutos)."""
        if interaction.author.id != self.author_id or not self._check(interaction):
            await interaction.response.send_message(self.tr(self._deny_key), ephemeral=True)
            return False
        self._manager = manages(interaction)
        return True

    async def _redraw(self, interaction: Any, note: str = "", *, deferred: bool = False) -> None:
        if not deferred:  # a lista de estações pode esperar fichas do diretório por alguns segundos
            await interaction.response.defer()
        payload = await self.render()
        if note:
            payload["content"] = f"{payload['content']}\n{note}"
        await interaction.edit_original_message(**payload)

    def _selected(self, interaction: disnake.MessageInteraction) -> Optional[str]:
        values = getattr(interaction.data, "values", None) or []
        value = values[0] if len(values) == 1 else None
        return value if isinstance(value, str) and value in self._offered else None

    async def _expired(self, interaction: disnake.MessageInteraction) -> None:
        await self._redraw(interaction, self.tr("radio.local.expired"))

    def _open_list(self, mode: Optional[str]) -> None:
        self.node_id, self.page, self.hits, self.partial, self.chosen = None, 0, (), False, None
        self.list_mode = mode

    async def _on_level(self, interaction: disnake.MessageInteraction) -> None:
        picked = self._selected(interaction)
        if picked == SEARCH_ID:
            await interaction.response.send_modal(SearchModal(self.tr, self._on_search))
            return
        if picked in _LIST_OF:
            self._open_list(_LIST_OF[picked])
            await self._redraw(interaction)
            return
        node = self.tree.node(picked) if picked else None
        if node is None:
            await self._expired(interaction)
            return
        self._open_list(None)
        self.node_id = node.id
        await self._redraw(interaction)

    async def _on_filter(self, interaction: disnake.MessageInteraction) -> None:
        """Um dos MEUS filtros: abre o local dele como um clique na árvore (o id é conferido contra os mostrados e contra a árvore)."""
        picked = self._selected(interaction)
        node = self.tree.node(picked) if picked else None
        if node is None:
            await self._expired(interaction)
            return
        self._open_list(None)
        self.node_id = node.id
        await self._redraw(interaction)

    async def _on_search(self, interaction: disnake.ModalInteraction, term: str) -> None:
        """O envio do modal (A3): só o autor; balde do servidor; até 24 estações; sem resultado, cheio ou fora do ar viram aviso no topo."""
        if interaction.author.id != self.author_id or not self._check(interaction):
            await interaction.response.send_message(self.tr(self._deny_key), ephemeral=True)
            return
        await interaction.response.defer(with_message=False)
        note = ""
        stations: list[Station] = []
        try:
            if await self.hub.premium.get(self.guild_id) is not None:
                result = await self.hub.directory.search_result(term, key=self.guild_id)
                if result.reason == REASON_LIMITED:
                    note = self.tr("jukebox.radio.search.limited")
                elif result.reason == REASON_UNAVAILABLE:
                    note = self.tr("radio.owner.unavailable")
                else:
                    stations = list(result.stations)[:SEARCH_RESULTS_MAX]
        except RadioUnavailable:
            note = self.tr("radio.owner.unavailable")
        except Exception as error:  # noqa: BLE001 - o envio não pode ficar sem resposta; só o tipo vai ao log (R-12)
            logger.warning("Busca por nome do seletor falhou (%s)", type(error).__name__)
            note = self.tr("radio.owner.unavailable")
        if not note and not stations:
            note = self.tr("jukebox.radio.search.empty")
        if stations:
            self._open_list(LIST_SEARCH)
            self.hits = tuple(Hit(station, 0, False, "name") for station in stations)
        await self._redraw(interaction, note, deferred=True)

    async def _on_station(self, interaction: disnake.MessageInteraction) -> None:
        picked = self._selected(interaction)
        hit = next((h for h in self.hits if h.station.uuid == picked), None) if picked else None
        if hit is None:
            await self._expired(interaction)
            return
        if self.hub.health.unavailable(hit.station.uuid):
            await self._redraw(interaction, self.tr("radio.refused.station_down"))
            return
        self.chosen = hit.station
        await self._redraw(interaction)

    async def _on_previous(self, interaction: disnake.MessageInteraction) -> None:
        self.page = max(0, self.page - 1)
        await self._redraw(interaction)

    async def _on_next(self, interaction: disnake.MessageInteraction) -> None:
        self.page += 1
        await self._redraw(interaction)

    async def _on_back(self, interaction: disnake.MessageInteraction) -> None:
        if self.chosen is not None:
            self.chosen = None
        elif self.list_mode is not None:
            self.list_mode, self.page, self.hits = None, 0, ()
        else:
            node = self.tree.node(self.node_id) if self.node_id else None
            self.node_id = node.parent if node is not None else None
            self.page, self.hits, self.partial = 0, (), False
        await self._redraw(interaction)

    # ------------------------------------------------------------------ estrelas e filtros (B1)

    async def _on_star_mine(self, interaction: disnake.MessageInteraction) -> None:
        """⭐ minhas: o uuid vem da escolha guardada, o nome é relido pelo diretório no store (s6); a identidade, da interação."""
        station = self.chosen
        if station is None:
            await self._expired(interaction)
            return
        await interaction.response.defer()
        try:
            result = await self.hub.user_favorites.toggle_station(interaction.author.id, station.uuid)
        except FavoriteNotFound:
            note = self.tr("radio.live.not_found")
        except RadioUnavailable:
            note = self.tr("radio.owner.unavailable")
        else:
            name = stored_text(station.name, NAME_LIMIT)
            note = {
                Toggled.ADDED: self.tr("radio.favorites.mine.added", station=name),
                Toggled.REMOVED: self.tr("radio.favorites.mine.removed", station=name),
            }.get(result) or self.tr("radio.favorites.mine.full", max=USER_LIVE_FAVORITES_MAX)
        self._mine = None
        await self._redraw(interaction, note, deferred=True)

    async def _on_star_server(self, interaction: disnake.MessageInteraction) -> None:
        """⭐ do servidor: o store confere Gerenciar Servidor DO ATOR (``can_manage_guild``) em cada gravação [m4]; a tela só esconde o botão."""
        station = self.chosen
        if station is None:
            await self._expired(interaction)
            return
        await interaction.response.defer()
        favorites = self.hub.favorites
        name = stored_text(station.name, NAME_LIMIT)
        try:
            if any(f.uuid == station.uuid for f in self._favorites or ()):
                await favorites.remove(self.guild_id, interaction.author.id, station.uuid)
                note = self.tr("radio.favorites.picker.server_removed", station=name)
            else:
                result = await favorites.add_live(
                    self.guild_id, interaction.author.id, station.uuid, self.hub.directory.by_uuid_strict
                )
                note = (
                    self.tr("radio.panel.favorite_full", max=MAX_FAVORITES)
                    if result == AddResult.FULL
                    else self.tr("radio.favorites.picker.server_added", station=name)
                )
        except FavoriteForbidden:
            note = self.tr("radio.refused.no_manage_guild")
        except FavoriteNotFound:
            note = self.tr("radio.live.not_found")
        except RadioUnavailable:
            note = self.tr("radio.owner.unavailable")
        self._favorites = None
        await self._redraw(interaction, note, deferred=True)

    async def _on_filter_toggle(self, interaction: disnake.MessageInteraction) -> None:
        """📌 salvar/remover o local de agora; o store valida o id contra a árvore (nunca grava nó não validado)."""
        node_id = self.node_id
        if node_id is None:
            await self._expired(interaction)
            return
        await interaction.response.defer()
        try:
            result = await self.hub.user_favorites.toggle_filter(interaction.author.id, node_id)
        except FavoriteNotFound:
            note = self.tr("radio.local.expired")
        except RadioUnavailable:
            note = self.tr("radio.owner.unavailable")
        else:
            label = filter_label(self.tree, node_id, self.locale)
            note = {
                Toggled.ADDED: self.tr("radio.favorites.picker.filter_saved", name=label),
                Toggled.REMOVED: self.tr("radio.favorites.picker.filter_removed", name=label),
            }.get(result) or self.tr("radio.favorites.filters.full", max=USER_FILTERS_MAX)
        self._filters = None
        await self._redraw(interaction, note, deferred=True)

    # ------------------------------------------------------------------ confirmar

    async def _on_confirm(self, interaction: disnake.MessageInteraction) -> None:
        station = self.chosen
        if station is None:
            await self._expired(interaction)
            return
        await interaction.response.defer()
        try:
            if self.list_mode in (LIST_FAV, LIST_MINE):
                # a favorita só guarda uuid e nome: a estação de verdade vem do diretório, que a confere de novo
                station = await self.hub.directory.by_uuid(station.uuid)
                if station is None:
                    raise GenericError(self.tr("radio.live.not_found"))
            station_list = tuple(h.station.uuid for h in self.hits)[:LIST_MAX] if len(self.hits) >= 2 else ()
            text = await self._apply(interaction, station, station_list)
        except GenericError as error:
            text = error.text
        except RadioUnavailable:
            text = self.tr("radio.owner.unavailable")
        except Exception as error:  # noqa: BLE001 - o clique não pode ficar sem resposta; só o tipo vai ao log (R-12)
            logger.warning("Seletor de estação falhou (%s)", type(error).__name__)
            text = self.tr("radio.owner.unavailable")
        self.stop()
        await interaction.edit_original_message(
            content=text, view=None, allowed_mentions=disnake.AllowedMentions.none()
        )


class VolumePicker(disnake.ui.View):
    """O volume da rádio ao vivo: 10 % a 100 %, só para quem abriu, 120 s. O ``apply`` grava e aplica; o texto de volta é o do painel."""

    def __init__(
        self,
        tr: Translator,
        *,
        author_id: int,
        current: int,
        apply: Callable[[disnake.MessageInteraction, int], Awaitable[str]],
        check: Callable[[disnake.MessageInteraction], bool],
    ) -> None:
        super().__init__(timeout=120.0)
        self.tr, self.author_id, self._apply, self._check = tr, author_id, apply, check
        self._levels = tuple(range(10, 101, 10))
        select = disnake.ui.Select(
            placeholder=fit(tr("radio.panel.volume.placeholder"), 150),
            options=[
                disnake.SelectOption(label=f"{level}%", value=str(level), default=level == current)
                for level in self._levels
            ],
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_pick
        self.add_item(select)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:  # type: ignore[override]
        if interaction.author.id != self.author_id or not self._check(interaction):
            await interaction.response.send_message(self.tr("radio.panel.call_only"), ephemeral=True)
            return False
        return True

    async def _on_pick(self, interaction: disnake.MessageInteraction) -> None:
        values = getattr(interaction.data, "values", None) or []
        level = int(values[0]) if len(values) == 1 and str(values[0]).isdecimal() else None
        await interaction.response.defer()
        if level not in self._levels:
            text = self.tr("radio.local.expired")
        else:
            try:
                text = await self._apply(interaction, level)
            except GenericError as error:
                text = error.text
            except RadioUnavailable:
                text = self.tr("radio.owner.unavailable")
        self.stop()
        await interaction.edit_original_message(content=text, view=None)
