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

from empiremuse.rules.request_panel import PAUSED, PanelEntry
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.converters import md_code, md_link
from empiremuse.utils.others import PlayerControls

MSG_JOIN_A_CALL = "**Entre na call em que um dos bots está tocando para usar isto.**"
TITLE = "🎶 Pedidos de música"
FOOTER = "Os botões e o select agem no player da call em que você está."
LAST_ACTION_MAX = 200

_STATE = {PAUSED: ("⏸️", "Em pausa"), "playing": ("▶️", "Tocando")}


def _header(entry: PanelEntry) -> str:
    """Nome do campo (texto puro no Discord: sem markdown nem menção): estado, bot e call."""
    icon, _ = _STATE.get(entry.state, _STATE["playing"])
    call = "canal privado" if entry.private else entry.voice_name
    return f"{icon} {entry.bot_name} · 🔊 {call}"[:256]


def _body(entry: PanelEntry) -> str:
    if entry.private:
        return "-# 🔒 A call é privada: a faixa não aparece aqui."
    _, state = _STATE.get(entry.state, _STATE["playing"])
    track = md_link(entry.title, entry.url, limit=60) if entry.title else "`sem faixa`"
    line = f"-# {state}: {track}"
    details = []
    if entry.requester:
        details.append(f"✋ {md_code(entry.requester, limit=32)}")
    if entry.queue:
        details.append(f"📑 {entry.queue} na fila")
    if details:
        line += "\n-# " + " · ".join(details)
    return line[:1024]


def _buttons(bot: Any, *, paused: bool) -> list[disnake.ui.Button]:
    """Os botões comuns: pedir, pausar/retomar, pular, parar e favoritos; o estado de pausa vem de todos os players."""
    return [
        disnake.ui.Button(
            label="Pedir uma música",
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
    playlist_options: Sequence[disnake.SelectOption] = (),
    last_action: str = "",
) -> dict[str, Any]:
    """A mensagem do painel com 2+ players: ``{"content", "embeds", "components"}`` (quem envia põe ``AllowedMentions.none()``)."""
    embed = disnake.Embed(title=TITLE, color=bot.get_color(guild.me))
    embed.description = (
        f"Entre em um canal de voz e peça uma música aqui no canal (ou pelo botão abaixo). "
        f"**{len(entries)} players ativos** neste servidor:"
    )
    for entry in entries[:25]:  # um embed tem no máximo 25 campos
        embed.add_field(name=_header(entry), value=_body(entry), inline=False)
    footer = FOOTER
    if last_action:
        footer = f"Última ação: {last_action.replace('**', '')[:LAST_ACTION_MAX]}\n{FOOTER}"
    embed.set_footer(text=footer)
    embed.set_thumbnail(guild.me.display_avatar.replace(size=256).url)

    components: list[Any] = [
        disnake.ui.ActionRow(*_buttons(bot, paused=all(e.state == PAUSED for e in entries) if entries else False))
    ]
    if playlist_options:
        components.append(
            disnake.ui.ActionRow(
                disnake.ui.Select(
                    placeholder="Playlists do servidor (toca na sua call).",
                    options=list(playlist_options)[:25],
                    custom_id="player_guild_pin",
                    min_values=0,
                    max_values=1,
                )
            )
        )
    return {"content": None, "embeds": [embed], "components": components}
