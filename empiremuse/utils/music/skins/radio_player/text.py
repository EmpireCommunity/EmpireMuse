"""Rádio Texto v2 (``text``, spec §8B): o painel da rádio sem imagem gerada, irmão da Jukebox Texto v2 (``muse/muse_text.py``).

Só o embed: os controles são os mesmos do visor e saem de ``utils/radio/panel.py`` (``text_payload``), que também anexa o logo.
Puro e síncrono, sem render: a vaga de render não é usada, e a faixa de luz é o asset fixo da Jukebox Texto (por URL).

- **Texto de fora:** na rádio ao vivo o nome, o país, o título ICY e o "Tocou há pouco" já vêm guardados escapados
  (``stored_text``); na personalizada o nome da estação e a faixa chegam crus (``external_text``). O caminho do filtro
  (nomes do local ou o tipo) é cru nas duas. Nenhum link nem menção sai daqui.
- **Call privada:** a rádio não tem essa noção hoje; o texto mostra o mesmo que o visor mostra.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.text import external_text, stored_text
from empiremuse.utils.branding import asset_url
from empiremuse.utils.radio.panel import QUALITY_EMOJI  # a cor sempre com a palavra (visor.quality.*)
from empiremuse.utils.radio.visor import following_stations  # a regra única das estações seguintes

SKIN_ID = "text"
WAVE_ASSET = "nebulosa_wave.gif"  # a mesma faixa de luz da Jukebox Texto v2
STATES = ("live", "muted", "offline")

AUTHOR_MAX = 256
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FOOTER_MAX = 2048
NAME_MAX = 80
SONG_MAX = 120
PATH_PART_MAX = 40
COUNTRY_MAX = 40
CODEC_MAX = 8
CHANNEL_MAX = 60
HISTORY_ITEMS = 5
NEXT_ITEMS = 4

Tr = Callable[..., str]
Emojis = Callable[[str], str]
Clean = Callable[..., str]


def _clean(custom: bool) -> Clean:
    """O caminho do texto da estação: cru na personalizada, guardado escapado na ao vivo."""
    return external_text if custom else stored_text


def _code(text: str) -> str:
    """Texto dentro de código inline (os chips): sem crase, que fecharia o código."""
    return text.replace("`", "'")


def state_of(snapshot: Any) -> str:
    state = getattr(snapshot, "state", "live")
    return state if state in STATES else "live"


def author_text(tr: Tr, snapshot: Any, *, custom: bool, channel: Optional[str] = None) -> str:
    """ "AO VIVO · Rádio · #lounge" (a personalizada diz "Estação do servidor" no lugar de "Rádio")."""
    parts = [tr(f"visor.{state_of(snapshot)}"), tr("visor.custom.detail" if custom else "radio.text.radio")]
    if channel and (name := external_text(channel, CHANNEL_MAX, markdown=False)):
        parts.append(f"#{name}")
    return " · ".join(parts)


def song_line(snapshot: Any, *, custom: bool) -> str:
    """ "**Céu de Neon** · Banda Órion"; vazio sem título, no mudo e fora do ar (como o painel de antes)."""
    if state_of(snapshot) != "live":
        return ""
    clean = _clean(custom)
    title = clean(getattr(snapshot, "now_title", None) or "", SONG_MAX)
    if not title:
        return ""
    artist = clean(getattr(snapshot, "now_artist", None) or "", SONG_MAX)
    return f"**{title}** · {artist}" if artist else f"**{title}**"


def origin_line(tr: Tr, snapshot: Any, *, custom: bool) -> str:
    """ "-# São Paulo, BR · entrou no ar <t:…:R>" (o horário é o do início: o mesmo texto em toda edição)."""
    parts = []
    if country := _clean(custom)(getattr(snapshot, "country", "") or "", COUNTRY_MAX):
        parts.append(country)
    started = getattr(snapshot, "started_at", None)
    if started is not None and state_of(snapshot) != "offline":
        parts.append(tr("radio.text.since", t=f"<t:{int(started.timestamp())}:R>"))
    return f"-# {' · '.join(parts)}" if parts else ""


def chips(tr: Tr, snapshot: Any, emojis: Emojis, *, custom: bool, now: float) -> list[str]:
    """A linha de chips (emoji fora, palavra em código) e, fora do ar, a próxima tentativa."""
    state = state_of(snapshot)
    out: list[str] = []
    if state == "muted":
        out.append(f"🔇 `{tr('radio.text.muted')}`")
    elif state == "offline":
        out.append(f"🔴 `{tr('radio.text.offline')}`")
    total = int(getattr(snapshot, "total", 0) or 0)
    position = int(getattr(snapshot, "position", 0) or 0)
    if total and position >= 1:
        out.append(f"`{tr('radio.text.position', n=position, total=total)}`")
    rate = int(getattr(snapshot, "bitrate_kbps", 0) or 0)
    codec = external_text(getattr(snapshot, "codec", "") or "", CODEC_MAX, markdown=False).upper()
    if detail := " · ".join(p for p in (f"{rate} kbps" if rate else "", codec) if p):
        out.append(f"`{_code(detail)}`")
    if not custom:
        quality = getattr(snapshot, "quality", "good")
        quality = quality if quality in QUALITY_EMOJI else "good"
        out.append(f"{QUALITY_EMOJI[quality]} `{tr(f'visor.quality.{quality}')}`")
    out.append(f"{emojis('volume')} `{int(getattr(snapshot, 'volume', 100) or 0)}`")
    lines = ["  ".join(out)]
    retry = getattr(snapshot, "retry_seconds", None)
    if state == "offline" and retry:
        lines.append(f"-# {tr('radio.text.retry', t=f'<t:{int(now + int(retry))}:R>')}")
    return lines


def context_line(tr: Tr, snapshot: Any, emojis: Emojis, *, custom: bool) -> str:
    """ "-# 🧭 Brasil › São Paulo · ⭐ favorita do servidor · 🎧 reserva 2 no ar"; a personalizada mostra o tipo."""
    path = tuple(getattr(snapshot, "filter_path", ()) or ())
    favorites_list = getattr(snapshot, "list_kind", "") == "favorites"
    parts: list[str] = []
    if custom:
        kind = external_text(path[-1], PATH_PART_MAX) if path else ""
        parts.append(f"{emojis('playlists')} {kind or tr('radio.text.all_types')}")
        return f"-# {' · '.join(parts)}"
    if favorites_list:
        parts.append(f"⭐ {tr('visor.favorites_path')}")
    elif names := [name for name in (external_text(part, PATH_PART_MAX) for part in path) if name]:
        parts.append("🧭 " + " › ".join(names))
    if getattr(snapshot, "favorite", False) and not favorites_list:
        parts.append(f"⭐ {tr('radio.text.favorite')}")
    reserve = getattr(snapshot, "reserve_number", None)
    parts.append(
        f"{emojis('headset')} {tr('radio.text.reserve', n=int(reserve)) if reserve else tr('radio.text.no_reserve')}"
    )
    return f"-# {' · '.join(parts)}"


def history_block(tr: Tr, snapshot: Any) -> list[str]:
    """ "### Tocou há pouco" e até 5 títulos ICY, do mais novo ao mais antigo; nada sem histórico (a personalizada não tem)."""
    items = tuple(getattr(snapshot, "recent_titles", ()) or ())[-HISTORY_ITEMS:]
    lines = [f"<t:{int(at)}:t> {title}" for at, raw in reversed(items) if (title := stored_text(raw, SONG_MAX))]
    return [f"### {tr('radio.text.history')}", *lines] if lines else []


def next_block(tr: Tr, snapshot: Any, *, custom: bool) -> list[str]:
    """ "### A seguir · 12" e até 4 estações seguintes; na ao vivo com o ponto de qualidade."""
    following = following_stations(snapshot)
    if not following:
        return []
    clean = _clean(custom)
    lines = [f"### {tr('visor.next')} · {int(getattr(snapshot, 'total', 0) or 0) or len(following)}"]
    for station in following[:NEXT_ITEMS]:
        parts = [f"**{clean(station.name, NAME_MAX) or '—'}**"]
        if station.bitrate_kbps:
            parts.append(f"{int(station.bitrate_kbps)} kbps")
        dot = "" if custom else f"{QUALITY_EMOJI.get(station.quality, QUALITY_EMOJI['good'])} "
        lines.append(f"`{int(station.position):02d}` {dot}{' · '.join(parts)}")
    return lines


def description(tr: Tr, snapshot: Any, emojis: Emojis, *, custom: bool, now: float) -> str:
    blocks = [
        [line for line in (song_line(snapshot, custom=custom), origin_line(tr, snapshot, custom=custom)) if line],
        [*chips(tr, snapshot, emojis, custom=custom, now=now), context_line(tr, snapshot, emojis, custom=custom)],
        history_block(tr, snapshot),
        next_block(tr, snapshot, custom=custom),
    ]
    return fit("\n\n".join("\n".join(block) for block in blocks if block), DESCRIPTION_MAX)


def embed(
    tr: Tr,
    snapshot: Any,
    *,
    emojis: Emojis,
    color: int = 0,
    custom: bool = False,
    channel: Optional[str] = None,
    thumbnail: Optional[str] = None,
    now: Optional[float] = None,
) -> disnake.Embed:
    """O embed da Rádio Texto v2. ``thumbnail`` = a URL da miniatura (o painel passa ``attachment://logo.png``)."""
    moment = time.time() if now is None else now
    title = _clean(custom)(getattr(snapshot, "station_name", "") or "", TITLE_MAX) or "—"
    result = disnake.Embed(
        title=fit(title, TITLE_MAX),
        description=description(tr, snapshot, emojis, custom=custom, now=moment),
        color=color,
    )
    result.set_author(name=fit(author_text(tr, snapshot, custom=custom, channel=channel), AUTHOR_MAX))
    if thumbnail:
        result.set_thumbnail(url=thumbnail)
    result.set_image(url=asset_url(WAVE_ASSET))
    result.set_footer(text=fit(tr("visor.footer"), FOOTER_MAX))
    return result
