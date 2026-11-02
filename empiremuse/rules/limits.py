"""Constantes de limite e de conjunto permitido, usadas pelos comandos e pelo painel."""

from __future__ import annotations

# Favoritos pessoais (``fav_links`` do usuário).
FAV_NAME_MIN, FAV_NAME_MAX = 2, 25
FAV_URL_MIN, FAV_URL_MAX = 10, 200

# Integrações pessoais (``integration_links`` do usuário).
ITG_TITLE_MAX = 90
ITG_URL_MAX = 200  # o mesmo teto do modal de integração (o importador aceita tudo que o modal salva)
ITG_AVATAR_MAX = 250

# Links fixos do servidor (``player_controller.fav_links``).
MAX_GUILD_LINKS = 25
GUILD_LINK_NAME_MIN, GUILD_LINK_NAME_MAX = 2, 25
GUILD_LINK_URL_MIN, GUILD_LINK_URL_MAX = 10, 250
GUILD_LINK_DESCRIPTION_MIN, GUILD_LINK_DESCRIPTION_MAX = 3, 100

# Playlists do servidor (seção ``playlists`` do documento único do servidor).
MAX_SERVER_PLAYLISTS = 25
PLAYLIST_NAME_MIN, PLAYLIST_NAME_MAX = 2, 32
PLAYLIST_DESCRIPTION_MIN, PLAYLIST_DESCRIPTION_MAX = 3, 250
PLAYLIST_URL_MIN, PLAYLIST_URL_MAX = 10, 250
PLAYLIST_GENRES_MAX = 8
PLAYLIST_GENRE_MIN, PLAYLIST_GENRE_MAX = 2, 32
PLAYLIST_ORDER_MAX = 9999
PLAYLIST_ICON_MAX = 16  # pontos de código de UM emoji (a sequência ZWJ mais longa tem 10)
PLAYLIST_FILE_MAX_BYTES = 64 * 1024
PLAYLIST_FILE_MAX_ITEMS = 200
PLAYLIST_PASTE_MAX = 4000
PLAYLIST_SELECT_DESCRIPTION_MAX = 100  # o select do Discord corta a descrição em 100
PLAYLIST_REPORT_MAX = 2000

# Prefixos (servidor e pessoal).
PREFIX_MAX_LENGTH = 5

# Status automático do canal de voz.
VOICE_STATUS_MAX_LENGTH = 496
VOICE_PLACEHOLDERS = (
    "{track.title}",
    "{track.timestamp}",
    "{track.emoji}",
    "{track.author}",
    "{track.duration}",
    "{track.source}",
    "{track.playlist}",
    "{requester.name}",
    "{requester.id}",
)

# Player: 5…100 de 5 em 5 e 110…150 de 10 em 10.
VOLUMES = tuple(range(5, 101, 5)) + tuple(range(110, 151, 10))
MAX_DJ_ROLES = 25

# Skins: prefixo do valor que representa uma skin personalizada do servidor.
CUSTOM_SKIN_PREFIX = "> custom_skin: "

# Jukebox: título do post de fórum.
FORUM_TITLE_MIN, FORUM_TITLE_MAX = 4, 30
