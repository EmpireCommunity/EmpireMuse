"""Jukebox Texto v1 (``muse_text_classic``, spec §2): a lista de linhas com um ícone cada, opção de volta da v2 (D13).

Só carrega quando o id entrar em ``rules/skins.JUKEBOX_SKINS`` (decisão do dono: só a v2, só a v1 ou as duas); até lá o
``load_muse_skins`` não a importa. Mesmas regras de texto, privacidade e componentes da v2 (``_text``, ``_controls``).
"""

from __future__ import annotations

import time
from typing import Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.converters import time_format
from empiremuse.utils.music.skins.muse import _text as tx
from empiremuse.utils.music.skins.muse._base import base_features, preview_url
from empiremuse.utils.music.skins.muse._controls import components
from empiremuse.utils.music.skins.muse.muse_text import AUTHOR_MAX, DESCRIPTION_MAX, author_icon, thumbnail_of, title_of

SKIN_ID = "muse_text_classic"
BAR_ASSET = "nebulosa_bar.png"


def _line(player: Any, emoji: str, text: str) -> str:
    return f"{app_emoji(player.bot, emoji)} {text}" if text else ""


def timeline(player: Any, ctx: tx.Context, tr: tx.Tr) -> str:
    current = player.current
    if current.is_stream:
        return _line(player, "record", f"🔴 {tr('jbx.state.radio')}")
    total = time_format(current.duration)
    if player.paused:
        return _line(player, "history", f"{total} · {tr('jbx.paused_word')}")
    end = tx.relative(tx.track_end(player, ctx.now))
    return _line(player, "history", f"{total} · {tr('jbx.text.ends', t=end)}")


def track_lines(player: Any, ctx: tx.Context, tr: tx.Tr) -> list[str]:
    if ctx.hidden:
        return [timeline(player, ctx, tr)]
    current = player.current
    origin = " · ".join(
        p
        for p in (
            _line(player, "disc", tx.origin_md(current)),
            _line(player, "playlists", tx.playlist_md(current)),
        )
        if p
    )
    return [
        timeline(player, ctx, tr),
        _line(player, "microphone", tx.artists_md(current)),
        _line(player, "user", tx.requested_line(player, ctx, tr)),
        origin,
    ]


def body(player: Any, ctx: tx.Context, tr: tx.Tr) -> list[str]:
    chips = " · ".join(tx.chips(player, ctx, tr))
    if ctx.radio is not None:
        return [*tx.radio_lines(ctx, tr), chips]
    lines = [line for line in track_lines(player, ctx, tr) if line] + [chips]
    if limit := tx.queue_limit(player):
        if block := tx.queue_block(player, ctx, tr, limit=limit):
            lines += ["", *block]
    return lines


def build(player: Any, now: float) -> dict:
    tr = player.tr
    ctx = tx.context(player, now)
    title, url = title_of(player, ctx, tr)
    embed = disnake.Embed(title=title, url=url, color=player.bot.get_color(player.guild.me))
    embed.set_author(name=fit(tx.author_text(player, ctx, tr), AUTHOR_MAX), icon_url=author_icon(player))
    lines = body(player, ctx, tr)
    if action := tx.last_action(player):
        lines.append(action)
    embed.description = fit("\n".join(lines).strip(), DESCRIPTION_MAX)
    if thumb := thumbnail_of(player, ctx):
        embed.set_thumbnail(url=thumb)
    embed.set_image(url=asset_url(BAR_ASSET))
    tx.set_footer(embed, player, tr)
    return {"content": None, "embeds": [embed], "components": components(player, hidden=ctx.hidden)}


class MuseTextClassicSkin:
    __slots__ = ("name", "preview", "card", "theme", "project")

    def __init__(self) -> None:
        self.name = SKIN_ID
        self.preview = preview_url(SKIN_ID)
        self.card = None
        self.theme = None
        self.project = None

    def setup_features(self, player: Any) -> None:
        base_features(player, auto_update=0, mini_queue=True)  # a v1 não tem barra que anda

    def load(self, player: Any) -> dict:
        return build(player, time.time())


def load() -> MuseTextClassicSkin:
    return MuseTextClassicSkin()
