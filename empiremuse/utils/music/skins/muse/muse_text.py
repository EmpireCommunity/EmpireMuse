"""Jukebox Texto v2 (``muse_text``, spec §2B): tudo em texto, com hierarquia (título, subtexto, chips, "A seguir").

Nada aqui depende de imagem gerada: a faixa de luz é um asset fixo por URL (``branding.asset_url``).

A barra em texto anda a cada ``text_bar_interval_s`` (opção C do Alicerce): o ``auto_update`` relê a skin, e o ``_np_data`` só edita
quando o texto mudou (o segmento da barra; os horários saem arredondados).
"""

from __future__ import annotations

import time
from typing import Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import music_source_image
from empiremuse.utils.music.skins.muse import _text as tx
from empiremuse.utils.music.skins.muse._base import base_features, playing_on_bot, preview_url
from empiremuse.utils.music.skins.muse._controls import components

SKIN_ID = "muse_text"
WAVE_ASSET = "nebulosa_wave.gif"
PAUSE_ICON = "player_pause_icon.png"
AUTHOR_MAX = 256
DESCRIPTION_MAX = 4096


def author_icon(player: Any) -> str:
    if player.paused:
        return asset_url(PAUSE_ICON)
    return music_source_image(player.current.info.get("sourceName"))


def timeline(player: Any, ctx: tx.Context, tr: tx.Tr) -> list[str]:
    """§2B.2: barra + duração e "termina <t:R>"; pausada, a barra parada com o ponto exato e "pausada"; ao vivo, sem barra."""
    current = player.current
    if current.is_stream:
        return [f"🔴 {tr('jbx.state.radio')}"]
    total = f"`{tx.clock(current.duration)}`"
    bar = tx.text_bar(tx.bar_segment(player))
    if player.paused:
        return [f"`{tx.clock(player.position)}` {bar} {total}", f"-# {tr('jbx.paused_word')}"]
    end = tx.track_end(player, ctx.now)
    return [f"`{tx.clock(tx.elapsed_ms(player))}` {bar} {total}", f"-# {tr('jbx.text.ends', t=tx.relative(end))}"]


def track_lines(player: Any, ctx: tx.Context, tr: tx.Tr) -> list[str]:
    """Artista · álbum, e o subtexto com a playlist e quem pediu. Em call privada, nada da faixa."""
    if ctx.hidden:
        return []
    current = player.current
    artists = tx.artists_md(current)
    head = " · ".join(p for p in (f"**{artists}**" if artists else "", tx.origin_md(current)) if p)
    sub = []
    if playlist := tx.playlist_md(current):
        sub.append(tr("jbx.text.from_playlist", nome=playlist))
    if requested := tx.requested_line(player, ctx, tr):
        sub.append(requested)
    lines = [head] if head else []
    if sub:
        lines.append("-# " + " · ".join(sub))
    return lines


def body(player: Any, ctx: tx.Context, tr: tx.Tr) -> list[str]:
    chips = "  ".join(tx.chips(player, ctx, tr))
    if ctx.radio is not None:
        return [*tx.radio_lines(ctx, tr), "", chips]
    lines = [*track_lines(player, ctx, tr), "", *timeline(player, ctx, tr), "", chips]
    if limit := tx.queue_limit(player):
        if block := tx.queue_block(player, ctx, tr, limit=limit):
            lines += ["", *block]
    return lines


def title_of(player: Any, ctx: tx.Context, tr: tx.Tr) -> tuple[str, str | None]:
    if ctx.hidden:
        return tr("jbx.hidden_track"), None
    if ctx.radio is not None:
        return tx.radio_title(ctx), None
    return tx.title_md(player.current), tx.track_url(player.current)


def thumbnail_of(player: Any, ctx: tx.Context) -> str | None:
    if ctx.hidden:
        return player.bot.user.display_avatar.replace(size=256, static_format="png").url
    if ctx.radio is not None:
        return None  # o logo da estação só pelo LogoService (F-8), nunca o favicon
    return player.current.thumb or None


def build(player: Any, now: float, *, image: str = WAVE_ASSET) -> dict:
    tr = player.tr
    ctx = tx.context(player, now)
    title, url = title_of(player, ctx, tr)
    embed = disnake.Embed(title=title, url=url, color=player.bot.get_color(player.guild.me))
    embed.set_author(name=fit(tx.author_text(player, ctx, tr), AUTHOR_MAX), icon_url=author_icon(player))
    lines = body(player, ctx, tr)
    if action := tx.last_action(player, ctx):
        lines.append(action)
    embed.description = fit("\n".join(lines).strip(), DESCRIPTION_MAX)
    if thumb := thumbnail_of(player, ctx):
        embed.set_thumbnail(url=thumb)
    embed.set_image(url=asset_url(image))
    return {"content": None, "embeds": [embed], "components": components(player, hidden=ctx.hidden)}


class MuseTextSkin:
    __slots__ = ("name", "preview", "card", "theme", "project")

    def __init__(self) -> None:
        self.name = SKIN_ID
        self.preview = preview_url(SKIN_ID)
        self.card = None  # skin de texto: nenhum passo de render
        self.theme = None
        self.project = None

    def setup_features(self, player: Any) -> None:
        from empiremuse.utils.music.card_facts import text_bar_interval_s

        base_features(player, auto_update=text_bar_interval_s(playing_on_bot(player)), mini_queue=True)

    def load(self, player: Any) -> dict:
        return build(player, time.time())


def load() -> MuseTextSkin:
    return MuseTextSkin()
