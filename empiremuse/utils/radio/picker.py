"""O seletor de estação por local do Discord (item 8, R4): região → país → estado → cidade → estação, num select por vez, só para quem abriu.

Abre pelo ``/radio ligar`` sem ``playlist`` nem ``estacao`` (liga uma rádio ao vivo num canal) e pelo botão "Estações" do painel da rádio ao vivo (troca a
estação principal da rádio que já toca). O estado fica numa ``disnake.ui.View`` em memória (nó, página, lista de estações, escolha), com 300 s de vida e
só para o autor; NADA dele vai em ``custom_id`` (R-6). O select só traz ids que a árvore conhece e o que volta é conferido contra as opções mostradas
agora: valor fora delas é recusado ("expirou"), nunca trocado por outro. Premium e ``manage_guild`` são conferidos a cada passo que gasta recurso.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.radio_geo import MAX_OPTIONS
from empiremuse.rules.radio_stations import Station
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio.directory import Hit
from empiremuse.utils.radio.locations import LocationTree, LocNode, LocOption
from empiremuse.utils.radio.premium import RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

TIMEOUT_S = 300.0
PAGE_SIZE = MAX_OPTIONS
LABEL_LIMIT = 100
NAME_LIMIT = 80

# Aplica a escolha: recebe a interação do clique e a estação, devolve o texto final (ou levanta ``GenericError`` com o motivo traduzido).
Apply = Callable[[disnake.MessageInteraction, Station], Awaitable[str]]


def station_option(hub: RadioHub, tr: Translator, station: Station) -> disnake.SelectOption:
    """Uma estação no select: nome (R-11), codec/taxa/país, e ⛔ se o health check a marcou indisponível."""
    technical = f"{station.codec} {station.bitrate}k" if station.bitrate else station.codec
    detail = " · ".join(part for part in (technical, station.country) if part)
    label = external_text(station.name, NAME_LIMIT)
    if hub.health.unavailable(station.uuid):
        label = f"⛔ {label}"
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
    ) -> None:
        super().__init__(timeout=TIMEOUT_S)
        self.hub, self.tree, self.tr = hub, tree, tr
        self.author_id, self.guild_id = author_id, guild_id
        self.locale, self.guild_locale = locale, guild_locale
        self._apply, self._confirm_label, self._check = apply, confirm_label, check
        self.node_id: Optional[str] = None
        self.page = 0
        self.hits: tuple[Hit, ...] = ()
        self.partial = False
        self.chosen: Optional[Station] = None
        self._offered: set[str] = set()  # os valores que o select de agora aceita
        self.message_text = ""

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

    async def render(self) -> dict[str, Any]:
        """Monta ``content`` e a ``view`` do passo de agora (sempre uma nova leva de itens)."""
        self.clear_items()
        self._offered = set()
        node = self.tree.node(self.node_id) if self.node_id else None
        lines = [self.tr("radio.local.title")]
        if path := self._path_text():
            lines.append(path)
        if self.chosen is not None:
            lines.append(self.tr("radio.local.chosen", name=external_text(self.chosen.name, NAME_LIMIT)))
            self._add_confirm()
        elif node is None or not node.final:
            lines.append(self._prompt(node))
            self._add_level_select(self.tree.children(self.node_id, self.locale, guild_locale=self.guild_locale))
        else:
            lines.extend(await self._add_station_select(node))
        if self.node_id is not None or self.chosen is not None:
            self._add_back()
        return {"content": "\n".join(lines), "view": self}

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

    async def _add_station_select(self, node: LocNode) -> list[str]:
        """O último nível: a lista de estações do local (24 por página) e os botões de página. Devolve as linhas de aviso."""
        notes: list[str] = []
        if not self.hits and not self.partial:
            await self._load_hits(node)
        if self.partial:
            notes.append(self.tr("radio.local.partial"))
        pages = max(1, -(-len(self.hits) // PAGE_SIZE))
        self.page = min(self.page, pages - 1)
        shown = self.hits[self.page * PAGE_SIZE : (self.page + 1) * PAGE_SIZE]
        if not shown:
            notes.append(self.tr("radio.local.no_results"))
            return notes
        self._offered = {hit.station.uuid for hit in shown}
        select = disnake.ui.Select(
            placeholder=fit(self.tr("radio.local.pick_station"), 150),
            options=[station_option(self.hub, self.tr, hit.station) for hit in shown],
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
            await interaction.response.send_message(self.tr("radio.refused.no_manage_guild"), ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: disnake.MessageInteraction, note: str = "") -> None:
        await interaction.response.defer()  # a lista de estações pode esperar fichas do diretório por alguns segundos
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

    async def _on_level(self, interaction: disnake.MessageInteraction) -> None:
        picked = self._selected(interaction)
        node = self.tree.node(picked) if picked else None
        if node is None:
            await self._expired(interaction)
            return
        self.node_id, self.page, self.hits, self.partial, self.chosen = node.id, 0, (), False, None
        await self._redraw(interaction)

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
        else:
            node = self.tree.node(self.node_id) if self.node_id else None
            self.node_id = node.parent if node is not None else None
            self.page, self.hits, self.partial = 0, (), False
        await self._redraw(interaction)

    async def _on_confirm(self, interaction: disnake.MessageInteraction) -> None:
        station = self.chosen
        if station is None:
            await self._expired(interaction)
            return
        await interaction.response.defer()
        try:
            text = await self._apply(interaction, station)
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
