# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime
import json
import logging
import re
import unicodedata
from typing import Union, TYPE_CHECKING
from urllib.parse import urlsplit

import disnake

from empiremuse.rules.urls import URL_REG as URL_REG  # reexportado: cogs e testes importam daqui
from empiremuse.utils.branding import asset_url

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

YOUTUBE_VIDEO_REG = re.compile(r"(https?://)?(www\.|music\.)?youtube\.(com|nl)/watch\?v=([-\w]+)")

replaces = [
    ("&quot;", '"'),
    ("&amp;", "&"),
    ("(", "\u0028"),
    (")", "\u0029"),
    ("[", "【"),
    ("]", "】"),
    ("  ", " "),
    ("*", '"'),
    ("_", " "),
    ("{", "\u0028"),
    ("}", "\u0029"),
    ("`", "'"),
]


async def google_search(bot, query: str, *, max_entries: int = 20) -> list:
    try:
        async with bot.session.get(
            "https://suggestqueries.google.com/complete/search",
            headers={"User-Agent": bot.pool.current_useragent} if bot.pool.current_useragent else None,
            params={"client": "youtube", "q": query, "ds": "yt", "hl": "en"},
        ) as r:
            text = await r.text()
            json_text = text[text.find("(") + 1 : text.rfind(")")]
            return [result[0] for result in json.loads(json_text)[1][:max_entries]]
    except Exception:
        logger.exception("Falha na busca de sugestões do Google")
        return []


def get_button_style(enabled: bool, red=True):
    if enabled:
        if red:
            return disnake.ButtonStyle.red
        return disnake.ButtonStyle.green
    return disnake.ButtonStyle.grey


def fix_characters(text: str, limit: int = 0):
    for r in replaces:
        old, new = r
        text = text.replace(old, new)

    if limit:
        text = f"{text[:limit]}..." if len(text) > limit else text

    return text


_URL_ENCODE_RE = re.compile(r"[()<>`\\\x00-\x20\x7f]")
_URL_SCHEMES = {"http", "https"}
# Controles de direção e caracteres de largura zero que permitem falsificar o texto exibido; o ZWJ (U+200D) e o ZWNJ (U+200C) ficam,
# porque compõem emojis e escritas legítimas.
_SPOOFING_RE = re.compile("[\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_LINE_BREAK_RE = re.compile(r"\r\n|[\r\n\u2028\u2029]")


def _clean_label(text: str) -> str:
    return _LINE_BREAK_RE.sub(" ", _SPOOFING_RE.sub("", text))


def _percent_encode(char: str) -> str:
    return "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))


def _has_web_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in _URL_SCHEMES and bool(parts.hostname) and parts.username is None and parts.password is None
        )
    except ValueError:
        return False


def md_url(url: object) -> str | None:
    """URL http/https com host, pronta para ficar dentro de ``(<...>)``; ``None`` se não for uma."""
    if not isinstance(url, str) or not _has_web_host(url):
        return None

    encoded = "".join(
        _percent_encode(char) if _URL_ENCODE_RE.match(char) or unicodedata.category(char) == "Cf" else char
        for char in url
    )

    return encoded if _has_web_host(encoded) else None


def md_code(text: object, *, limit: int = 0) -> str:
    """Code span com texto externo, sem como fechá-lo por dentro."""
    return f"`{fix_characters(_clean_label(str(text)), limit) or '?'}`"


def md_bracketed(inner: str, *, prefix: str = "") -> str:
    """``[inner]`` em code spans, para conteúdo já seguro (menção, timestamp, link de ``md_link``) do próprio bot.

    ``prefix`` (também do próprio bot) fica dentro do primeiro code span, antes do ``[``.
    """
    return f"`{prefix}[`{inner}`]`"


def md_link(text: object, url: object, *, code: bool = True, limit: int = 0) -> str:
    """Link mascarado ``[`texto`](<url>)`` com texto e URL externos neutralizados; sem URL válida sai só o rótulo."""
    label = fix_characters(_clean_label(str(text)), limit) or "?"

    if not code:
        label = disnake.utils.escape_markdown(label)

    label = f"`{label}`" if code else label
    safe_url = md_url(url)

    return f"[{label}](<{safe_url}>)" if safe_url else label


def time_format(milliseconds: Union[int, float], use_names: bool = False) -> str:
    minutes, seconds = divmod(int(milliseconds / 1000), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    if use_names:
        times = []

        for time_, name in ((days, "dia"), (hours, "hora"), (minutes, "minuto"), (seconds, "segundo")):
            if not time_:
                continue

            times.append(f"{time_} {name}" + ("s" if time_ > 1 else ""))

        try:
            last_time = times.pop()
        except IndexError:
            last_time = None
            times = ["1 segundo"]

        strings = ", ".join(t for t in times)

        if last_time:
            strings += f" e {last_time}" if strings else last_time

    else:
        strings = f"{minutes:02d}:{seconds:02d}"

        if hours:
            strings = f"{hours}:{strings}"

        if days:
            strings = (f"{days} dias" if days > 1 else f"{days} dia") + (f", {strings}" if strings != "00:00" else "")

    return strings


time_names = ["seconds", "minutes", "hours"]


def string_to_seconds(time):
    try:
        times = reversed([i for i in time.replace(" ", ":").split(":") if i.isdigit()])
        time_dict = {}

        for n, t in enumerate(times):
            time_dict[time_names[n]] = int(t)

        return datetime.timedelta(**time_dict).total_seconds()

    except Exception:
        return


def percentage(part, whole):
    return int((part * whole) / 100.0)


sources = {
    "deezer": asset_url("provider_deezer_icon.png"),
    "soundcloud": asset_url("provider_soundcloud_icon.png"),
    "spotify": asset_url("provider_spotify_icon.png"),
    "youtube": asset_url("provider_youtube_icon.png"),
    "applemusic": asset_url("provider_applemusic_icon.png"),
    "twitch": asset_url("provider_twitch_icon.png"),
    "jiosaavn": asset_url("provider_jiosaavn_icon.png"),
    "tidal": asset_url("provider_tidal_icon.png"),
    "youtubemusic": asset_url("provider_youtubemusic_icon.png"),
    "last.fm": asset_url("provider_lastfm_emoji.png"),
}


def music_source_image(sourcename):
    return sources.get(sourcename, asset_url("player_equalizer_1.gif"))


perms_translations = {
    "add_reactions": "Adicionar Reações",
    "administrator": "Administrador",
    "attach_files": "Anexar Arquivos",
    "ban_members": "Banir Membros",
    "change_nickname": "Alterar Apelido",
    "connect": "Conectar em Canal de Voz",
    "create_instant_invite": "Criar Convite Instantâneo",
    "create_private_threads": "Criar Tópicos Privados",
    "create_public_threads": "Criar Tópicos Públicos",
    "deafen_members": "Ensurdecer Membros",
    "embed_links": "Inserir Links",
    "kick_members": "Expulsar Membros",
    "manage_channels": "Gerenciar Canais",
    "manage_emojis_and_stickers": "Gerenciar Emojis e Figurinhas",
    "manage_events": "Gerenciar Eventos",
    "manage_guild": "Gerenciar Servidor",
    "manage_messages": "Gerenciar Mensagens",
    "manage_nicknames": "Gerenciar Apelidos",
    "manage_roles": "Gerenciar Cargos",
    "manage_threads": "Gerenciar Tópicos",
    "manage_webhooks": "Gerenciar Webhooks",
    "mention_everyone": "Marcar @everyone e @here",
    "moderate_members": "Moderar Membros",
    "move_members": "Mover Membros",
    "mute_members": "Silenciar Membros",
    "priority_speaker": "Prioridade para Falar",
    "read_message_history": "Mostrar Histórico de Mensagens",
    "read_messages": "Ler Mensagens",
    "request_to_speak": "Pedir para Falar",
    "send_messages": "Enviar Mensagem",
    "send_messages_in_threads": "Enviar Mensagem em Tópicos",
    "send_tts_messages": "Enviar Mensagens de Texto-a-Voz",
    "speak": "Falar em Canal de Voz",
    "stream": "Transmitir",
    "use_application_commands": "Usar Comandos de Aplicações/Bots",
    "use_embedded_activities": "Usar Atividades",
    "use_external_emojis": "Usar Emojis Externos",
    "use_external_stickers": "Usar Figurinhas Externas",
    "use_voice_activation": "Usar Detecção de Voz Automática",
    "view_audit_log": "Visualizar Registro de Auditoria",
    "view_channel": "Ver Canal",
    "view_guild_insights": "Ver Análises do Servidor",
}
