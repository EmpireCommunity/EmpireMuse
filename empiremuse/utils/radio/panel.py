"""As duas mensagens de controle de rádio no chat da própria call (item 8): a AO VIVO e a PERSONALIZADA, separadas e no estilo da Jukebox.

São duas funcionalidades distintas e não se misturam: a ao vivo é só uma estação do Radio Browser (nome, o que está tocando agora, reserva, trocar de
estação por região, desligar); a personalizada é uma playlist do servidor tocada como se fosse uma rádio (pausar, pular, desligar). Nenhuma das duas
tem pedido por busca, link, fila nem favoritos: isso é da Jukebox. O visual segue o painel da Jukebox (embed com campo por player, ``-#`` nas linhas
miúdas, ícones da mnauliady nos botões, miniatura, rodapé).

Os ``custom_id`` são fixos (``rules.radio.PANEL_IDS``) e NÃO levam rádio, bot nem servidor: quem trata o clique acha a rádio por ``interaction.guild``
mais o bot que recebeu a interação. Todo texto de fora (estação, título do fluxo, playlist) passa pela função única de texto externo (R-11).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple, Optional

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.radio import (
    PANEL_MENU_ID,
    PANEL_MUTE_ID,
    PANEL_NEXT_ID,
    PANEL_PAUSE_ID,
    PANEL_PREVIOUS_ID,
    PANEL_STOP_ID,
    PANEL_VOLUME_DOWN_ID,
    PANEL_VOLUME_UP_ID,
)
from empiremuse.rules.text import external_text
from empiremuse.utils.music.app_emojis import APP_EMOJIS

GENERIC_ICON = Path(__file__).resolve().parents[3] / "assets" / "radio" / "global.png"
LOGO_NAME = "logo.png"  # o nome do anexo que a miniatura do embed aponta (attachment://logo.png)

NAME_LIMIT = 80
COUNTRY_LIMIT = 40
TITLE_LIMIT = 150
LABEL_LIMIT = 80
FIELD_LIMIT = 1024

Emojis = Callable[[str], str]


def fallback_emojis(name: str) -> str:
    """Os Unicode de reserva dos ícones (quem tem o bot usa ``app_emojis.emoji(bot, nome)``)."""
    return APP_EMOJIS[name]


def is_radio_panel(message: Any) -> bool:
    """A mensagem é (ou foi) um painel de rádio? Tem um botão ou select com um dos ``custom_id`` fixos do painel. Serve para achar painéis órfãos."""
    from empiremuse.rules.radio import PANEL_IDS

    ours = {*PANEL_IDS, PANEL_MENU_ID}
    for row in getattr(message, "components", None) or ():
        for child in getattr(row, "children", None) or ():
            if getattr(child, "custom_id", None) in ours:
                return True
    return False


@cache
def generic_icon() -> Optional[bytes]:
    """O ícone genérico de rádio internacional (``assets/radio/global.png``) para a estação sem logo; ``None`` se o arquivo faltar (vale o avatar do bot)."""
    try:
        return GENERIC_ICON.read_bytes()
    except OSError:
        return None


class LiveView(NamedTuple):
    """O que o painel ao vivo mostra além da estação: o título do fluxo (já limpo pela R-11), se a reserva está no ar, se está fora do ar, se está
    silenciada e se há uma lista de estações por onde andar (anterior e próxima)."""

    title: Optional[str] = None
    on_reserve: bool = False
    offline: bool = False
    has_list: bool = False
    muted: bool = False


class StationCard(NamedTuple):
    """A estação no ar: nome, país, codec e taxa (já limpos pela R-11)."""

    name: object = "—"
    country: object = ""
    codec: object = ""
    bitrate: int = 0


def split_stream_title(raw: object) -> tuple[str, Optional[str]]:
    """O título do fluxo vem como ``Artista - Música``: devolve ``(música, artista)``. Sem o separador, tudo é a música e o artista é ``None``."""
    text = raw.strip() if isinstance(raw, str) else ""
    artist, separator, song = text.partition(" - ")
    if separator and artist.strip() and song.strip():
        return song.strip(), artist.strip()
    return text, None


def _button(tr: Any, emojis: Emojis, key: str, icon: str, custom_id: str, style: Any = None) -> disnake.ui.Button:
    return disnake.ui.Button(
        label=fit(tr(key), LABEL_LIMIT),
        emoji=emojis(icon),
        custom_id=custom_id,
        **({"style": style} if style is not None else {}),
    )


def _live_text(tr: Any, station: StationCard, view: LiveView) -> str:
    """O corpo do painel: ``{estação} – {país}``; ``Tocando agora: {música} – {artista}`` só quando o fluxo mandou um título (nunca "desconhecido")."""
    name = external_text(station.name, NAME_LIMIT)
    country = external_text(station.country, COUNTRY_LIMIT)
    lines = [f"**{name} – {country}**" if country else f"**{name}**"]
    if view.title and not view.offline and not view.muted:
        song, artist = split_stream_title(view.title)
        if artist:
            lines.append(
                tr(
                    "radio.panel.live.now",
                    song=external_text(song, TITLE_LIMIT),
                    artist=external_text(artist, TITLE_LIMIT),
                )
            )
        else:
            lines.append(tr("radio.panel.live.now_song", song=external_text(song, TITLE_LIMIT)))
    if view.muted:
        lines.append(f"-# {tr('radio.panel.live.muted')}")
    elif view.offline:
        lines.append(f"-# {tr('radio.live.panel_offline')}")
    elif view.on_reserve:
        lines.append(f"-# {tr('radio.live.panel_reserve')}")
    return fit("\n".join(lines), 4000)


def _menu(tr: Any, emojis: Emojis) -> disnake.ui.Select:
    """O select "Mais opções": trocar de estação, favoritar no servidor, volume, informações e desligar."""
    options = [
        ("stations", "radio.panel.menu.stations", "search"),
        ("favorite", "radio.panel.menu.favorite", "star"),
        ("volume", "radio.panel.menu.volume", "volume"),
        ("info", "radio.panel.menu.info", "notification"),
        ("stop", "radio.panel.menu.stop", "stop"),
    ]
    return disnake.ui.Select(
        placeholder=fit(tr("radio.panel.menu.placeholder"), 150),
        options=[
            disnake.SelectOption(label=fit(tr(key), LABEL_LIMIT), value=value, emoji=emojis(icon))
            for value, key, icon in options
        ],
        custom_id=PANEL_MENU_ID,
        min_values=1,
        max_values=1,
    )


def live_payload(
    tr: Any,
    emojis: Emojis = fallback_emojis,
    *,
    station: Optional[StationCard] = None,
    view: Optional[LiveView] = None,
    color: int = 0,
    avatar: Optional[str] = None,
    logo: Optional[bytes] = None,
) -> dict[str, Any]:
    """O painel da rádio AO VIVO: ``{"embeds", "components"}`` e, com logo (ou o ícone genérico), ``"file"`` que a miniatura aponta. Quem envia põe
    ``AllowedMentions.none()`` (e, ao editar, ``attachments=[]`` para trocar o anexo)."""
    station = station or StationCard()
    view = view or LiveView()
    embed = disnake.Embed(title=tr("radio.panel.live.title"), description=_live_text(tr, station, view), color=color)
    embed.set_footer(text=tr("radio.panel.live.footer"))
    payload: dict[str, Any] = {}
    picture = logo or generic_icon()
    if picture:
        embed.set_thumbnail(url=f"attachment://{LOGO_NAME}")
        payload["file"] = disnake.File(io.BytesIO(picture), filename=LOGO_NAME)
    elif avatar:
        embed.set_thumbnail(url=avatar)
    buttons: list[disnake.ui.Button] = []
    if (
        view.has_list
    ):  # sem uma lista de onde a estação foi escolhida não há anterior nem próxima: os botões nem aparecem
        buttons.append(_button(tr, emojis, "radio.panel.previous", "backward", PANEL_PREVIOUS_ID))
    buttons.append(_button(tr, emojis, "radio.panel.volume_down", "volume", PANEL_VOLUME_DOWN_ID))
    buttons.append(
        _button(
            tr,
            emojis,
            "radio.panel.unmute" if view.muted else "radio.panel.mute",
            "pause" if not view.muted else "play",
            PANEL_MUTE_ID,
            disnake.ButtonStyle.danger if view.muted else None,
        )
    )
    buttons.append(_button(tr, emojis, "radio.panel.volume_up", "volume", PANEL_VOLUME_UP_ID))
    if view.has_list:
        buttons.append(_button(tr, emojis, "radio.panel.next", "skip", PANEL_NEXT_ID))
    payload["embeds"] = [embed]
    payload["components"] = [disnake.ui.ActionRow(*buttons), disnake.ui.ActionRow(_menu(tr, emojis))]
    return payload


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
    logo: Optional[bytes] = None,
) -> dict[str, Any]:
    """O painel certo para o tipo da rádio: ``live`` escolhe entre ``live_payload`` e ``custom_payload``."""
    if live:
        card = station if station is not None else StationCard(name=playlist_name)
        return live_payload(tr, emojis, station=card, view=view, color=color, avatar=avatar, logo=logo)
    return custom_payload(tr, emojis, playlist_name=playlist_name, color=color, avatar=avatar)
