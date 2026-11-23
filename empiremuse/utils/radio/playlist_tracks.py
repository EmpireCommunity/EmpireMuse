"""As faixas das playlists de escopo rádio de um servidor, para o pool de uma estação ``playlists`` (a rádio 24 h e a estação da Jukebox).

A URL exata da playlist salva (``source=False``: sem ``ytsearch:`` nem Deezer/Spotify), no nó do player quando há. Uma playlist que falha ao carregar
é pulada (só o tipo do erro vai ao log): as outras ainda formam o pool.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from empiremuse.rules.playlists import SCOPE_RADIO
from empiremuse.rules.radio_shuffle import Track

logger = logging.getLogger(__name__)


async def radio_playlist_tracks(
    pool: Any, bot: Any, guild: Any, channel: Any, node: Any, names: Sequence[str]
) -> list[Track]:
    music = bot.get_cog("Music") if bot is not None else None
    if music is None or guild is None:
        return []
    shim = SimpleNamespace(guild=guild, guild_id=guild.id, author=guild.me, bot=bot, channel=channel)
    found: list[Track] = []
    for name in names:
        playlist = await pool.servers.playlists.find(guild.id, name, scope=SCOPE_RADIO)
        if playlist is None:
            continue
        try:
            result, _ = await music.get_tracks(playlist.url, shim, guild.me, source=False, node=node, bot=bot)
        except Exception as error:  # noqa: BLE001 - uma playlist quebrada não derruba o pool inteiro
            logger.warning("Playlist de rádio não carregada (%s)", type(error).__name__)
            continue
        for track in list(getattr(result, "tracks", result) or []):
            author, title = str(getattr(track, "author", "") or ""), str(getattr(track, "title", "") or "")
            if author.strip() and title.strip():
                found.append(Track(author, title))
    return found
