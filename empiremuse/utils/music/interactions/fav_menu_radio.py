# -*- coding: utf-8 -*-
"""Modo "Rádios favoritas" do ``/fav_manager`` (J3): as estações ao vivo e os filtros de local do próprio membro.

Só **Remover** e **Limpar** (o Tocar é da J2a e não deixa porta aqui). A lista pessoal e os filtros são dado pessoal
(contrato Jukebox §1.2, m7): só existem em mensagem efêmera. ``radio_private`` é o único portão: nenhum texto com a
lista é montado sem ele, e ele só vira ``True`` onde o menu já está numa mensagem efêmera (ou numa nova, aberta para
isso). A identidade vem sempre de ``inter.author``; nenhum ``custom_id`` nem valor de select decide de quem é a lista.
Nada aqui registra log com usuário e estação.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.interactions.common import ViewMode
from empiremuse.utils.others import CustomContext, paginator
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.user_favorites import USER_FILTERS_MAX, USER_LIVE_FAVORITES_MAX

# O Discord limita um select a 25 opções, o rótulo a 100 caracteres e o conteúdo da mensagem a 2.000.
LABEL_LIMIT = 100
PLACEHOLDER_LIMIT = 150
TEXT_NAME_LIMIT = 40
TEXT_LIST_MAX_SIZE = 800

RadioOperation = Callable[[Any, int], Awaitable[bool]]


class FavMenuRadioMixin:
    """Estado, componentes, texto e callbacks do modo rádio. Compartilha a view com os outros modos do menu."""

    def init_radio_state(self, *, private: bool) -> None:
        self.radio_private = private
        self.radio_stations: list[Any] = []
        self.radio_filters: list[tuple[str, str]] = []
        self.radio_unavailable = False
        self.current_station: str | None = None
        self.current_filter: str | None = None

    def radio_available(self) -> bool:
        """O modo existe em comando de barra (mensagem que pode ser efêmera) e com as rádios ligadas neste processo."""
        return not isinstance(self.ctx, CustomContext) and getattr(self.bot.pool, "radio", None) is not None

    # ------------------------------------------------------------------ dados

    async def load_radio(self) -> None:
        """Lê as favoritas e os filtros de quem abriu o menu. Banco fora do ar vira ``radio_unavailable`` (tentar de novo)."""
        favorites = self.bot.pool.radio.user_favorites
        user = self.ctx.author.id
        try:
            stations = await favorites.stations(user)
            nodes = await favorites.filters(user)
        except RadioUnavailable:
            self.radio_unavailable = True
            self.radio_stations, self.radio_filters = [], []
            return
        self.radio_unavailable = False
        self.radio_stations = list(stations)
        self.radio_filters = [(node, self._filter_label(node)) for node in nodes]

    def _filter_label(self, node_id: str) -> str:
        """O caminho do local no idioma do menu (o documento só guarda o id do nó)."""
        tree = self.bot.pool.radio.locations
        locale = str(getattr(self.tr, "locale", "en-US"))
        steps = [tree.label(node, locale) for node in tree.path(node_id)] if tree is not None else []
        return external_text(" › ".join(steps) or node_id, LABEL_LIMIT, markdown=False)

    # ------------------------------------------------------------------ componentes

    def add_radio_components(self) -> None:
        if self.radio_unavailable:
            self._add_button(self.tr("radio.favorites.retry.label"), "🔄", self.radio_retry_callback)
        else:
            self._add_radio_station_select()
            self._add_radio_filter_select()
            self._add_radio_buttons()
        self._add_button(self.tr("fav.close.label"), "❌", self.cancel_callback)

    def _add_radio_station_select(self) -> None:
        if not self.radio_stations:
            self.current_station = None
            return
        if self.current_station not in {s.uuid for s in self.radio_stations}:
            self.current_station = self.radio_stations[0].uuid
        options = [
            disnake.SelectOption(
                label=external_text(s.name, LABEL_LIMIT, markdown=False) or "—",
                value=s.uuid,
                emoji="📻",
                default=s.uuid == self.current_station,
            )
            for s in self.radio_stations[:USER_LIVE_FAVORITES_MAX]
        ]
        select = disnake.ui.Select(
            options=options,
            min_values=1,
            max_values=1,
            placeholder=fit(self.tr("radio.favorites.stations.placeholder"), PLACEHOLDER_LIMIT),
        )
        select.callback = self.radio_select_station_callback
        self.add_item(select)

    def _add_radio_filter_select(self) -> None:
        if not self.radio_filters:
            self.current_filter = None
            return
        if self.current_filter not in {node for node, _ in self.radio_filters}:
            self.current_filter = self.radio_filters[0][0]
        options = [
            disnake.SelectOption(label=label or "—", value=node, emoji="📌", default=node == self.current_filter)
            for node, label in self.radio_filters[:USER_FILTERS_MAX]
        ]
        select = disnake.ui.Select(
            options=options,
            min_values=1,
            max_values=1,
            placeholder=fit(self.tr("radio.favorites.filters.placeholder"), PLACEHOLDER_LIMIT),
        )
        select.callback = self.radio_select_filter_callback
        self.add_item(select)

    def _add_radio_buttons(self) -> None:
        tr = self.tr
        no_stations, no_filters = not self.radio_stations, not self.radio_filters
        add = self._add_button
        add(tr("radio.favorites.remove_station.label"), "♻️", self.radio_remove_station_callback, disabled=no_stations)
        add(tr("radio.favorites.clear_stations.label"), "🚮", self.radio_clear_stations_callback, disabled=no_stations)
        add(tr("radio.favorites.remove_filter.label"), "♻️", self.radio_remove_filter_callback, disabled=no_filters)
        add(tr("radio.favorites.clear_filters.label"), "🚮", self.radio_clear_filters_callback, disabled=no_filters)

    # ------------------------------------------------------------------ texto

    def radio_manager_txt(self) -> str:
        """O texto do modo. Sem ``radio_private`` a lista nunca é montada: o menu pode estar numa mensagem pública."""
        tr = self.tr
        if not self.radio_private:
            raise GenericError(tr("radio.favorites.private_only"))
        txt = tr("radio.favorites.heading")
        if self.radio_unavailable:
            return txt + tr("radio.owner.unavailable") + "\n"
        txt += tr("radio.favorites.stations.heading", count=len(self.radio_stations), max=USER_LIVE_FAVORITES_MAX)
        if self.radio_stations:
            lines = (
                f"> ` {n:02} ` 📻 {external_text(s.name, TEXT_NAME_LIMIT)}"
                for n, s in enumerate(self.radio_stations, 1)
            )
            txt += paginator("\n".join(lines), max_size=TEXT_LIST_MAX_SIZE)[0] + "\n"
        else:
            txt += tr("radio.favorites.stations.empty")
        txt += tr("radio.favorites.filters.heading", count=len(self.radio_filters), max=USER_FILTERS_MAX)
        if self.radio_filters:
            lines = (f"> ` {n:02} ` 📌 {label}" for n, (_, label) in enumerate(self.radio_filters, 1))
            txt += paginator("\n".join(lines), max_size=TEXT_LIST_MAX_SIZE)[0] + "\n"
        else:
            txt += tr("radio.favorites.filters.empty")
        return txt + tr("radio.favorites.private_note")

    # ------------------------------------------------------------------ callbacks

    async def radio_select_station_callback(self, inter: disnake.MessageInteraction) -> None:
        self.current_station = inter.values[0]
        await inter.response.defer()

    async def radio_select_filter_callback(self, inter: disnake.MessageInteraction) -> None:
        self.current_filter = inter.values[0]
        await inter.response.defer()

    async def radio_retry_callback(self, inter: disnake.MessageInteraction) -> None:
        await self._radio_guarded(inter, None, "", {})

    async def radio_remove_station_callback(self, inter: disnake.MessageInteraction) -> None:
        uuid = self.current_station
        if uuid is None:
            await inter.send(self.tr("fav.select_item"), ephemeral=True)
            return
        name = next((s.name for s in self.radio_stations if s.uuid == uuid), "")
        await self._radio_guarded(
            inter,
            lambda favorites, user: favorites.remove_station(user, uuid),
            "radio.favorites.log.station_removed",
            {"name": external_text(name, TEXT_NAME_LIMIT)},
        )

    async def radio_clear_stations_callback(self, inter: disnake.MessageInteraction) -> None:
        await self._radio_guarded(
            inter, lambda favorites, user: favorites.clear_stations(user), "radio.favorites.log.stations_cleared", {}
        )

    async def radio_remove_filter_callback(self, inter: disnake.MessageInteraction) -> None:
        node = self.current_filter
        if node is None:
            await inter.send(self.tr("fav.select_item"), ephemeral=True)
            return
        label = next((text for n, text in self.radio_filters if n == node), "")
        await self._radio_guarded(
            inter,
            lambda favorites, user: favorites.remove_filter(user, node),
            "radio.favorites.log.filter_removed",
            {"name": external_text(label, TEXT_NAME_LIMIT)},
        )

    async def radio_clear_filters_callback(self, inter: disnake.MessageInteraction) -> None:
        await self._radio_guarded(
            inter, lambda favorites, user: favorites.clear_filters(user), "radio.favorites.log.filters_cleared", {}
        )

    async def _radio_guarded(
        self, inter: disnake.MessageInteraction, operation: RadioOperation | None, log_key: str, fmt: dict[str, str]
    ) -> None:
        """Roda uma remoção (ou só relê, com ``operation=None``) para quem clicou e redesenha o menu.

        O portão ``radio_private`` é conferido de novo no clique. ``RadioUnavailable`` vira "tente de novo"; remover o
        que já não existe é idempotente e só avisa que nada mudou.
        """
        tr = self.tr
        if not self.radio_private or self.mode != ViewMode.radio_favorites:
            await inter.send(tr("radio.favorites.private_only"), ephemeral=True)
            return
        await inter.response.defer()
        self.log = ""
        if operation is not None:
            try:
                changed = await operation(self.bot.pool.radio.user_favorites, inter.author.id)
            except RadioUnavailable:
                self.log = tr("radio.owner.unavailable")
            else:
                self.log = tr(log_key, **fmt) if changed else tr("radio.favorites.nothing_changed")
        await self.load_radio()
        await self._redraw()
