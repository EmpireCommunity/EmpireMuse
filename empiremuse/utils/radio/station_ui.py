"""A interface das estações do servidor no Discord (item 8, R5, contrato §5): criar e editar (``StationEditor``), apagar (``ConfirmDelete``) e listar.

Tudo é efêmero, só para quem abriu, com 300 s de vida; o estado fica na ``disnake.ui.View`` em memória e NADA dele vai em ``custom_id`` (R-6). ``manage_guild`` é
conferido de novo a cada clique (a permissão pode mudar nos 5 minutos). A gravação é a MESMA do painel web (``hub.stations``): as regras, o teto de 10, o nome único
e o compare-and-set em ``rev`` não são daqui.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.radio_genres import GROUPS
from empiremuse.rules.radio_station import (
    ARTIST_NAME_MAX,
    KIND_ARTISTS,
    KIND_GENRE,
    KIND_PLAYLISTS,
    MAX_ARTISTS,
    MAX_PLAYLISTS,
    NAME_MAX,
    STATION_KINDS,
    CustomStation,
)
from empiremuse.rules.text import external_text
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.stations import StationConflict, StationInUse, StationInvalid, StationNotFound

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

TIMEOUT_S = 300.0
CONFIRM_TIMEOUT_S = 60.0
SELECT_MAX = 25
SHOWN = 80


class _TextModal(disnake.ui.Modal):
    """Um modal de um campo só; o texto digitado volta pela função ``on_submit`` (que responde a interação)."""

    def __init__(
        self,
        *,
        title: str,
        label: str,
        value: str,
        max_length: int,
        long: bool,
        on_submit: Callable[[disnake.ModalInteraction, str], Awaitable[None]],
    ) -> None:
        self._on_submit = on_submit
        super().__init__(
            title=fit(title, 45),
            custom_id="radio_station_modal",
            timeout=TIMEOUT_S,
            components=[
                disnake.ui.TextInput(
                    label=fit(label, 45),
                    custom_id="text",
                    value=value or None,
                    max_length=max_length,
                    required=True,
                    style=disnake.TextInputStyle.paragraph if long else disnake.TextInputStyle.short,
                )
            ],
        )

    async def callback(self, interaction: disnake.ModalInteraction, /) -> None:
        await self._on_submit(interaction, str(interaction.text_values.get("text", "")))


def error_text(tr: Translator, error: Exception) -> str:
    """O texto da recusa de gravar uma estação, no idioma do servidor (nunca texto do banco nem rastro)."""
    if isinstance(error, StationInvalid):
        return tr(
            f"radio.station.err.{error.error.code}", limit=error.error.limit, value=external_text(error.error.value, 60)
        )
    if isinstance(error, StationInUse):
        return tr("radio.station.err.in_use", radios=len(error.radios))
    if isinstance(error, StationConflict):
        return tr("radio.station.err.conflict")
    if isinstance(error, StationNotFound):
        return tr("radio.station.err.not_found")
    return tr("radio.owner.unavailable")


def summary(tr: Translator, station: CustomStation) -> str:
    """Uma linha para a lista: o gênero (nome traduzido), os primeiros artistas (⚠️ os desconhecidos) ou as playlists."""
    if station.kind == KIND_GENRE:
        return tr(f"radio.genre.{station.source.get('genre')}")
    names = station.source.get("artists" if station.kind == KIND_ARTISTS else "playlists", ())
    shown = [(f"⚠️ {n}" if n in station.unknown else n) for n in names[:3]]
    more = f" +{len(names) - 3}" if len(names) > 3 else ""
    return external_text(", ".join(shown), SHOWN) + more


class StationEditor(disnake.ui.View):
    """Cria ou edita UMA estação: tipo, fonte (gênero em dois níveis, artistas num modal ou playlists num select) e nome. Só salva quando a fonte está completa."""

    def __init__(
        self,
        hub: RadioHub,
        tr: Translator,
        *,
        author_id: int,
        guild_id: int,
        playlists: Sequence[str],
        station: Optional[CustomStation],
        name: str,
        kind: str,
        can_manage: Callable[[disnake.Interaction], bool],
        on_saved: Optional[Callable[[CustomStation], Awaitable[None]]] = None,
    ) -> None:
        super().__init__(timeout=TIMEOUT_S)
        self.hub, self.tr, self.author_id, self.guild_id = hub, tr, author_id, guild_id
        self.playlists = list(playlists)[:SELECT_MAX]
        self.editing = station
        self.name, self.kind = name, kind if kind in STATION_KINDS else KIND_GENRE
        self.group = GROUPS[0]
        self.genre: Optional[str] = None
        self.artists: list[str] = []
        self.chosen_playlists: list[str] = []
        if station is not None:
            self._load(station)
        self._can_manage, self._on_saved = can_manage, on_saved

    def _load(self, station: CustomStation) -> None:
        self.kind = station.kind
        if station.kind == KIND_GENRE:
            self.genre = station.source.get("genre")
            known = next((g for g in self.hub.stations.genres() if g.id == self.genre), None)
            self.group = known.group if known else GROUPS[0]
        elif station.kind == KIND_ARTISTS:
            self.artists = list(station.source.get("artists", ()))
        else:
            self.chosen_playlists = [p for p in station.source.get("playlists", ()) if p in self.playlists]

    # ------------------------------------------------------------------ o que falta e o que vai gravar

    def _source(self) -> Optional[dict[str, Any]]:
        if self.kind == KIND_GENRE and self.genre:
            return {"genre": self.genre}
        if self.kind == KIND_ARTISTS and self.artists:
            return {"artists": self.artists}
        if self.kind == KIND_PLAYLISTS and self.chosen_playlists:
            return {"playlists": self.chosen_playlists}
        return None

    def _source_text(self) -> str:
        if self.kind == KIND_GENRE:
            return self.tr(f"radio.genre.{self.genre}") if self.genre else self.tr("radio.station.ui.pick_genre")
        items = self.artists if self.kind == KIND_ARTISTS else self.chosen_playlists
        if not items:
            return self.tr(
                "radio.station.ui.pick_artists" if self.kind == KIND_ARTISTS else "radio.station.ui.pick_playlists"
            )
        return external_text(", ".join(items), SHOWN * 2)

    # ------------------------------------------------------------------ a mensagem de agora

    def render(self) -> dict[str, Any]:
        self.clear_items()
        title = "radio.station.ui.edit_title" if self.editing else "radio.station.ui.create_title"
        lines = [
            self.tr(title),
            self.tr("radio.station.ui.name", name=external_text(self.name, NAME_MAX)),
            self.tr("radio.station.ui.kind", kind=self.tr(f"radio.station.kind.{self.kind}")),
            self.tr("radio.station.ui.source", source=self._source_text()),
        ]
        if self.kind != KIND_PLAYLISTS and not self.hub.lastfm.configured:
            lines.append(
                self.tr("radio.station.ui.lastfm_missing")
            )  # sem a chave do Last.fm a estação salva, mas não toca
        kind_select = disnake.ui.Select(
            placeholder=self.tr("radio.station.ui.kind_placeholder"),
            options=[
                disnake.SelectOption(label=self.tr(f"radio.station.kind.{k}"), value=k, default=k == self.kind)
                for k in STATION_KINDS
            ],
        )
        kind_select.callback = self._on_kind
        self.add_item(kind_select)
        if self.kind == KIND_GENRE:
            self._add_genre_selects()
        elif self.kind == KIND_ARTISTS:
            button = disnake.ui.Button(
                label=self.tr("radio.station.ui.artists_button"), custom_id="radio_station_artists"
            )
            button.callback = self._on_artists_button
            self.add_item(button)
        else:
            self._add_playlist_select(lines)
        name_button = disnake.ui.Button(label=self.tr("radio.station.ui.name_button"), custom_id="radio_station_name")
        name_button.callback = self._on_name_button
        save = disnake.ui.Button(
            label=self.tr("radio.station.ui.save"),
            custom_id="radio_station_save",
            style=disnake.ButtonStyle.success,
            disabled=self._source() is None,
        )
        save.callback = self._on_save
        cancel = disnake.ui.Button(label=self.tr("radio.station.ui.cancel"), custom_id="radio_station_cancel")
        cancel.callback = self._on_cancel
        for item in (name_button, save, cancel):
            self.add_item(item)
        return {"content": "\n".join(lines), "view": self}

    def _add_genre_selects(self) -> None:
        group = disnake.ui.Select(
            placeholder=self.tr("radio.station.ui.group_placeholder"),
            options=[
                disnake.SelectOption(label=self.tr(f"radio.genre.group.{g}"), value=g, default=g == self.group)
                for g in GROUPS
            ],
        )
        group.callback = self._on_group
        self.add_item(group)
        genres = [g for g in self.hub.stations.genres() if g.group == self.group][:SELECT_MAX]
        genre = disnake.ui.Select(
            placeholder=self.tr("radio.station.ui.genre_placeholder"),
            options=[
                disnake.SelectOption(
                    label=fit(self.tr(f"radio.genre.{g.id}"), 100), value=g.id, default=g.id == self.genre
                )
                for g in genres
            ],
        )
        genre.callback = self._on_genre
        self.add_item(genre)

    def _add_playlist_select(self, lines: list[str]) -> None:
        if not self.playlists:
            lines.append(self.tr("radio.station.ui.no_playlists"))
            return
        select = disnake.ui.Select(
            placeholder=self.tr("radio.station.ui.playlists_placeholder"),
            min_values=1,
            max_values=min(MAX_PLAYLISTS, len(self.playlists)),
            options=[
                disnake.SelectOption(label=fit(p, 100), value=p, default=p in self.chosen_playlists)
                for p in self.playlists
            ],
        )
        select.callback = self._on_playlists
        self.add_item(select)

    # ------------------------------------------------------------------ cliques

    async def interaction_check(self, interaction: disnake.Interaction) -> bool:  # type: ignore[override]
        if interaction.author.id != self.author_id or not self._can_manage(interaction):
            await interaction.response.send_message(self.tr("radio.refused.no_manage_guild"), ephemeral=True)
            return False
        return True

    @staticmethod
    def _values(interaction: disnake.MessageInteraction) -> list[str]:
        return [v for v in (getattr(interaction.data, "values", None) or []) if isinstance(v, str)]

    async def _redraw(self, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.edit_message(**self.render())

    async def _on_kind(self, interaction: disnake.MessageInteraction) -> None:
        values = self._values(interaction)
        if len(values) == 1 and values[0] in STATION_KINDS:
            self.kind = values[0]
        await self._redraw(interaction)

    async def _on_group(self, interaction: disnake.MessageInteraction) -> None:
        values = self._values(interaction)
        if len(values) == 1 and values[0] in GROUPS:
            self.group, self.genre = values[0], None
        await self._redraw(interaction)

    async def _on_genre(self, interaction: disnake.MessageInteraction) -> None:
        values = self._values(interaction)
        offered = {g.id for g in self.hub.stations.genres() if g.group == self.group}
        if len(values) == 1 and values[0] in offered:  # só o que o select de agora ofereceu
            self.genre = values[0]
        await self._redraw(interaction)

    async def _on_playlists(self, interaction: disnake.MessageInteraction) -> None:
        self.chosen_playlists = [v for v in self._values(interaction) if v in self.playlists][:MAX_PLAYLISTS]
        await self._redraw(interaction)

    async def _on_artists_button(self, interaction: disnake.MessageInteraction) -> None:
        async def submit(modal: disnake.ModalInteraction, text: str) -> None:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            self.artists = lines[:MAX_ARTISTS]
            await modal.response.edit_message(**self.render())

        await interaction.response.send_modal(
            _TextModal(
                title=self.tr("radio.station.ui.artists_title"),
                label=self.tr("radio.station.ui.artists_label", max=MAX_ARTISTS),
                value="\n".join(self.artists),
                max_length=MAX_ARTISTS * (ARTIST_NAME_MAX + 1),
                long=True,
                on_submit=submit,
            )
        )

    async def _on_name_button(self, interaction: disnake.MessageInteraction) -> None:
        async def submit(modal: disnake.ModalInteraction, text: str) -> None:
            self.name = " ".join(text.split())
            await modal.response.edit_message(**self.render())

        await interaction.response.send_modal(
            _TextModal(
                title=self.tr("radio.station.ui.name_title"),
                label=self.tr("radio.station.ui.name_label", max=NAME_MAX),
                value=self.name,
                max_length=NAME_MAX,
                long=False,
                on_submit=submit,
            )
        )

    async def _on_cancel(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        await interaction.response.edit_message(content=self.tr("radio.station.ui.cancelled"), view=None)

    async def _on_save(self, interaction: disnake.MessageInteraction) -> None:
        source = self._source()
        if source is None:
            await self._redraw(interaction)
            return
        await interaction.response.defer()
        raw = {"name": self.name, "kind": self.kind, "source": source}
        try:
            if self.editing is None:
                saved = await self.hub.stations.create(
                    self.guild_id, raw, playlists=self.playlists, updated_by=str(self.author_id)
                )
            else:
                saved = await self.hub.stations.update(
                    self.guild_id,
                    self.editing.id,
                    raw,
                    expected_rev=self.editing.rev,
                    playlists=self.playlists,
                    updated_by=str(self.author_id),
                )
        except (StationInvalid, StationConflict, StationNotFound, RadioUnavailable) as error:
            await interaction.edit_original_message(content=error_text(self.tr, error), view=self)
            return
        self.stop()
        if self._on_saved is not None:
            await self._on_saved(saved)
        await interaction.edit_original_message(
            content=self.tr("radio.station.ui.saved", name=external_text(saved.name, NAME_MAX)), view=None
        )


class ConfirmDelete(disnake.ui.View):
    """Apagar uma estação: um botão de confirmar, 60 s, só para quem pediu. O apagar em si recusa estação em uso."""

    def __init__(
        self,
        hub: RadioHub,
        tr: Translator,
        *,
        author_id: int,
        station: CustomStation,
        can_manage: Callable[[disnake.Interaction], bool],
    ) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.hub, self.tr, self.author_id, self.station, self._can_manage = hub, tr, author_id, station, can_manage
        confirm = disnake.ui.Button(
            label=self.tr("radio.station.ui.delete_confirm"),
            custom_id="radio_station_delete_confirm",
            style=disnake.ButtonStyle.danger,
        )
        confirm.callback = self._on_confirm
        self.add_item(confirm)

    async def interaction_check(self, interaction: disnake.Interaction) -> bool:  # type: ignore[override]
        if interaction.author.id != self.author_id or not self._can_manage(interaction):
            await interaction.response.send_message(self.tr("radio.refused.no_manage_guild"), ephemeral=True)
            return False
        return True

    async def _on_confirm(self, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.defer()
        try:
            await self.hub.stations.delete(self.station.guild_id, self.station.id, expected_rev=self.station.rev)
        except StationInUse as error:
            using = ", ".join(f"<#{r}>" for r in await self._channels(error.radios))
            text = self.tr("radio.station.err.in_use_where", channels=using or "—")
        except (StationConflict, StationNotFound, RadioUnavailable) as error:
            text = error_text(self.tr, error)
        else:
            text = self.tr("radio.station.ui.deleted", name=external_text(self.station.name, NAME_MAX))
        self.stop()
        await interaction.edit_original_message(content=text, view=None)

    async def _channels(self, radio_ids: Sequence[str]) -> list[int]:
        found: list[int] = []
        for radio_id in radio_ids[:5]:
            radio = await self.hub.store.get(radio_id)
            if radio is not None:
                found.append(radio.channel_id)
        return found
