# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional

from empiremuse.rules.errors import (
    DESCRIPTION_INVALID,
    DESCRIPTION_TOO_LONG,
    FILE_TOO_LARGE,
    ITEM_FORMAT,
    NAME_TAKEN,
    NAME_TOO_LONG,
    QUOTA_FULL,
    URL_TOO_LONG,
    RuleError,
)
from empiremuse.utils.music.audio_sources.deezer import deezer_regex
from empiremuse.utils.music.audio_sources.spotify import spotify_regex_w_user
from empiremuse.utils.music.converters import time_format

from empiremuse.utils.music.interactions.common import (
    IMPORT_ECHO_LIMIT,
    soundcloud_regex,
    youtube_regex,
)


def _echo(value, limit: int = IMPORT_ECHO_LIMIT) -> str:
    """Trecho de um valor do arquivo importado para mensagens: curto e sem crases (não fecha o bloco de código)."""
    text = str(value).replace("`", "'")
    return text if len(text) <= limit else f"{text[:limit]}…"


def _invalid_link_text(tr, value) -> str:
    return tr("fav.file.invalid_link", value=_echo(value))


def _invalid_name_text(tr, name, low: int, high: int) -> str:
    return tr("fav.file.invalid_name", name=_echo(name, 40), low=low, high=high)


def _url_error_text(tr, error: RuleError, *, with_label: bool = False) -> str:
    """Mensagem de uma URL de arquivo recusada: longa demais (cita o limite) ou inválida."""
    if error.code == URL_TOO_LONG:
        key = "fav.file.url_too_long_labeled" if with_label else "fav.file.url_too_long"
        return tr(key, value=_echo(error.value), limit=error.limit)
    return _invalid_link_text(tr, error.value)


def _name_error_text(tr, error: RuleError, low: int, high: int) -> str:
    """Mensagem de um nome de arquivo recusado: acima do teto da configuração ou inválido/repetido."""
    if error.code == NAME_TOO_LONG:
        return tr("fav.file.name_too_long", value=_echo(error.value), limit=error.limit)
    return _invalid_name_text(tr, error.value, low, high)


def _save_error_text(tr, error: RuleError, low: int, high: int, taken: str) -> str:
    """Mensagem de uma recusa ao salvar um favorito/link pelo modal (``taken``: texto do nome repetido)."""
    if error.code == NAME_TAKEN:
        return f"**{taken}**"
    if error.code == QUOTA_FULL:
        return tr("fav.save.quota_full")
    return tr("fav.save.invalid_name", low=low, high=high)


# chaves (arquivo grande demais, sem espaço) por tipo de importação: favoritos do membro e integrações
_IMPORT_QUOTA_KEYS = {  # ``_IMPORT_QUOTA_TEXTS`` abaixo é o nome antigo, reexportado pelo pacote
    "fav": ("fav.import.quota.too_large", "fav.import.quota.no_space"),
    "itg": ("fav.itg.import.quota.too_large", "fav.itg.import.quota.no_space"),
}
_IMPORT_QUOTA_TEXTS = _IMPORT_QUOTA_KEYS


def _import_quota_text(tr, error: RuleError, kind: str) -> str:
    """Mensagem de cota de uma importação (``kind``: ``fav`` ou ``itg``)."""
    too_large, no_space = _IMPORT_QUOTA_KEYS[kind]
    if error.code == FILE_TOO_LARGE:
        return tr(too_large, limit=error.limit)
    kept, incoming = error.value
    return tr(no_space, limit=error.limit, kept=kept, need=kept + incoming - error.limit)


_NO_ROOM_INTEGRATION_TEXT = "fav.itg.quota_full"  # a chave: o texto sai de ``tr(_NO_ROOM_INTEGRATION_TEXT)``


def _retry_text(tr, retry_after: float) -> str:
    """Mensagem neutra do cooldown ao adicionar uma integração (o balde é o do importador, mas não é uma importação)."""
    return tr("fav.itg.retry", time=time_format(int(max(retry_after, 1)) * 1000, use_names=True))


def _wait_text(tr, retry_after: float) -> str:
    """Mensagem do cooldown por membro do importador (a do exportador e a do modal de integração são outras)."""
    return tr("fav.import.wait", time=time_format(int(max(retry_after, 1)) * 1000, use_names=True))


def _profile_source(url: str) -> Optional[str]:
    """De onde vem o perfil do link (``spotify``, ``deezer``, ``youtube`` ou ``soundcloud``), ou ``None`` se não é suportado."""
    if spotify_regex_w_user.match(url):
        return "spotify"
    if deezer_regex.match(url):
        return "deezer"
    if youtube_regex.search(url):
        return "youtube"
    if soundcloud_regex.search(url):
        return "soundcloud"
    return None


def _profile_key(url) -> Optional[tuple]:
    """Identifica o perfil de um link (fonte e id), para saber se duas URLs diferentes são do mesmo perfil."""
    if not isinstance(url, str):
        return None
    if match := spotify_regex_w_user.match(url):
        return "spotify", match.groups()[-1]
    if match := deezer_regex.match(url):
        return "deezer", match.groups()[-1]
    if match := youtube_regex.search(url):
        return "youtube", match.group(0).lower()
    if match := soundcloud_regex.search(url):
        return "soundcloud", match.group(1).lower()
    return None


def _profile_saved(entries: dict, url: str) -> bool:
    """Alguma integração já salva é do mesmo perfil do link (gravar de novo sobrescreve, não ocupa vaga)?"""
    key = _profile_key(url)
    return key is not None and any(
        _profile_key(entry.get("url") if isinstance(entry, dict) else entry) == key for entry in entries.values()
    )


def _spotify_profile(result: dict, user_id: str, url: str) -> dict:
    """Título, URL e avatar da resposta do Spotify; perfil sem nome, sem imagem ou sem link não quebra (N-4)."""
    name = str(result.get("display_name") or "").strip() or user_id
    profile = {"title": f"[SP]: {name[:90]}", "url": (result.get("external_urls") or {}).get("spotify") or url}
    images = result.get("images") or []
    if images and isinstance(images[-1], dict) and images[-1].get("url"):
        profile["avatar"] = images[-1]["url"]
    return profile
