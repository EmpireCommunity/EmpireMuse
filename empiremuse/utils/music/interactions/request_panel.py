# -*- coding: utf-8 -*-
"""Painel do canal de pedidos com 2+ players (item 11, P4): uma seção por player, botões comuns e o select de playlists.

Só monta a mensagem: quem envia/edita é o bot do painel (``utils/server_request``), com ``AllowedMentions.none()``. Os botões
levam os ``custom_id`` de hoje (``PlayerControls``), sem bot nem player dentro: quem apertou age no player da PRÓPRIA call
(``resolve_player``, S-V8). Texto de terceiros (faixa, call, quem pediu) só sai via ``md_link``/``md_code``; call privada
não mostra faixa nem quem pediu (S-V7).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import disnake

from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.request_panel import PAUSED, PanelEntry
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.converters import md_code, md_link
from empiremuse.utils.others import PlayerControls

LAST_ACTION_MAX = 200

_STATE = {PAUSED: ("⏸️", "player.panel.state_paused"), "playing": ("▶️", "player.panel.state_playing")}


def _header(entry: PanelEntry, tr: Translator) -> str:
    """Nome do campo (texto puro no Discord: sem markdown nem menção): estado, bot e call."""
    icon, _ = _STATE.get(entry.state, _STATE["playing"])
    call = tr("player.panel.private_call") if entry.private else entry.voice_name
    return f"{icon} {entry.bot_name} · 🔊 {call}"[:256]


def _body(entry: PanelEntry, tr: Translator) -> str:
    if entry.private:
        return tr("player.panel.private_body")
    _, state_key = _STATE.get(entry.state, _STATE["playing"])
    state = tr(state_key)
    track = md_link(entry.title, entry.url, limit=60) if entry.title else tr("player.panel.no_track")
    line = f"-# {state}: {track}"
    details = []
    if entry.requester:
        details.append(f"✋ {md_code(entry.requester, limit=32)}")
    if entry.queue:
        details.append(tr("player.panel.queue_count", count=entry.queue))
    if details:
        line += "\n-# " + " · ".join(details)
    return line[:1024]


def _buttons(bot: Any, tr: Translator, *, paused: bool) -> list[disnake.ui.Button]:
    """Os botões comuns: pedir, pausar/retomar, pular, parar e favoritos; o estado de pausa vem de todos os players."""
    return [
        disnake.ui.Button(
            label=fit(tr("player.request_a_song"), 80),
            emoji=app_emoji(bot, "music_notes"),
            style=disnake.ButtonStyle.primary,
            custom_id=PlayerControls.add_song,
        ),
        disnake.ui.Button(
            emoji=app_emoji(bot, "playpause"),
            style=disnake.ButtonStyle.green if paused else disnake.ButtonStyle.grey,
            custom_id=PlayerControls.pause_resume,
        ),
        disnake.ui.Button(emoji=app_emoji(bot, "skip"), custom_id=PlayerControls.skip),
        disnake.ui.Button(emoji=app_emoji(bot, "stop"), custom_id=PlayerControls.stop),
        disnake.ui.Button(emoji=app_emoji(bot, "loved"), custom_id=PlayerControls.fav_manager),
    ]


def render_panel(
    bot: Any,
    guild: disnake.Guild,
    entries: Sequence[PanelEntry],
    *,
    tr: Translator,
    playlist_options: Sequence[disnake.SelectOption] = (),
    last_action: str = "",
) -> dict[str, Any]:
    """A mensagem do painel com 2+ players: ``{"content", "embeds", "components"}`` (quem envia põe ``AllowedMentions.none()``)."""
    embed = disnake.Embed(title=tr("player.panel.title"), color=bot.get_color(guild.me))
    embed.description = tr("player.panel.intro", count=len(entries))
    for entry in entries[:25]:  # um embed tem no máximo 25 campos
        embed.add_field(name=_header(entry, tr), value=_body(entry, tr), inline=False)
    footer = tr("player.panel.footer")
    if last_action:
        footer = tr(
            "player.panel.last_action_footer", action=last_action.replace("**", "")[:LAST_ACTION_MAX], footer=footer
        )
    embed.set_footer(text=footer)
    embed.set_thumbnail(guild.me.display_avatar.replace(size=256).url)

    components: list[Any] = [
        disnake.ui.ActionRow(*_buttons(bot, tr, paused=all(e.state == PAUSED for e in entries) if entries else False))
    ]
    if playlist_options:
        components.append(
            disnake.ui.ActionRow(
                disnake.ui.Select(
                    placeholder=fit(tr("player.panel.playlists.placeholder"), 150),
                    options=list(playlist_options)[:25],
                    custom_id="player_guild_pin",
                    min_values=0,
                    max_values=1,
                )
            )
        )
    return {"content": None, "embeds": [embed], "components": components}
