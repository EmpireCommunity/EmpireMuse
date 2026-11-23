"""As peças de texto das skins do EmpireMuse (as de texto e o embed que acompanha as de imagem). Sem I/O, síncrono.

- **Texto de fora** (faixa, artista, álbum, playlist, nome de membro, estação): sai por ``external_text`` (markdown escapado,
  sem link nem menção); o nome e o título ICY da rádio já vêm guardados escapados e saem por ``stored_text`` (via
  ``jukebox_ui``). A única menção é a de quem pediu, ``<@id>`` montado aqui de um id numérico.
- **Call privada:** o mesmo predicado do painel (``jukebox_panel.is_private``); sem call conhecida, conta como privada (como o
  ``card_facts``). Oculta faixa, capa, call, quem pediu, títulos da fila e a rádio.
- **Nada que muda sozinho entre duas leituras:** todo horário ``<t:…>`` sai arredondado em ``TIME_QUANTUM_S``, para o
  ``payload_key`` do ``_np_data`` não editar a mensagem só pelo relógio (a barra em texto anda a cada 15 s).
"""

from __future__ import annotations

import itertools
from typing import Any, Callable, NamedTuple, Optional

from empiremuse.rules.jukebox_panel import last_action_of
from empiremuse.rules.text import external_text
from empiremuse.utils.jukebox_panel import is_private
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.converters import md_url, time_format
from empiremuse.utils.music.jukebox_ui import server_station_md, station_md

TIME_QUANTUM_S = 5
TITLE_MAX = 120
ARTISTS_MAX = 120
ORIGIN_MAX = 80
NAME_MAX = 60
QUEUE_TITLE_MAX = 60
QUEUE_ARTIST_MAX = 40
ICY_MAX = 120
COUNTRY_MAX = 8
MINI_QUEUE_ITEMS = 3
STATIC_QUEUE_ITEMS = 8
TEXT_BAR_CELLS = 12
BAR_FULL, BAR_HEAD, BAR_EMPTY = "▰", "●", "▱"  # os emojis pb_* da spec ainda não existem no kit (desvio, ver relatório)

Tr = Callable[..., str]


class Context(NamedTuple):
    """O que as skins leem do player uma vez por ``load``: privacidade, modo rádio (R), estação (S) e o horário."""

    hidden: bool
    radio: Any  # ``RadioView`` ou ``None`` (sempre ``None`` em call privada)
    station: Any  # ``StationView`` ou ``None`` (sempre ``None`` em call privada)
    now: float


def _jukebox(player: Any) -> Any:
    return getattr(getattr(getattr(getattr(player, "bot", None), "pool", None), "radio", None), "jukebox", None)


def voice_channel(player: Any) -> Any:
    return getattr(getattr(getattr(getattr(player, "guild", None), "me", None), "voice", None), "channel", None)


def is_hidden(player: Any) -> bool:
    voice = voice_channel(player)
    return True if voice is None else is_private(voice)


def context(player: Any, now: float) -> Context:
    hidden = is_hidden(player)
    jukebox = _jukebox(player)
    if hidden or jukebox is None:
        return Context(hidden, None, None, now)
    radio = jukebox.view(player)
    read_station = getattr(jukebox, "station_view", None)
    station = read_station(player) if radio is None and read_station is not None else None
    return Context(hidden, radio, station, now)


def stamp(seconds: float) -> int:
    """Um horário para ``<t:…>``, arredondado: duas leituras seguidas dão o mesmo texto."""
    return int(round(seconds / TIME_QUANTUM_S) * TIME_QUANTUM_S)


def relative(seconds: float) -> str:
    return f"<t:{stamp(seconds)}:R>"


# ---------------------------------------------------------------------- faixa


def track_end(player: Any, now: float) -> Optional[float]:
    """Quando a faixa atual termina; ``None`` em transmissão, pausa ou sem faixa."""
    current = player.current
    if current is None or current.is_stream or player.paused:
        return None
    return now + max(0, current.duration - player.position) / 1000


def queue_end(player: Any, now: float) -> Optional[float]:
    """Quando a fila termina (como as Clássicas: nada com repetir, rádio, pausa ou fila vazia)."""
    end = track_end(player, now)
    if end is None or not player.queue or player.loop or player.keep_connected:
        return None
    return end + sum(t.duration for t in player.queue if not t.is_stream) / 1000


def track_url(track: Any) -> Optional[str]:
    return md_url(track.uri or track.search_uri)


def member_name(player: Any, member_id: object) -> str:
    if not str(member_id).isdigit():
        return ""
    member = player.guild.get_member(int(str(member_id)))
    return external_text(member.display_name, NAME_MAX) if member is not None else ""


def mention(member_id: object) -> str:
    return f"<@{int(str(member_id))}>" if str(member_id).isdigit() else ""


def title_md(track: Any) -> str:
    return external_text(track.single_title, TITLE_MAX) or "—"


def artists_md(track: Any) -> str:
    return external_text(track.authors_string, ARTISTS_MAX)


def origin_md(track: Any) -> str:
    return external_text(track.album_name or "", ORIGIN_MAX)


def playlist_md(track: Any) -> str:
    return external_text(track.playlist_name or "", ORIGIN_MAX)


def from_station(ctx: Context) -> bool:
    return ctx.station is not None and bool(ctx.station.playing)


def requested_line(player: Any, ctx: Context, tr: Tr) -> str:
    """ "pedida por @Alex", "Recomendada" ou "da estação {nome}"; vazio em call privada."""
    current = player.current
    if ctx.hidden or current is None:
        return ""
    if from_station(ctx):
        return tr("jbx.from_station", nome=server_station_md(ctx.station.name))
    if current.autoplay:
        return tr("jbx.recommended")
    who = mention(current.requester)
    return tr("jbx.text.requested", mention=who) if who else ""


# ---------------------------------------------------------------------- barra em texto


def text_bar(segment: Optional[int]) -> str:
    """12 células: cheias até ``segment``, a cabeça e as vazias. ``None`` = sem barra (transmissão)."""
    if segment is None:
        return ""
    filled = max(0, min(TEXT_BAR_CELLS, int(segment)))
    if filled >= TEXT_BAR_CELLS:
        return BAR_FULL * TEXT_BAR_CELLS
    return BAR_FULL * filled + BAR_HEAD + BAR_EMPTY * (TEXT_BAR_CELLS - filled - 1)


def bar_segment(player: Any) -> Optional[int]:
    from empiremuse.utils.music.card_facts import text_bar_segment  # do S5 do Malho (fonte única do segmento)

    current = player.current
    if current is None or current.is_stream:
        return None
    return text_bar_segment(player.position, current.duration)


# ---------------------------------------------------------------------- chips e estado


def chips(player: Any, ctx: Context, tr: Tr) -> list[str]:
    """Um chip por estado ligado e sempre o volume: o emoji do kit FORA e a palavra em código inline (spec §2B.1)."""
    bot = player.bot
    out: list[str] = []
    left = player.current.track_loops if player.current is not None else 0
    if left:
        out.append(f"{app_emoji(bot, 'repeat')} `{tr('jbx.repeat.track', n=left)}`")
    elif player.loop == "current":
        out.append(f"{app_emoji(bot, 'repeat')} `{tr('jbx.repeat.track_on')}`")
    elif player.loop == "queue":
        out.append(f"{app_emoji(bot, 'repeat_all')} `{tr('jbx.repeat.queue')}`")
    flags = (
        (player.autoplay, "refresh", "jbx.chip.autoplay"),
        (player.nightcore, "sound_spectrum", "jbx.chip.nightcore"),
        (player.keep_connected, "headset", "jbx.chip.always_on"),
        (player.restrict_mode, "lock", "jbx.chip.restricted"),
    )
    out.extend(f"{app_emoji(bot, emoji)} `{tr(key)}`" for on, emoji, key in flags if on is True)
    if ctx.station is not None:
        name = external_text(ctx.station.name, NAME_MAX, markdown=False).replace("`", "'")
        out.append(f"📻 `{tr('jbx.chip.station', nome=name)}`")
    vote = getattr(ctx.radio, "vote", None)
    if vote is not None:
        out.append(f"🗳 `{tr('jbx.vote', votos=vote.votes, quorum=vote.needed)}`")
    out.append(f"{app_emoji(bot, 'volume')} `{player.volume}`")
    return out


def state_key(player: Any, ctx: Context) -> str:
    if ctx.radio is not None:
        return "jbx.state.radio" if ctx.radio.on_air else "jbx.state.reconnecting"
    if player.paused:
        return "jbx.state.paused"
    if ctx.station is not None:
        return "jbx.state.station"
    return "jbx.state.playing"


def author_text(player: Any, ctx: Context, tr: Tr) -> str:
    """ "TOCANDO · Muse 2 · Lounge"; em call privada sem o nome da call."""
    parts = [tr(state_key(player, ctx)).upper(), external_text(player.guild.me.display_name, NAME_MAX, markdown=False)]
    voice = voice_channel(player)
    if not ctx.hidden and voice is not None:
        parts.append(external_text(voice.name, NAME_MAX, markdown=False))
    return " · ".join(p for p in parts if p)


def radio_lines(ctx: Context, tr: Tr) -> list[str]:
    """R: o título ICY, o país e "entrou no ar há…"; reconectando: "Tentativa n de 3" e a próxima em ``<t:R>``."""
    radio = ctx.radio
    lines = []
    if radio.title:
        lines.append(f"**{external_text(radio.title, ICY_MAX)}**")
    if radio.country:
        lines.append(f"-# {station_md(radio.country)}")
    if radio.on_air:
        lines.append(tr("jbx.radio.since", t=relative(radio.since)))
    else:
        retry = f" · {relative(radio.retry_at)}" if radio.retry_at else ""
        lines.append(f"{tr('jbx.retry', n=radio.attempt)}{retry}")
    return lines


def radio_title(ctx: Context) -> str:
    return station_md(ctx.radio.name)


# ---------------------------------------------------------------------- fila


def queue_items(player: Any) -> tuple[str, list[Any]]:
    """("queue" | "recommended" | "", faixas): a fila, senão as recomendadas do autoplay."""
    if player.queue:
        return "queue", list(player.queue)
    if player.autoplay and player.queue_autoplay:
        return "recommended", list(player.queue_autoplay)
    return "", []


def queue_item_line(player: Any, n: int, track: Any, start: Optional[float]) -> str:
    length = "🔴" if track.is_stream else time_format(track.duration)
    parts = [f"**{external_text(track.single_title, QUEUE_TITLE_MAX) or '—'}**"]
    if artist := external_text(track.authors_string, QUEUE_ARTIST_MAX):
        parts.append(artist)
    parts.append(length)
    if not track.autoplay and (who := member_name(player, track.requester)):
        parts.append(who)
    if start is not None:
        parts.append(relative(start))
    return f"`{n:02d}` " + " · ".join(parts)


def queue_block(player: Any, ctx: Context, tr: Tr, *, limit: int) -> list[str]:
    """ "### A seguir · n", o fim da fila, os itens (no canal da Jukebox, com o início em ``<t:R>``) e "Depois: estação".
    Em call privada, só a contagem."""
    kind, tracks = queue_items(player)
    if not tracks:
        return [f"-# {tr('jbx.then_station', nome=server_station_md(ctx.station.name))}"] if ctx.station else []
    if ctx.hidden:
        return [f"-# {tr('jbx.hidden_queue', count=len(tracks))}"]
    head = tr("jbx.up_next", n=len(tracks)) if kind == "queue" else tr("jbx.recommended_list")
    lines = [f"### {head}"]
    if (end := queue_end(player, ctx.now)) is not None:
        lines.append(f"-# {tr('jbx.text.queue_ends', t=relative(end))}")
    start = track_end(player, ctx.now) if player.static else None
    for n, track in enumerate(itertools.islice(tracks, limit), start=1):
        lines.append(queue_item_line(player, n, track, start))
        if start is not None:
            start = None if track.is_stream else start + track.duration / 1000
    if len(tracks) > limit:
        lines.append(f"-# {tr('jbx.and_more', n=len(tracks) - limit)}")
    if ctx.station is not None:
        lines.append(f"-# {tr('jbx.then_station', nome=server_station_md(ctx.station.name))}")
    return lines


def queue_limit(player: Any) -> int:
    """8 no canal da Jukebox; 3 na mini-fila do modo normal; 0 com a mini-fila desligada."""
    if player.static:
        return STATIC_QUEUE_ITEMS
    return MINI_QUEUE_ITEMS if player.mini_queue_enabled else 0


# ---------------------------------------------------------------------- rodapé e última ação


def last_action(player: Any, ctx: Context) -> str:
    """A última ação ("> Alex pulou a música"): texto montado pelo próprio bot, como nas Clássicas. Nada em call privada
    (a mesma regra do painel, ``last_action_of``): o registro pode trazer o título e o link da faixa."""
    text = last_action_of([(ctx.hidden, player.command_log)])
    if not text:
        return ""
    return f"> {player.command_log_emoji} {text}".rstrip()


def set_footer(embed: Any, player: Any, tr: Tr) -> None:
    """A dica, ou o servidor de música e a versão (como hoje)."""
    if player.current_hint:
        embed.set_footer(text=tr("player.tip", current_hint=str(player.current_hint)))
    else:
        embed.set_footer(text=str(player))
