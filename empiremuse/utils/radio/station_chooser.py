"""O seletor efêmero "🎙️ Rádios do Servidor" da Jukebox (T-434 J2b, A5): só monta a tela e repassa a escolha ao controlador.

Nível 1: as estações do servidor (até 10, as ⭐ de quem abriu primeiro, nome e tipo) e as duas entradas "🌎 Gêneros internacionais" e
"🇧🇷 Gêneros brasileiros"; cada uma abre o select dos gêneros prontos. Com a fila vazia, escolher já toca (P5); com músicas na fila (ou a
rádio ao vivo no ar), o aviso aparece antes, com "Tocar estação". A decisão (DJ, voto, recusa) é sempre do controlador, no clique
(``jukebox.request_station``); a tela só aceita uma ref que ela mesma ofereceu, e o controlador a relê no servidor.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Optional

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.radio_genres import GROUPS
from empiremuse.rules.radio_mode import VOTE_TTL_S
from empiremuse.rules.radio_station import genre_ref
from empiremuse.rules.text import external_text
from empiremuse.utils.music import jukebox_ui
from empiremuse.utils.radio.jukebox_errors import Refused

logger = logging.getLogger(__name__)

TIMEOUT_S = 300.0
STATIONS_SHOWN = 10  # MAX_SERVER_STATIONS
GROUP_PREFIX = "f:"
BACK_VALUE = "f:back"
GROUP_EMOJI = {"international": "🌎", "brazilian": "🇧🇷"}
NAME_LIMIT = 80
# recusas de UMA estação (sumiu do servidor ou está sem faixas): viram nota no mesmo seletor, que segue aberto para escolher outra
STAY_OPEN = frozenset({"jukebox.station.not_found", "jukebox.station.empty"})

Preview = Callable[
    [disnake.MessageInteraction], Awaitable[tuple[bool, bool, Optional[str]]]
]  # (fila, voto, rádio ao vivo no ar)


class StationChooser(disnake.ui.View):
    def __init__(
        self,
        *,
        jukebox: Any,
        tr: Callable[..., str],
        author_id: int,
        stations: Sequence[Any],
        favorites: Sequence[str],
        genre_groups: Sequence[tuple[str, Sequence[str]]],
        preview: Preview,
    ) -> None:
        super().__init__(timeout=TIMEOUT_S)
        self.jukebox, self.tr, self.author_id, self.preview = jukebox, tr, author_id, preview
        wanted = set(favorites)
        ordered = sorted(stations, key=lambda s: (s.id not in wanted, s.name_norm))
        self.stations = list(ordered)[:STATIONS_SHOWN]
        self.favorites = wanted
        self.groups = {group: list(ids) for group, ids in genre_groups if group in GROUPS}
        self.level: Optional[str] = None  # ``None`` = o primeiro nível; senão o grupo de gêneros aberto
        self.chosen: Optional[str] = None
        self.chosen_name = ""
        self._token = secrets.token_hex(6)  # o ``custom_id`` do efêmero; não autoriza nada
        self.message: Any = None  # a mensagem efêmera, para desativar os controles quando o seletor expira

    # ------------------------------------------------------------------ desenho

    def _station_options(self) -> list[disnake.SelectOption]:
        options = [
            disnake.SelectOption(
                label=fit(external_text(s.name, NAME_LIMIT, markdown=False) or "—", 100),
                value=s.id,
                emoji="⭐" if s.id in self.favorites else None,
                description=fit(self.tr(f"radio.station.kind.{s.kind}"), 100),
            )
            for s in self.stations
        ]
        for group in GROUPS:
            if self.groups.get(group):
                options.append(
                    disnake.SelectOption(
                        label=fit(self.tr(f"radio.genre.ready.{group}"), 100),
                        value=f"{GROUP_PREFIX}{group}",
                        emoji=GROUP_EMOJI.get(group),
                    )
                )
        return options

    def _genre_options(self) -> list[disnake.SelectOption]:
        options = [
            disnake.SelectOption(label=fit(self.tr("jukebox.station.picker.back"), 100), value=BACK_VALUE, emoji="◀️")
        ]
        for genre in self.groups.get(self.level or "", [])[:24]:
            options.append(
                disnake.SelectOption(label=fit(self.tr(f"radio.genre.{genre}"), 100), value=genre_ref(genre))
            )
        return options

    def offered(self) -> set[str]:
        """As refs que esta tela ofereceu: uma escolha fora delas é ignorada (o controlador ainda relê a estação no servidor)."""
        refs = {s.id for s in self.stations}
        for ids in self.groups.values():
            refs.update(genre_ref(g) for g in ids)
        return refs

    def render(self, note: str = "", *, confirm: bool = False) -> dict[str, Any]:
        self.clear_items()
        if confirm:
            play = disnake.ui.Button(
                label=fit(self.tr("jukebox.station.picker.confirm"), 80),
                style=disnake.ButtonStyle.success,
                custom_id=f"jb_station_go:{self._token}",
            )
            play.callback = self._on_confirm
            back = disnake.ui.Button(
                label=fit(self.tr("jukebox.station.picker.back"), 80),
                style=disnake.ButtonStyle.secondary,
                custom_id=f"jb_station_back:{self._token}",
            )
            back.callback = self._on_back
            self.add_item(play)
            self.add_item(back)
            return {"content": note, "view": self}
        options = self._genre_options() if self.level else self._station_options()
        select = disnake.ui.Select(
            placeholder=fit(self.tr("jukebox.station.picker.placeholder"), 150),
            options=options[:25] or [disnake.SelectOption(label="—", value=BACK_VALUE)],
            custom_id=f"jb_station:{self._token}",
        )
        select.callback = self._on_select
        self.add_item(select)
        return {"content": note or self.tr("jukebox.station.picker.title"), "view": self}

    # ------------------------------------------------------------------ cliques

    async def on_timeout(self) -> None:
        """Expirou: os controles ficam desativados na mensagem (nenhum clique cai num seletor que não responde mais)."""
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except Exception as error:  # noqa: BLE001 - o efêmero pode ter sido descartado; só o tipo vai ao log
            logger.warning("Seletor de estações expirado não editado (%s)", type(error).__name__)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:  # type: ignore[override]
        if interaction.author.id != self.author_id:
            await interaction.response.send_message(self.tr("jukebox.radio.leave.not_author"), ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: disnake.MessageInteraction) -> None:
        value = (interaction.values or [""])[0]
        if value == BACK_VALUE:
            self.level = None
            await interaction.response.edit_message(**self.render())
            return
        if value.startswith(GROUP_PREFIX) and value[len(GROUP_PREFIX) :] in self.groups:
            self.level = value[len(GROUP_PREFIX) :]
            await interaction.response.edit_message(**self.render())
            return
        if value not in self.offered():
            await interaction.response.edit_message(**self.render(self.tr("player.the_selected_item_was_not")))
            return
        self.chosen = value
        self.chosen_name = name = self._name_of(value)
        await interaction.response.defer()
        queued, votes, radio_name = await self.preview(interaction)
        warning = jukebox_ui.station_warning(self.tr, station_name=name, queued=queued, radio_name=radio_name)
        if not warning and not votes:
            # fila vazia e sem votação: escolher já toca (P5), com a ref DESTE clique (outro clique pode ter trocado ``chosen``)
            await self._play(interaction, value)
            return
        if votes:  # também com a estação ligada e a fila vazia: a votação nunca abre sem o aviso antes (§2.1)
            warning = "\n".join(filter(None, (warning, jukebox_ui.vote_warning(self.tr, VOTE_TTL_S))))
        await interaction.edit_original_message(**self.render(warning, confirm=True))

    def _name_of(self, ref: str) -> str:
        for station in self.stations:
            if station.id == ref:
                return station.name
        return self.tr(f"radio.genre.{ref.removeprefix('g:')}")

    async def _on_back(self, interaction: disnake.MessageInteraction) -> None:
        self.chosen = None
        await interaction.response.edit_message(**self.render())

    async def _on_confirm(self, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.defer()
        await self._play(interaction, self.chosen)

    async def _play(self, interaction: disnake.MessageInteraction, ref: Optional[str]) -> None:
        if ref is None:
            return
        try:
            key, fields = await self.jukebox.request_station(interaction, ref)
        except Refused as refused:
            key, fields = refused.key, refused.fields  # só a chave do controlador, nunca o texto de uma exceção
        except Exception as error:  # noqa: BLE001 - o clique não fica sem resposta; só o tipo vai ao log
            logger.warning("Estação da Jukebox não ligada (%s)", type(error).__name__)
            key, fields = "radio.owner.unavailable", {}
        if "station" in fields:
            fields = {**fields, "station": jukebox_ui.server_station_md(fields["station"])}
        if key in STAY_OPEN:
            await self._choose_again(interaction, key, fields)
            return
        self.stop()
        try:
            await interaction.edit_original_message(content=self.tr(key, **fields), view=None)
        except Exception as error:  # noqa: BLE001
            logger.warning("Resposta do seletor de estações não editada (%s)", type(error).__name__)

    async def _choose_again(self, interaction: disnake.MessageInteraction, key: str, fields: dict[str, Any]) -> None:
        """A recusa entra como nota no lugar do título e o select volta; a estação que sumiu sai da lista."""
        if key == "jukebox.station.not_found":
            self.stations = [station for station in self.stations if station.id != self.chosen]
        self.chosen = None
        try:
            await interaction.edit_original_message(**self.render(self.tr(key, **fields)))
        except Exception as error:  # noqa: BLE001
            logger.warning("Resposta do seletor de estações não editada (%s)", type(error).__name__)


async def open_server_stations(bot: Any, interaction: disnake.MessageInteraction) -> None:
    """O clique em "🎙️ Rádios do Servidor" (``jb:server``): premium sem cache, o select fixo volta à página 1 e o seletor abre efêmero."""
    from empiremuse.utils.music.checks import resolve_player
    from empiremuse.utils.music.guild_pin import load_guild_pin, replace_guild_pin
    from empiremuse.utils.radio.premium import RadioUnavailable

    await interaction.response.defer(with_message=False)
    tr = await bot.pool.i18n.for_guild(interaction.guild)
    hub = getattr(bot.pool, "radio", None)
    try:
        premium = None if hub is None else await hub.premium.fresh(interaction.guild_id)
    except RadioUnavailable:
        await interaction.followup.send(tr("radio.owner.unavailable"), ephemeral=True)
        return
    try:
        first = await load_guild_pin(bot, tr, interaction.guild_id)
        await interaction.edit_original_message(components=replace_guild_pin(interaction.message, first))
    except Exception as error:  # noqa: BLE001 - redesenhar o select é conveniência; só o tipo vai ao log
        logger.warning("Select da Jukebox não redesenhado (%s)", type(error).__name__)
    if premium is None or hub is None:
        await interaction.followup.send(tr("radio.refused.no_premium"), ephemeral=True)
        return
    try:
        stations = await hub.stations.list_for_guild(interaction.guild_id)
    except RadioUnavailable:
        await interaction.followup.send(tr("radio.owner.unavailable"), ephemeral=True)
        return
    try:
        favorites = await hub.custom_favorites.list(interaction.guild_id, interaction.author.id)
    except RadioUnavailable:
        favorites = []  # sem as ⭐ a lista só perde a ordem

    async def preview(click: disnake.MessageInteraction) -> tuple[bool, bool, Optional[str]]:
        resolved = resolve_player(bot.pool, click.guild_id, click.author)
        if resolved is None:
            return False, False, None
        player = resolved[1]
        live = hub.jukebox.view(player)
        try:
            votes = await hub.jukebox.station_preview(player.guild.get_member(click.author.id) or click.author, player)
        except Exception as error:  # noqa: BLE001 - o aviso é conveniência; só o tipo vai ao log
            logger.warning("Prévia da estação falhou (%s)", type(error).__name__)
            votes = False
        return bool(player.queue), votes, live.name if live is not None else None

    chooser = StationChooser(
        jukebox=hub.jukebox,
        tr=tr,
        author_id=interaction.author.id,
        stations=stations,
        favorites=favorites,
        genre_groups=hub.stations.genre_groups(),
        preview=preview,
    )
    chooser.message = await interaction.followup.send(
        **chooser.render(), ephemeral=True, allowed_mentions=disnake.AllowedMentions.none()
    )
