"""Jukebox (``muse_jukebox``): o player em Components V2 do próprio Discord, com o disco girando na Thumbnail (spec C, §1 a §6).

Uma mensagem V2 só tem ``components`` (nada de ``content`` nem ``embeds``). A árvore é um ``Container`` com a cor de destaque do
estado: a ``Section`` (cabeçalho, título e artista, com o disco de acessório), a linha do tempo, os chips, "A seguir" e as linhas
de controle com os MESMOS ``custom_id`` das outras skins (``_controls``). Todo texto de fora passa pelas peças já escapadas de
``_text``; a regra de call privada é a de sempre (``jukebox_panel.player_hides_details``, via ``tx.Context.hidden``).

``build`` é síncrono e não conhece o disco: o passo ``np_v2`` pede o disco (assíncrono) e entrega a referência em ``thumb``
(``attachment://disc-<hash>.gif``/``.png``, a URL da capa no plano B, ou o logo da estação). ``load(player)`` (sem passo) monta
com o disco "sem capa" por URL, só para quem lê a skin fora do ``invoke_np``.
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple, Optional

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.jukebox_panel import last_action_of
from empiremuse.rules.text import external_text
from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.skins.muse import _text as tx
from empiremuse.utils.music.skins.muse._base import base_features, playing_on_bot, preview_url
from empiremuse.utils.music.skins.muse._controls import components

SKIN_ID = "muse_jukebox"
V2 = True  # o ``np_v2`` trata esta skin como Components V2
VIOLET = disnake.Colour(0x8B5CF6)
AMBER = disnake.Colour(0xF59E0B)
RED = disnake.Colour(0xE11D48)
GRAY = disnake.Colour(0x80848E)
CALL_MAX = 40
TITLE_MAX = 80
ARTISTS_MAX = 80
ALBUM_MAX = 60
JUKEBOX_QUEUE_ITEMS = 8
NORMAL_QUEUE_ITEMS = 3
TEXT_MAX = 4000  # a soma dos TextDisplay de uma mensagem V2
ACTION_MAX = 300
ALT_MAX = 1024
NO_COVER_ASSET = "disc_nocover.png"

ICON_PLAYING, ICON_PAUSED, ICON_LIVE, ICON_RETRY = "▶", "❚❚", "🔴", "⟳"


class Thumb(NamedTuple):
    """A imagem da ``Section``: ``media`` (``attachment://…`` ou URL) e o texto alternativo."""

    media: str
    alt: str


_LABEL_SAFE = str.maketrans({"[": "(", "]": ")", "\\": "/"})


def fallback_thumb(player: Any) -> Thumb:
    """Sem passo de disco (``load`` solto): o "sem capa" fixo por URL, com o texto alternativo simples."""
    return Thumb(asset_url(NO_COVER_ASSET), player.tr("jbx.disc_alt_plain"))


# ---------------------------------------------------------------------- estado


def accent(player: Any, ctx: tx.Context) -> disnake.Colour:
    """A cor de destaque (decorativa: o estado também vem escrito no cabeçalho)."""
    if ctx.radio is not None:
        return RED if ctx.radio.on_air else AMBER
    if player.paused:
        return AMBER
    if player.current is not None and player.current.is_stream:
        return RED
    return VIOLET


def header(player: Any, ctx: tx.Context, tr: tx.Tr) -> str:
    """``-# ▶ TOCANDO · Lounge``; em call privada sem o nome da call; sem o nome do bot (já é o autor)."""
    if ctx.radio is not None:
        icon, word = (ICON_LIVE, "jbx.state.radio") if ctx.radio.on_air else (ICON_RETRY, "jbx.state.reconnecting")
    elif player.paused:
        icon, word = ICON_PAUSED, "jbx.state.paused"
    elif player.current is not None and player.current.is_stream:
        icon, word = ICON_LIVE, "jbx.state.radio"
    else:
        icon, word = ICON_PLAYING, "jbx.state.playing"
    parts = [f"{icon} {tr(word).upper()}"]
    voice = tx.voice_channel(player)
    if not ctx.hidden and voice is not None:
        parts.append(external_text(voice.name, CALL_MAX, markdown=False))
    return "-# " + " · ".join(p for p in parts if p)


def link_label(text: str) -> str:
    """Rótulo de link mascarado: o Discord NÃO desfaz ``\\(`` dentro de ``[..](..)`` (a barra aparecia), então aqui nada de escape;
    ``[``, ``]`` e a barra, que quebrariam o link, viram parecidos inofensivos."""
    return text.translate(_LABEL_SAFE)


def _title_text(track: Any, tr: tx.Tr) -> str:
    """O título da faixa em ``##`` com o link; transmissão sem título (vazio ou igual à URL) = ``jbx.stream.untitled``."""
    url = tx.track_url(track)
    title = link_label(external_text(track.single_title, TITLE_MAX, markdown=False))
    raw_url = external_text(str(track.uri or track.search_uri or ""), TITLE_MAX, markdown=False)
    if track.is_stream and (not title or title == raw_url):
        title = tr("jbx.stream.untitled")
    title = title or "—"
    return f"## [{title}]({url})" if url and len(url) <= URL_MAX else f"## {title}"  # URL enorme comeria o teto de 4000


def _track_lines(player: Any, ctx: tx.Context, tr: tx.Tr) -> list[str]:
    current = player.current
    head = []
    if artists := external_text(current.authors_string, ARTISTS_MAX):
        head.append(f"**{artists}**")
    if album := external_text(current.album_name or "", ALBUM_MAX):
        head.append(album)
    lines = [" · ".join(head)] if head else []
    sub = []
    if requested := tx.requested_line(player, ctx, tr):
        sub.append(requested)
    if playlist := tx.playlist_md(current):
        sub.append(tr("jbx.text.from_playlist", nome=playlist))
    if sub:
        lines.append("-# " + " · ".join(sub))
    return lines


def _radio_lines(ctx: tx.Context, tr: tx.Tr) -> list[str]:
    """R: ``## estação``, o título ICY (ou "Rádio ao vivo") e a linha de país e "entrou no ar" (ou a nova tentativa)."""
    radio = ctx.radio
    title = f"**{external_text(radio.title, tx.ICY_MAX)}**" if radio.title else tr("jbx.radio.live_line")
    sub = []
    if radio.country:
        sub.append(tx.station_md(radio.country))
    if radio.on_air:
        sub.append(tr("jbx.radio.since_short", t=tx.relative(radio.since)))
    else:
        retry = tr("jbx.retry", n=radio.attempt)
        sub.append(f"{retry} · {tx.relative(radio.retry_at)}" if radio.retry_at else retry)
    return [f"## {tx.radio_title(ctx)}", title, "-# " + " · ".join(sub)]


def section_texts(player: Any, ctx: tx.Context, tr: tx.Tr) -> list[str]:
    """Os textos da ``Section``: cabeçalho, título e o resto. Em call privada, nada da faixa nem da estação (spec §6)."""
    head = header(player, ctx, tr)
    if ctx.hidden:
        radio_like = ctx.radio is not None or player.current.is_stream
        title = tr("jbx.radio.live_line") if radio_like else tr("jbx.hidden_track")
        return [head, f"## {title}", f"-# {tr('jbx.hidden_note')}"]
    if ctx.radio is not None:
        return [head, *_radio_lines(ctx, tr)]
    return [head, _title_text(player.current, tr), "\n".join(_track_lines(player, ctx, tr))]


def timeline(player: Any, ctx: tx.Context, tr: tx.Tr) -> str:
    """``1:12 ▰▰▰▰●▱▱▱▱▱▱▱ 3:29 · termina <t:R>``; pausada, sem o ``<t:R>``; ao vivo, sem barra; em R, nenhuma."""
    current = player.current
    if ctx.radio is not None or ctx.hidden:  # call privada: nem a duração da faixa
        return ""
    if current.is_stream:
        return f"{ICON_LIVE} {tr('jbx.state.radio')}"
    bar = tx.text_bar(tx.bar_segment(player))
    total = f"`{tx.clock(current.duration)}`"
    if player.paused:
        return f"`{tx.clock(player.position)}` {bar} {total} · {tr('jbx.paused_word')}"
    end = tx.track_end(player, ctx.now)
    return f"`{tx.clock(tx.elapsed_ms(player))}` {bar} {total} · {tr('jbx.text.ends', t=tx.relative(end))}"


ROW_WIDTH = (
    76  # caracteres visíveis de uma linha da fila no contêiner de 600 px do desktop (o ``<t:R>`` conta TIME_WIDTH)
)
TIME_WIDTH = 14  # "em 25 segundos"
ROW_MIN_TITLE = 24
ROW_ARTIST_MAX = 24
ROW_NAME_MAX = 14
URL_MAX = 400


def _visible(text: str) -> int:
    return len(text.replace("\\", ""))


def queue_row(player: Any, n: int, track: Any, start: Optional[float]) -> str:
    """Uma linha da fila numa linha só no desktop: o artista sai quando o título já começa com ele ("Dua Lipa - Levitating") e o
    título é cortado para o que sobra; o horário relativo fica na mesma linha."""
    length = "🔴" if track.is_stream else tx.clock(track.duration)
    tail = []
    artist = external_text(track.authors_string, ROW_ARTIST_MAX)
    plain_title = external_text(track.single_title, TITLE_MAX, markdown=False).casefold()
    plain_artist = external_text(track.authors_string, TITLE_MAX, markdown=False).casefold()
    if artist and not (plain_artist and plain_title.startswith(plain_artist)):
        tail.append(artist)
    tail.append(length)
    if not tx.by_bot(player, track) and (who := tx.member_name(player, track.requester)):
        tail.append(who if _visible(who) <= ROW_NAME_MAX else who[: ROW_NAME_MAX - 1].rstrip("\\") + "…")
    if start is not None:
        tail.append("x" * TIME_WIDTH)  # só a largura: o ``<t:R>`` entra abaixo
    room = max(ROW_MIN_TITLE, ROW_WIDTH - 3 - sum(_visible(part) + 3 for part in tail))
    title = external_text(track.single_title, room) or "—"
    if start is not None:
        tail[-1] = tx.relative(start)
    return f"`{n:02d}` " + " · ".join([f"**{title}**", *tail])


def _queue_tail(player: Any, ctx: tx.Context, tr: tx.Tr, shown: int, total: int) -> str:
    tail = []
    if total > shown:
        tail.append(tr("jbx.and_more", n=total - shown))
    if (end := tx.queue_end(player, ctx.now)) is not None:
        tail.append(tr("jbx.text.queue_ends", t=tx.relative(end)))
    return " · ".join(tail)


def _queue_limit(player: Any) -> int:
    if player.static:
        return JUKEBOX_QUEUE_ITEMS
    return NORMAL_QUEUE_ITEMS if player.mini_queue_enabled else 0


def queue_text(player: Any, ctx: tx.Context, tr: tx.Tr) -> str:
    """O bloco "A seguir" (ou "Recomendadas"); fila vazia sem recomendadas = a linha ``jbx.queue.empty``; sem nada = vazio."""
    kind, tracks = tx.queue_items(player)
    if ctx.hidden:
        return f"-# {tr('jbx.hidden_queue', count=len(tracks))}" if tracks else ""
    if not tracks:
        if ctx.station is not None:
            return f"-# {tr('jbx.then_station', nome=tx.server_station_md(ctx.station.name))}"
        return "" if ctx.radio is not None else f"-# {tr('jbx.queue.empty')}"
    limit = _queue_limit(player)
    if not limit:
        return ""
    head = tr("jbx.up_next", n=len(tracks)) if kind == "queue" else tr("jbx.recommended_list")
    repeating = player.loop == "current" or getattr(player.current, "track_loops", 0) > 0
    start = tx.track_end(player, ctx.now) if player.static and not repeating else None
    lines = [f"### {head}"]
    for n, track in enumerate(tracks[:limit], start=1):
        lines.append(queue_row(player, n, track, start))
        if start is not None:
            start = None if track.is_stream else start + track.duration / 1000
    if tail := _queue_tail(player, ctx, tr, min(limit, len(tracks)), len(tracks)):
        lines.append(f"-# {tail}")
    if ctx.station is not None:
        lines.append(f"-# {tr('jbx.then_station', nome=tx.server_station_md(ctx.station.name))}")
    return "\n".join(lines)


def last_action_text(player: Any, ctx: tx.Context) -> str:
    text = last_action_of([(ctx.hidden, player.command_log)])
    return fit(f"-# {player.command_log_emoji} {text}".replace("  ", " ").rstrip(), ACTION_MAX) if text else ""


def fit_lines(text: str, room: int) -> str:
    """O bloco cortado por linhas inteiras (a cauda sai) para caber em ``room`` caracteres."""
    if len(text) <= room:
        return text
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        if used + len(line) + 1 > room:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


# ---------------------------------------------------------------------- a árvore


def _text(content: str) -> disnake.ui.TextDisplay:
    return disnake.ui.TextDisplay(fit(content, TEXT_MAX))


def rows(player: Any, ctx: tx.Context) -> list[Any]:
    """Os 5 botões, o select de opções e, no canal da Jukebox fora de call privada, o select da fila (em ``ActionRow``)."""
    from empiremuse.utils.music.v2 import components_to_rows

    return components_to_rows(components(player, hidden=ctx.hidden))


def container(player: Any, ctx: tx.Context, tr: tx.Tr, thumb: Thumb) -> disnake.ui.Container:
    """O ``Container`` do estado "música" (e de R, S e escondida): a árvore da spec §2."""
    texts = section_texts(player, ctx, tr)
    line = timeline(player, ctx, tr)
    chips = "  ".join(tx.chips(player, ctx, tr))
    action = last_action_text(player, ctx)
    # o teto de 4000 é da mensagem toda: a fila (o único bloco que cresce) leva o que sobra
    room = TEXT_MAX - sum(len(t) for t in (*texts, line, chips, action)) - 1
    queue = fit_lines(queue_text(player, ctx, tr), max(0, room))
    children: list[Any] = [
        disnake.ui.Section(
            *(_text(t) for t in texts if t),
            accessory=disnake.ui.Thumbnail(media=thumb.media, description=fit(thumb.alt, ALT_MAX)),
        )
    ]
    if line:
        children.append(_text(line))
    children.append(_text(chips))
    children.append(disnake.ui.Separator(divider=True, spacing=disnake.SeparatorSpacing.small))
    if queue:
        children.append(_text(queue))
        children.append(disnake.ui.Separator(divider=False, spacing=disnake.SeparatorSpacing.small))
    children.extend(rows(player, ctx))
    if action:
        children.append(_text(action))
    return disnake.ui.Container(*children, accent_colour=accent(player, ctx))


def build(player: Any, now: float, thumb: Optional[Thumb] = None) -> dict:
    """O payload V2 do player tocando. ``thumb`` é a imagem que o passo de disco escolheu (``None`` = o "sem capa" por URL)."""
    ctx = tx.context(player, now)
    tree = container(player, ctx, player.tr, thumb if thumb is not None else fallback_thumb(player))
    return {"content": None, "embeds": [], "components": [tree]}


def idle_payload(tr: tx.Tr, components: Any, *, off: Optional[int] = None, lead: str = "") -> dict:
    """O ocioso (spec §4) como payload V2: título, texto e (só com timer de desligar, ``off`` em epoch) quando desliga; os selects
    de pedir música e de playlists e rádios (``components``) sem mudar valor. ``lead`` = linha antes do título (post de fórum)."""
    from empiremuse.utils.music.v2 import components_to_rows

    lines = [f"## {tr('jbx.idle.title')}", tr("jbx.idle.body")]
    if off is not None:
        lines.append("-# " + tr("jbx.idle.off", t=f"<t:{off // tx.TIME_QUANTUM_S * tx.TIME_QUANTUM_S}:R>"))
    if lead:
        lines.insert(0, lead)
    tree = disnake.ui.Container(
        _text("\n".join(lines)),
        disnake.ui.Separator(divider=True, spacing=disnake.SeparatorSpacing.small),
        *components_to_rows(components),
        accent_colour=GRAY,
    )
    return {"content": None, "embeds": [], "components": [tree], "attachments": []}


def idle(player: Any, kwargs: dict) -> dict:
    """O ocioso do player: ``idle_payload`` com o timer de desligar (só sem ``keep_connected``)."""
    from empiremuse.utils.others import PlayerControls

    off = None if player.keep_connected else int(time.time()) + int(player.bot.config["IDLE_TIMEOUT"])
    out = {name: value for name, value in kwargs.items() if name not in ("embed", "content", "components")}
    components = kwargs.get("components")
    if tx.is_hidden(player):  # call privada: as opções "voltar" e "relacionadas" não levam o título da última faixa
        for item in components or ():
            for option in getattr(item, "options", ()):
                if option.value in (PlayerControls.back, PlayerControls.autoplay):
                    option.description = player.tr("jbx.hidden_track")
    out.update(idle_payload(player.tr, components, off=off, lead=kwargs.get("content") or ""))
    return out


class MuseJukeboxSkin:
    __slots__ = ("name", "preview", "card", "theme", "project", "v2")

    def __init__(self) -> None:
        self.name = SKIN_ID
        self.preview = preview_url(SKIN_ID)
        self.card = None  # nenhum cartão: o disco vem do passo ``np_v2``
        self.theme = None
        self.project = None
        self.v2 = V2

    def setup_features(self, player: Any) -> None:
        from empiremuse.utils.music.card_facts import text_bar_interval_s

        base_features(player, auto_update=text_bar_interval_s(playing_on_bot(player)), mini_queue=True)

    def load(self, player: Any) -> dict:
        return build(player, time.time())

    def build(self, player: Any, now: float, thumb: Optional[Thumb] = None) -> dict:
        return build(player, now, thumb)

    def idle(self, player: Any, kwargs: dict) -> dict:
        return idle(player, kwargs)


def load() -> MuseJukeboxSkin:
    return MuseJukeboxSkin()
