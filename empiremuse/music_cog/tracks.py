# -*- coding: utf-8 -*-
import re
import traceback
from typing import Union
from urllib.parse import quote

import disnake

import empiremuse.wavelink as wavelink
from empiremuse.utils.client import BotCore
from empiremuse.utils.music.audio_sources.spotify import spotify_link_regex, spotify_regex_w_user
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.models import (
    LavalinkTrack,
    LavalinkPlaylist,
    PartialTrack,
    PartialPlaylist,
)
from empiremuse.utils.others import (
    CustomContext,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    sc_recommended,
)


class TracksMixin:
    async def get_partial_tracks(
        self,
        query: str,
        ctx: Union[disnake.ApplicationCommandInteraction, CustomContext, disnake.MessageInteraction, disnake.Message],
        user: disnake.Member,
        node: wavelink.Node = None,
        bot: BotCore = None,
    ):
        if not bot:
            bot = self.bot

        tracks = []

        exceptions = set()

        if bot.pool.config["FORCE_USE_DEEZER_CLIENT"] or [
            n for n in bot.music.nodes.values() if "deezer" not in n.info.get("sourceManagers", [])
        ]:
            try:
                tracks = await self.bot.pool.deezer.get_tracks(
                    url=query, requester=user.id, search=True, check_title=80
                )
            except Exception as e:
                self.bot.dispatch("custom_error", ctx=ctx, error=e)
                exceptions.add(repr(e))

        if (
            not tracks
            and bot.spotify
            and not [n for n in bot.music.nodes.values() if "spotify" in n.info.get("sourceManagers", [])]
        ):
            try:
                tracks = await self.bot.pool.spotify.get_tracks(self.bot, user.id, query, search=True, check_title=80)
            except Exception as e:
                self.bot.dispatch("custom_error", ctx=ctx, error=e)
                exceptions.add(repr(e))

        return tracks, node, exceptions

    async def get_lavalink_tracks(
        self,
        query: str,
        ctx: Union[disnake.ApplicationCommandInteraction, CustomContext, disnake.MessageInteraction, disnake.Message],
        user: disnake.Member,
        node: wavelink.Node = None,
        source=None,
        bot: BotCore = None,
    ):
        if not bot:
            bot = self.bot

        if not node:
            nodes = sorted(
                [n for n in bot.music.nodes.values() if n.is_available and n.available], key=lambda n: len(n.players)
            )
        else:
            nodes = sorted(
                [n for n in bot.music.nodes.values() if n != node and n.is_available and n.available],
                key=lambda n: len(n.players),
            )
            nodes.insert(0, node)

        if not nodes:
            raise GenericError("**Não há servidores de música disponível!**")

        exceptions = set()

        tracks = []

        is_spotify_url = bool(spotify_regex_w_user.match(query) or spotify_link_regex.match(query))

        for n in nodes:
            if is_spotify_url and "spotify" not in n.info.get("sourceManagers", []):
                if not node:
                    node = n
                continue

            node_retry = False

            if source is False:
                providers = n.search_providers[:1]
                if query.startswith("https://www.youtube.com/live/"):
                    query = query.split("?")[0].replace("/live/", "/watch?v=")

                elif query.startswith("https://listen.tidal.com/album/") and "/track/" in query:
                    query = f"http://www.tidal.com/track/{query.split('/track/')[-1]}"

                elif query.startswith(("https://youtu.be/", "https://www.youtube.com/")):
                    for p in ("&ab_channel=", "&start_radio="):
                        if p in query:
                            try:
                                query = f"https://www.youtube.com/watch?v={re.search(r'v=([a-zA-Z0-9_-]+)', query).group(1)}"
                            except Exception:
                                pass
                            break
            elif source:
                providers = [s for s in n.search_providers if s != source]
                providers.insert(0, source)
            else:
                source = True
                providers = n.search_providers

            for search_provider in providers:
                tracks = None

                search_query = query

                if source:
                    if search_provider not in n.search_providers:
                        try:
                            if search_provider.startswith("dzsearch"):
                                tracks = await self.bot.pool.deezer.get_tracks(
                                    url=query, requester=user.id, search=True, check_title=50
                                )
                            elif search_provider.startswith("spsearch") and self.bot.pool.spotify:
                                tracks = await self.bot.pool.spotify.get_tracks(
                                    self.bot, user.id, query, search=True, check_title=50
                                )
                            else:
                                continue

                            if tracks:
                                return tracks, node, exceptions
                            else:
                                continue

                        except Exception as e:
                            self.bot.dispatch("custom_error", ctx=ctx, error=e)
                            exceptions.add(repr(e))
                            continue
                    else:
                        search_query = f"{search_provider}:{query}"

                try:
                    tracks = await n.get_tracks(
                        search_query,
                        track_cls=LavalinkTrack,
                        playlist_cls=LavalinkPlaylist,
                        requester=user.id,
                        # check_title=80
                    )
                except Exception as e:
                    # traceback.print_exc()
                    exceptions.add(repr(e))

                    if not isinstance(e, wavelink.TrackNotFound):
                        print(f"Falha ao processar busca...\n{query}\n{traceback.format_exc()}")
                        node_retry = True
                    elif not isinstance(e, GenericError):
                        self.bot.dispatch("custom_error", ctx=ctx, error=e)

                if tracks or not source:
                    break

            if not node_retry:
                node = n
                break

        return tracks, node, exceptions

    async def get_tracks(
        self,
        query: str,
        ctx: Union[disnake.ApplicationCommandInteraction, CustomContext, disnake.MessageInteraction, disnake.Message],
        user: disnake.Member,
        node: wavelink.Node = None,
        source=None,
        bot: BotCore = None,
        mix=False,
    ):
        exceptions = set()

        if mix:
            if not self.bot.pool.last_fm:
                raise GenericError(
                    "**No momento não há suporte a mix/recomendações devido o Last.fm não ter sido configurado na minha estrutura.**"
                )

            query = query.title()

            try:
                artist, track = query.split(" - ", 1)
            except Exception:
                try:
                    artist, track = query.split(" ", 1)
                except Exception:
                    raise GenericError("Você deve informar sua busca dessa forma: nome do artista - nome da música")

            current = None

            try:
                info = await self.bot.pool.last_fm.get_similar_tracks(track=track, artist=artist)
            except Exception as e:
                exceptions.add(e)
                info = []

            if not info:
                try:
                    info = await self.bot.pool.last_fm.get_artist_toptracks(artist)
                except Exception as e:
                    exceptions.add(e)

                if not info:
                    txt = f"**Não houve resultados de mixes para sua busca: {artist} - {track}**"
                    raise GenericError(txt, error="\n".join(repr(e) for e in exceptions) or None)

                track_url = f"https://www.last.fm/music/{quote(artist)}"
                playlist_name = f"TopTracks: {artist}"

            else:
                track_url = f"https://www.last.fm/music/{quote(artist)}/_/{quote(track)}"
                playlist_name = f"Mix: {artist} - {track}"
                current = PartialTrack(
                    uri=track_url,
                    title=track,
                    author=artist,
                    requester=user.id,
                    source_name="last.fm",
                )

            playlist = PartialPlaylist(url=track_url, data={"playlistInfo": {"name": playlist_name}})

            playlist.tracks = [
                PartialTrack(
                    uri=i["url"],
                    title=i["name"],
                    author=i["artist"]["name"],
                    requester=user.id,
                    source_name="last.fm",
                )
                for i in info
            ]

            if current:
                playlist.tracks.insert(0, current)

            return playlist, node

        if bool(sc_recommended.search(query)):
            try:
                info = await bot.loop.run_in_executor(
                    None, lambda: self.bot.pool.ytdl.extract_info(query, download=False)
                )
            except AttributeError:
                raise GenericError("**O uso do yt-dlp está desativado...**")

            playlist = PartialPlaylist(url=info["webpage_url"], data={"playlistInfo": {"name": info["title"]}})

            playlist.tracks = [
                PartialTrack(
                    uri=i["url"],
                    title=i["title"],
                    requester=user.id,
                    source_name="soundcloud",
                    identifier=i["id"],
                    playlist=playlist,
                )
                for i in info["entries"]
            ]

            return playlist, node

        tracks, node, exceptions = await self.get_lavalink_tracks(
            query=query, user=user, ctx=ctx, node=node, bot=bot, source=source
        )

        if not tracks:
            tracks, node, exceptions = await self.get_partial_tracks(
                query=query, ctx=ctx, user=user, node=node, bot=bot
            )

            if not tracks:
                txt = "\n".join(exceptions)

                if txt:
                    if "This track is not readable. Available countries:" in txt:
                        raise GenericError(
                            "**A música informada não está disponível na minha região atual...**", error=txt
                        )
                    raise GenericError(
                        "**Não foi possível concluir sua busca agora. Tente novamente em instantes "
                        "ou use outro termo/link.**",
                        error=txt,
                    )
                raise GenericError("**Não houve resultados para sua busca.**")

        return tracks, node
