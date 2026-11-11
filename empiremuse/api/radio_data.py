"""Saída de dados puros das rotas de rádio (B4, contrato T-456 §4 m5): cada função **monta** o objeto campo a campo, então
só sai o que está escrito aqui (lista de permissão); campo novo nas rádios ou nas estações não vaza sozinho.

Nada de segredo, de URL de fluxo (``url_resolved``), de favicon, de ``updated_by`` nem de texto de usuário final. Nome e
título que vêm de fora (estação do diretório, título ICY) passam por ``external_text`` (sem menção nem link vivos).
"""

from __future__ import annotations

from typing import Any, Optional

from empiremuse.panel.services.radio import RadioPage, state_of
from empiremuse.panel.services.radio_live import StationView
from empiremuse.panel.services.radio_local import LocalView, Paging
from empiremuse.panel.services.radio_stations import KIND_ARTISTS, KIND_GENRE, KIND_PLAYLISTS, channel_label
from empiremuse.rules.radio import CHAT_FIELDS, KIND_LIVE
from empiremuse.rules.text import external_text

NAME_LIMIT = 100
TITLE_LIMIT = 200
SOURCE_ID_LIMIT = 64


def safe_text(value: Any, limit: int = NAME_LIMIT) -> Optional[str]:
    """Texto de fora como pode ser publicado (sem menção nem link vivos, sem o escape de markdown); ``None`` se vazio."""
    return external_text(value, limit, markdown=False) or None


def station_view_entry(view: StationView) -> dict[str, Any]:
    """Uma estação do diretório: nunca a URL do fluxo nem a do logo."""
    return {
        "uuid": view.uuid,
        "name": safe_text(view.nome),
        "country": safe_text(view.pais),
        "codec": safe_text(view.codec, 16),
        "bitrate": view.bitrate,
        "unavailable": view.indisponivel,
    }


def source_ids(radio: Any) -> dict[str, Optional[str]]:
    """O que a rádio toca: ``station`` (uuid, id de estação do servidor ou ``g:<gênero>``) e, nas ao vivo, ``reserve``."""
    source = getattr(radio, "source", None) or {}
    out: dict[str, Optional[str]] = {}
    for name in ("station", "reserve"):
        value = source.get(name)
        out[name] = value[:SOURCE_ID_LIMIT] if isinstance(value, str) else None
    return out


async def radio_entry(
    ctx: Any,
    page: RadioPage,
    radio: Any,
    *,
    premium: bool,
    stations: bool,
) -> dict[str, Any]:
    """Uma rádio do servidor. ``can_edit`` = premium e ``manage_messages`` no canal; ``stations`` pede ao diretório os
    nomes da principal e da reserva (só as ao vivo, e só com premium, como a página do painel)."""
    bot = page.bot_of(ctx, radio)
    channel = bot.get_channel(int(radio.channel_id)) if bot is not None else None
    state = ctx.pool.radio.service.state(radio.id)
    entry: dict[str, Any] = {
        "id": str(radio.id),
        "kind": str(radio.kind),
        "enabled": bool(radio.enabled),
        "state": state_of(radio, state),
        "version": str(radio.rev),
        "bot": {"id": str(radio.bot_id), "name": str(bot.user.name) if bot is not None else None},
        "channel": {"id": str(radio.channel_id), "name": str(channel.name) if channel is not None else None},
        "title": safe_text(getattr(state, "title", None), TITLE_LIMIT) if state is not None else None,
        "can_edit": bool(premium and await page.can_edit(ctx, radio)),
        "chat": {name: radio.chat.get(name) for name in CHAT_FIELDS},
        "source": source_ids(radio),
    }
    if stations and radio.kind == KIND_LIVE:
        main = await page.live.station_view(ctx, radio.source.get("station"))
        reserve = await page.live.station_view(ctx, radio.source.get("reserve"))
        entry["stations"] = {
            "main": station_view_entry(main) if main is not None else None,
            "reserve": station_view_entry(reserve) if reserve is not None else None,
        }
    return entry


def setup_options_entry(options: Any) -> dict[str, Any]:
    """O que o formulário de criar e configurar oferece: os bots reserva livres e os canais de voz e de palco."""
    return {
        "bots": [{"id": str(bot_id), "name": safe_text(name)} for bot_id, name in options.bots],
        "channels": [
            {"id": str(channel_id), "name": safe_text(name), "kind": str(kind)}
            for channel_id, name, kind in options.channels
        ],
    }


def local_entry(view: Optional[LocalView]) -> dict[str, Any]:
    """A cascata de local: os níveis (com as opções de cada um), o nó escolhido e se ele é final."""
    if view is None:
        return {"node": None, "final": False, "levels": []}
    return {
        "node": view.node,
        "final": view.final,
        "levels": [
            {
                "depth": level.depth,
                "parent": level.parent,
                "selected": level.selected,
                "options": [
                    {
                        "id": option.id,
                        "label": safe_text(option.label),
                        "description": safe_text(option.description),
                        "count": option.count,
                    }
                    for option in level.options
                ],
            }
            for level in view.levels
        ],
    }


def paging_entry(paging: Optional[Paging]) -> Optional[dict[str, Any]]:
    return {"node": paging.node, "page": paging.page, "pages": paging.pages} if paging is not None else None


def custom_source(station: Any) -> dict[str, Any]:
    """A fonte de uma estação do servidor, só a que o tipo dela usa."""
    source = dict(getattr(station, "source", {}) or {})
    kind = str(station.kind)
    if kind == KIND_GENRE:
        return {"genre": str(source.get(KIND_GENRE) or "")}
    if kind in (KIND_ARTISTS, KIND_PLAYLISTS):
        return {kind: [safe_text(item) or "" for item in source.get(kind) or ()]}
    return {}


async def custom_station_entry(ctx: Any, station: Any) -> dict[str, Any]:
    """Uma estação personalizada do servidor, com as rádios que a usam (o canal e o id; nunca o resto da rádio)."""
    using = await ctx.pool.radio.stations.radios_using(ctx.guild_id, station.id)
    return {
        "id": str(station.id),
        "name": safe_text(station.name),
        "kind": str(station.kind),
        "source": custom_source(station),
        "unknown": [safe_text(item) or "" for item in getattr(station, "unknown", ()) or ()],
        "in_use": [{"radio_id": str(radio.id), "channel_name": channel_label(ctx, radio)} for radio in using],
        "version": str(station.rev),
    }
