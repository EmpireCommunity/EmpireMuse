"""As duas mensagens de controle de rádio no chat da própria call (item 8): a AO VIVO e a PERSONALIZADA, separadas e no estilo da Jukebox.

São duas funcionalidades distintas e não se misturam: a ao vivo é só uma estação do Radio Browser (nome, o que está tocando agora, reserva, trocar de
estação por região, desligar); a personalizada é uma playlist do servidor tocada como se fosse uma rádio (pausar, pular, desligar). Nenhuma das duas
tem pedido por busca, link, fila nem favoritos: isso é da Jukebox. O visual segue o painel da Jukebox (embed com campo por player, ``-#`` nas linhas
miúdas, ícones da mnauliady nos botões, miniatura, rodapé).

Os ``custom_id`` são fixos (``rules.radio.PANEL_IDS``) e NÃO levam rádio, bot nem servidor: quem trata o clique acha a rádio por ``interaction.guild``
mais o bot que recebeu a interação. Todo texto de fora (estação, título do fluxo, playlist) passa pela função única de texto externo (R-11).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple, Optional

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.radio import (
    PANEL_NEXT_ID,
    PANEL_PAUSE_ID,
    PANEL_RESERVE_ID,
    PANEL_STATIONS_ID,
    PANEL_STOP_ID,
)
from empiremuse.rules.text import external_text
from empiremuse.utils.music.app_emojis import APP_EMOJIS

NAME_LIMIT = 80
TITLE_LIMIT = 150
DETAIL_LIMIT = 100
LABEL_LIMIT = 80
FIELD_LIMIT = 1024

Emojis = Callable[[str], str]


def fallback_emojis(name: str) -> str:
    """Os Unicode de reserva dos ícones (quem tem o bot usa ``app_emojis.emoji(bot, nome)``)."""
    return APP_EMOJIS[name]


class LiveView(NamedTuple):
    """O que o painel ao vivo mostra além da estação: o título do fluxo (já limpo pela R-11), se a reserva está no ar, se está fora do ar e se há reserva."""

    title: Optional[str] = None
    on_reserve: bool = False
    offline: bool = False
    has_reserve: bool = False


class StationCard(NamedTuple):
    """A estação no ar: nome, uma linha de detalhe (codec, taxa, país) e o ``favicon`` que já passou no ``https`` + ``is_clean_url`` ('' = sem)."""

    name: object = "—"
    detail: object = ""
    favicon: str = ""


def _button(tr: Any, emojis: Emojis, key: str, icon: str, custom_id: str, style: Any = None) -> disnake.ui.Button:
    return disnake.ui.Button(
        label=fit(tr(key), LABEL_LIMIT),
        emoji=emojis(icon),
        custom_id=custom_id,
        **({"style": style} if style is not None else {}),
    )


def _live_lines(tr: Any, emojis: Emojis, station: StationCard, view: LiveView) -> tuple[str, str]:
    """``(nome do campo, valor)`` do painel ao vivo."""
    if view.offline:
        status = tr("radio.live.panel_offline")
    elif view.on_reserve:
        status = tr("radio.live.panel_reserve")
    else:
        status = tr("radio.live.panel_live")
    lines = [f"-# {status}"]
    if not view.offline:
        if view.title:
            lines.append(
                f"-# {emojis('music_note')} {tr('radio.live.panel_now_title', title=external_text(view.title, TITLE_LIMIT))}"
            )
        else:
            lines.append(f"-# {emojis('music_note')} {tr('radio.live.panel_waiting_title')}")
    if detail := external_text(station.detail, DETAIL_LIMIT):
        lines.append(f"-# {emojis('speaker')} {detail}")
    name = f"📻 {external_text(station.name, NAME_LIMIT)}"
    return fit(name, 256), fit("\n".join(lines), FIELD_LIMIT)


def live_payload(
    tr: Any,
    emojis: Emojis = fallback_emojis,
    *,
    station: Optional[StationCard] = None,
    view: Optional[LiveView] = None,
    color: int = 0,
    avatar: Optional[str] = None,
) -> dict[str, Any]:
    """O painel da rádio AO VIVO: ``{"embeds", "components"}``; quem envia põe ``AllowedMentions.none()``."""
    station = station or StationCard()
    view = view or LiveView()
    embed = disnake.Embed(title=tr("radio.panel.live.title"), description=tr("radio.panel.live.intro"), color=color)
    name, value = _live_lines(tr, emojis, station, view)
    embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=tr("radio.panel.live.footer"))
    if thumb := station.favicon or avatar:
        embed.set_thumbnail(url=thumb)
    buttons = [
        _button(tr, emojis, "radio.panel.pause", "playpause", PANEL_PAUSE_ID),
        _button(tr, emojis, "radio.panel.stations", "search", PANEL_STATIONS_ID),
    ]
    if view.has_reserve:  # sem reserva não há o que trocar: o botão nem aparece
        key = (
            "radio.panel.primary" if view.on_reserve else "radio.panel.reserve"
        )  # na reserva o botão leva de volta à principal
        buttons.append(_button(tr, emojis, key, "leftwards_arrow_with_hook", PANEL_RESERVE_ID))
    buttons.append(_button(tr, emojis, "radio.panel.stop", "stop", PANEL_STOP_ID, disnake.ButtonStyle.danger))
    return {"embeds": [embed], "components": [disnake.ui.ActionRow(*buttons)]}


def custom_payload(
    tr: Any,
    emojis: Emojis = fallback_emojis,
    *,
    playlist_name: object = "",
    color: int = 0,
    avatar: Optional[str] = None,
) -> dict[str, Any]:
    """O painel da rádio PERSONALIZADA (playlist do servidor): pausar, pular e desligar, e nada de pedido, link ou fila."""
    embed = disnake.Embed(title=tr("radio.panel.custom.title"), description=tr("radio.panel.custom.intro"), color=color)
    lines = [
        f"-# {emojis('playlists')} {tr('radio.panel.custom.playlist', playlist=external_text(playlist_name, NAME_LIMIT))}",
        f"-# {emojis('history')} {tr('radio.panel.custom.schedule')}",
    ]
    embed.add_field(
        name=fit(f"▶️ {external_text(playlist_name, NAME_LIMIT)}", 256), value="\n".join(lines), inline=False
    )
    embed.set_footer(text=tr("radio.panel.custom.footer"))
    if avatar:
        embed.set_thumbnail(url=avatar)
    buttons = [
        _button(tr, emojis, "radio.panel.pause", "playpause", PANEL_PAUSE_ID),
        _button(tr, emojis, "radio.panel.next", "skip", PANEL_NEXT_ID),
        _button(tr, emojis, "radio.panel.stop", "stop", PANEL_STOP_ID, disnake.ButtonStyle.danger),
    ]
    return {"embeds": [embed], "components": [disnake.ui.ActionRow(*buttons)]}


def panel_payload(
    tr: Any,
    *,
    playlist_name: object = "",
    color: int = 0,
    live: bool = False,
    view: Optional[LiveView] = None,
    station: Optional[StationCard] = None,
    emojis: Emojis = fallback_emojis,
    avatar: Optional[str] = None,
) -> dict[str, Any]:
    """O painel certo para o tipo da rádio: ``live`` escolhe entre ``live_payload`` e ``custom_payload``."""
    if live:
        card = station if station is not None else StationCard(name=playlist_name)
        return live_payload(tr, emojis, station=card, view=view, color=color, avatar=avatar)
    return custom_payload(tr, emojis, playlist_name=playlist_name, color=color, avatar=avatar)
