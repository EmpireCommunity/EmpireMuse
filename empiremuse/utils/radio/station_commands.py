"""Os comandos ``/radio station create|edit|delete|list`` (item 8, R5): a cola entre o slash e a interface (``station_ui``). As regras, o teto e a gravação são de ``hub.stations``.

Cada comando exige ``manage_guild`` EM EXECUÇÃO (o Discord também filtra, mas a decisão é nossa), responde só para quem pediu e leva um cooldown de 5 s por servidor.
O premium não é exigido para GERIR estações (só para ligar uma rádio); sem a chave do Last.fm o editor avisa que gênero e artistas não tocam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import disnake
from disnake.ext import commands

from empiremuse.rules.radio_station import (
    KIND_GENRE,
    MAX_STATIONS_PER_GUILD,
    STATION_KINDS,
    CustomStation,
    normalize_name,
)
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.station_ui import ConfirmDelete, StationEditor, summary

if TYPE_CHECKING:
    from empiremuse.cogs.radio import Radio

STATION_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 5, commands.BucketType.guild)
MAX_CHOICES = 25
NAME_LIMIT = 80


def can_manage(interaction: Any) -> bool:
    return bool(interaction.author.guild_permissions.manage_guild)


async def gate(cog: Radio, inter: disnake.ApplicationCommandInteraction):
    """Confirma em 3 s, traduz, confere ``manage_guild`` agora e aplica o cooldown de 5 s por servidor."""
    await inter.response.defer(ephemeral=True)
    tr = await cog.bot.pool.i18n.for_guild(inter.guild)
    if not can_manage(inter):
        raise GenericError(tr("radio.refused.no_manage_guild"))
    bucket = STATION_COOLDOWN.get_bucket(inter)
    if bucket is not None and (retry := bucket.update_rate_limit()):
        raise GenericError(tr("radio.cooldown", seconds=int(retry) + 1))
    return tr


async def playlist_names(inter: Any) -> list[str]:
    from empiremuse.utils.server_playlists import safe_list  # tarde: o módulo importa muita coisa

    return [p.name for p in await safe_list(inter.bot, inter.guild_id)]


async def server_station_autocomplete(inter: disnake.ApplicationCommandInteraction, current: str) -> list[str]:
    """Até 25 nomes de estação DESTE servidor (filtrados pelo que foi digitado); sem ``manage_guild`` ou sem banco, vazio."""
    if inter.guild_id is None or not can_manage(inter):
        return []
    try:
        stations = await inter.bot.pool.radio.stations.list_for_guild(inter.guild_id)
    except RadioUnavailable:
        return []
    wanted = (current or "").strip().casefold()
    return [s.name for s in stations if wanted in s.name.casefold()][:MAX_CHOICES]


async def find(cog: Radio, inter: Any, tr: Any, name: str) -> CustomStation:
    wanted = normalize_name(name)
    try:
        stations = await cog.hub.stations.list_for_guild(inter.guild_id)
    except RadioUnavailable:
        raise GenericError(tr("radio.owner.unavailable")) from None
    found = next((s for s in stations if s.name_norm == wanted), None)
    if found is None:
        raise GenericError(tr("radio.station.err.not_found"))
    return found


async def create(cog: Radio, inter: disnake.ApplicationCommandInteraction, name: str, kind: Optional[str]) -> None:
    tr = await gate(cog, inter)
    try:
        playlists = await playlist_names(inter)
    except RadioUnavailable:
        raise GenericError(tr("radio.owner.unavailable")) from None
    editor = StationEditor(
        cog.hub,
        tr,
        author_id=inter.author.id,
        guild_id=inter.guild_id,
        playlists=playlists,
        station=None,
        name=" ".join((name or "").split()),
        kind=kind if kind in STATION_KINDS else KIND_GENRE,
        can_manage=can_manage,
    )
    await inter.edit_original_message(**editor.render())


async def edit(cog: Radio, inter: disnake.ApplicationCommandInteraction, name: str) -> None:
    tr = await gate(cog, inter)
    station = await find(cog, inter, tr, name)
    editor = StationEditor(
        cog.hub,
        tr,
        author_id=inter.author.id,
        guild_id=inter.guild_id,
        playlists=await playlist_names(inter),
        station=station,
        name=station.name,
        kind=station.kind,
        can_manage=can_manage,
        on_saved=lambda saved: cog.station_saved(saved),
    )
    await inter.edit_original_message(**editor.render())


async def delete(cog: Radio, inter: disnake.ApplicationCommandInteraction, name: str) -> None:
    tr = await gate(cog, inter)
    station = await find(cog, inter, tr, name)
    view = ConfirmDelete(cog.hub, tr, author_id=inter.author.id, station=station, can_manage=can_manage)
    await inter.edit_original_message(
        content=tr("radio.station.ui.delete_ask", name=external_text(station.name, NAME_LIMIT)), view=view
    )


async def list_stations(cog: Radio, inter: disnake.ApplicationCommandInteraction) -> None:
    tr = await gate(cog, inter)
    try:
        stations = await cog.hub.stations.list_for_guild(inter.guild_id)
    except RadioUnavailable:
        raise GenericError(tr("radio.owner.unavailable")) from None
    if not stations:
        await inter.edit_original_message(content=tr("radio.station.list.empty"))
        return
    lines = [tr("radio.station.list.title", count=len(stations), max=MAX_STATIONS_PER_GUILD)]
    for station in stations:
        lines.append(
            f"• **{external_text(station.name, NAME_LIMIT)}** ({tr(f'radio.station.kind.{station.kind}')}): {summary(tr, station)}"
        )
    if cog.hub.lastfm.configured:
        lines.append(f"-# {tr('radio.station.list.credit')}")
    await inter.edit_original_message(
        content="\n".join(lines), allowed_mentions=disnake.AllowedMentions.none(), suppress_embeds=True
    )
