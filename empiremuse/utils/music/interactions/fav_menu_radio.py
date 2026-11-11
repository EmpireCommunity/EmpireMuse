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
from empiremuse.utils.others import CustomContext
from empiremuse.utils.radio.custom_favorites import MAX_CUSTOM_FAVORITES
from empiremuse.utils.radio.favorite_refs import is_genre, ref_labels
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.user_favorites import USER_FILTERS_MAX, USER_LIVE_FAVORITES_MAX

# O Discord limita um select a 25 opções, o rótulo a 100 caracteres e o conteúdo da mensagem a 2.000.
LABEL_LIMIT = 100
PLACEHOLDER_LIMIT = 150
TEXT_NAME_LIMIT = 40
TEXT_LIST_MAX_SIZE = 800

RadioOperation = Callable[[Any, int], Awaitable[bool]]


def fit_lines(lines: list[str], budget: int) -> str:
    """As linhas que cabem em ``budget`` caracteres; o resto vira uma linha final ``> … +N``, nunca um corte calado.

    O cabeçalho diz ``total/máximo``: a tela não pode mostrar menos itens do que ele conta sem avisar. O ``+N`` não tem
    palavra de idioma. O select logo abaixo continua oferecendo todas as opções.
    """
    shown: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        shown.append(line)
        used += len(line) + 1
    hidden = len(lines) - len(shown)
    if hidden:
        shown.append(f"> … +{hidden}")
    return "\n".join(shown) + "\n"


class FavMenuRadioMixin:
    """Estado, componentes, texto e callbacks do modo rádio. Compartilha a view com os outros modos do menu."""

    def init_radio_state(self, *, private: bool) -> None:
        self.radio_private = private
        self.radio_stations: list[Any] = []
        self.radio_filters: list[tuple[str, str]] = []
        self.radio_refs: list[tuple[str, str]] = []  # ``custom``/``genre`` do membro NESTE servidor (J2a)
        self.current_ref: str | None = None
        self.radio_refs_page = False  # o Discord só dá 5 linhas: as ``custom``/``genre`` têm a própria página
        self.radio_refs_unavailable = False
        self.radio_unavailable = False
        self.current_station: str | None = None
        self.current_filter: str | None = None

    def radio_available(self) -> bool:
        """O modo existe em comando de barra (mensagem que pode ser efêmera) e com as rádios ligadas neste processo."""
        return not isinstance(self.ctx, CustomContext) and getattr(self.bot.pool, "radio", None) is not None

    # ------------------------------------------------------------------ dados

    async def load_radio(self) -> None:
        """Lê as listas pessoais; refs indisponíveis não descartam as estações ao vivo nem os filtros."""
        favorites = self.bot.pool.radio.user_favorites
        user = self.ctx.author.id
        try:
            stations = await favorites.stations(user)
            nodes = await favorites.filters(user)
        except RadioUnavailable:
            self.radio_unavailable = True
            self.radio_refs_unavailable = True
            self.current_ref = None
            self.radio_stations, self.radio_filters, self.radio_refs = [], [], []
            return
        self.radio_unavailable = False
        self.radio_stations = list(stations)
        self.radio_filters = [(node, self._filter_label(node)) for node in nodes]
        try:
            self.radio_refs = await self._load_refs(user)
        except RadioUnavailable:
            self.radio_refs_unavailable = True
            self.radio_refs = []
            self.current_ref = None
        else:
            self.radio_refs_unavailable = False

    async def _load_refs(self, user: int) -> list[tuple[str, str]]:
        """As ``custom``/``genre`` do membro no servidor do comando (sem servidor, como na DM, não há o que listar)."""
        guild_id = self._radio_guild_id()
        if guild_id is None:
            return []
        hub = self.bot.pool.radio
        return await ref_labels(hub, self.tr, guild_id, await hub.custom_favorites.list(guild_id, user))

    def _radio_guild_id(self) -> int | None:
        """O servidor do comando (``guild_id`` da interação; o ``guild`` como reserva); ``None`` na DM."""
        guild_id = getattr(self.ctx, "guild_id", None)
        if guild_id is None:
            guild_id = getattr(getattr(self.ctx, "guild", None), "id", None)
        return guild_id

    def _filter_label(self, node_id: str) -> str:
        """O caminho do local no idioma do menu (o documento só guarda o id do nó)."""
        tree = self.bot.pool.radio.locations
        locale = str(getattr(self.tr, "locale", "en-US"))
        steps = [tree.label(node, locale) for node in tree.path(node_id)] if tree is not None else []
        return external_text(" › ".join(steps) or node_id, LABEL_LIMIT, markdown=False)

    # ------------------------------------------------------------------ componentes

    def add_radio_components(self) -> None:
        if self.radio_unavailable:
            self._add_button(
                self.tr("radio.favorites.retry.label"),
                "🔄",
                self.radio_retry_callback,
                custom_id="user_fav_radio_retry",
            )
        elif self.radio_refs_page:
            if self.radio_refs_unavailable:
                self._add_button(
                    self.tr("radio.favorites.retry.label"),
                    "🔄",
                    self.radio_retry_callback,
                    custom_id="user_fav_radio_retry",
                )
            self._add_radio_ref_select()
            add = self._add_button
            no_refs = not self.radio_refs
            add(
                self.tr("radio.favorites.remove_ref.label"),
                "♻️",
                self.radio_remove_ref_callback,
                custom_id="user_fav_radio_remove_ref",
                disabled=no_refs,
            )
            add(
                self.tr("radio.favorites.clear_refs.label"),
                "🚮",
                self.radio_clear_refs_callback,
                custom_id="user_fav_radio_clear_refs",
                disabled=no_refs,
            )
            add(
                self.tr("radio.favorites.back.label"),
                "↩️",
                self.radio_toggle_refs_callback,
                custom_id="user_fav_radio_back",
            )
        else:
            self._add_radio_station_select()
            self._add_radio_filter_select()
            self._add_radio_buttons()
        self._add_button(self.tr("fav.close.label"), "❌", self.cancel_callback, custom_id="user_fav_radio_close")

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
            custom_id="user_fav_radio_station_select",
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
            custom_id="user_fav_radio_filter_select",
            placeholder=fit(self.tr("radio.favorites.filters.placeholder"), PLACEHOLDER_LIMIT),
        )
        select.callback = self.radio_select_filter_callback
        self.add_item(select)

    def _add_radio_ref_select(self) -> None:
        if not self.radio_refs:
            self.current_ref = None
            return
        if self.current_ref not in {ref for ref, _ in self.radio_refs}:
            self.current_ref = self.radio_refs[0][0]
        options = [
            disnake.SelectOption(
                label=label or "—",
                value=ref,
                emoji="🎼" if is_genre(ref) else "🎛️",
                default=ref == self.current_ref,
            )
            for ref, label in self.radio_refs[:MAX_CUSTOM_FAVORITES]
        ]
        select = disnake.ui.Select(
            options=options,
            min_values=1,
            max_values=1,
            custom_id="user_fav_radio_ref_select",
            placeholder=fit(self.tr("radio.favorites.refs.placeholder"), PLACEHOLDER_LIMIT),
        )
        select.callback = self.radio_select_ref_callback
        self.add_item(select)

    def _add_radio_buttons(self) -> None:
        tr = self.tr
        no_stations, no_filters = not self.radio_stations, not self.radio_filters
        add = self._add_button
        add(
            tr("radio.favorites.remove_station.label"),
            "♻️",
            self.radio_remove_station_callback,
            custom_id="user_fav_radio_remove_station",
            disabled=no_stations,
        )
        add(
            tr("radio.favorites.clear_stations.label"),
            "🚮",
            self.radio_clear_stations_callback,
            custom_id="user_fav_radio_clear_stations",
            disabled=no_stations,
        )
        add(
            tr("radio.favorites.remove_filter.label"),
            "♻️",
            self.radio_remove_filter_callback,
            custom_id="user_fav_radio_remove_filter",
            disabled=no_filters,
        )
        add(
            tr("radio.favorites.clear_filters.label"),
            "🚮",
            self.radio_clear_filters_callback,
            custom_id="user_fav_radio_clear_filters",
            disabled=no_filters,
        )
        if self._radio_guild_id() is not None:
            add(
                tr("radio.favorites.refs.label"),
                "🎼",
                self.radio_toggle_refs_callback,
                custom_id="user_fav_radio_open_refs",
            )

    # ------------------------------------------------------------------ texto

    def radio_manager_txt(self) -> str:
        """O texto do modo. Sem ``radio_private`` a lista nunca é montada: o menu pode estar numa mensagem pública."""
        tr = self.tr
        if not self.radio_private:
            raise GenericError(tr("radio.favorites.private_only"))
        txt = tr("radio.favorites.heading")
        if self.radio_unavailable:
            return txt + tr("radio.owner.unavailable") + "\n"
        if (
            self.radio_refs_page
        ):  # a página das ``custom``/``genre``: só elas, para a mensagem nunca passar dos 2.000 caracteres
            return txt + self._radio_refs_txt() + tr("radio.favorites.private_note")
        txt += tr("radio.favorites.stations.heading", total=len(self.radio_stations), max=USER_LIVE_FAVORITES_MAX)
        if self.radio_stations:
            lines = [
                f"> ` {n:02} ` 📻 {external_text(s.name, TEXT_NAME_LIMIT, markdown=False)}"
                for n, s in enumerate(self.radio_stations, 1)
            ]
            txt += fit_lines(lines, TEXT_LIST_MAX_SIZE)
        else:
            txt += tr("radio.favorites.stations.empty")
        txt += tr("radio.favorites.filters.heading", total=len(self.radio_filters), max=USER_FILTERS_MAX)
        if self.radio_filters:
            lines = [f"> ` {n:02} ` 📌 {label}" for n, (_, label) in enumerate(self.radio_filters, 1)]
            txt += fit_lines(lines, TEXT_LIST_MAX_SIZE)
        else:
            txt += tr("radio.favorites.filters.empty")
        return txt + tr("radio.favorites.private_note")

    def _radio_refs_txt(self) -> str:
        tr = self.tr
        txt = tr(
            "radio.favorites.refs.heading",
            total="—" if self.radio_refs_unavailable else len(self.radio_refs),
            max=MAX_CUSTOM_FAVORITES,
        )
        if self.radio_refs_unavailable:
            return txt + tr("radio.favorites.refs.unavailable")
        if not self.radio_refs:
            return txt + tr("radio.favorites.refs.empty")
        lines = [
            f"> ` {n:02} ` {'🎼' if is_genre(ref) else '🎛️'} {external_text(label, TEXT_NAME_LIMIT)}"
            for n, (ref, label) in enumerate(self.radio_refs, 1)
        ]
        return txt + fit_lines(lines, TEXT_LIST_MAX_SIZE)

    # ------------------------------------------------------------------ callbacks

    async def radio_select_station_callback(self, inter: disnake.MessageInteraction) -> None:
        self.current_station = inter.values[0]
        await inter.response.defer()

    async def radio_select_filter_callback(self, inter: disnake.MessageInteraction) -> None:
        self.current_filter = inter.values[0]
        await inter.response.defer()

    async def radio_toggle_refs_callback(self, inter: disnake.MessageInteraction) -> None:
        self.radio_refs_page = not self.radio_refs_page
        await self._radio_guarded(inter, None, "", {})

    async def radio_select_ref_callback(self, inter: disnake.MessageInteraction) -> None:
        self.current_ref = inter.values[0]
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
            {"name": external_text(name, TEXT_NAME_LIMIT, markdown=False)},
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
            {"name": external_text(label, TEXT_NAME_LIMIT, markdown=False)},
        )

    async def radio_clear_filters_callback(self, inter: disnake.MessageInteraction) -> None:
        await self._radio_guarded(
            inter, lambda favorites, user: favorites.clear_filters(user), "radio.favorites.log.filters_cleared", {}
        )

    async def radio_remove_ref_callback(self, inter: disnake.MessageInteraction) -> None:
        if self.radio_refs_unavailable:
            await self._radio_guarded(inter, None, "", {})
            return
        ref = self.current_ref
        guild_id = self._radio_guild_id()
        if ref is None or guild_id is None:
            await inter.send(self.tr("fav.select_item"), ephemeral=True)
            return
        label = next((text for r, text in self.radio_refs if r == ref), "")
        await self._radio_guarded(
            inter,
            lambda favorites, user: self.bot.pool.radio.custom_favorites.remove(guild_id, user, ref),
            "radio.favorites.log.ref_gone"
            if label == self.tr("radio.favorites.ref.missing")
            else "radio.favorites.log.ref_removed",
            {} if label == self.tr("radio.favorites.ref.missing") else {"name": external_text(label, TEXT_NAME_LIMIT)},
        )

    async def radio_clear_refs_callback(self, inter: disnake.MessageInteraction) -> None:
        if self.radio_refs_unavailable:
            await self._radio_guarded(inter, None, "", {})
            return
        guild_id = self._radio_guild_id()
        if guild_id is None:
            await inter.send(self.tr("fav.select_item"), ephemeral=True)
            return
        await self._radio_guarded(
            inter,
            lambda favorites, user: self.bot.pool.radio.custom_favorites.clear(guild_id, user),
            "radio.favorites.log.refs_cleared",
            {},
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
