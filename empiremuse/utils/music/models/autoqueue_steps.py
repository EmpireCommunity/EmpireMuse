# -*- coding: utf-8 -*-
"""Etapas de `LavalinkPlayer.get_autoqueue_tracks` (busca de músicas recomendadas)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import disnake
from aiohttp import ClientConnectionError, ClientError, ClientResponseError

import empiremuse.wavelink as wavelink
from empiremuse.utils.music.converters import fix_characters, md_link, md_code
from empiremuse.utils.music.errors import (
    code_suffix,
    error_code_text,
    log_unexpected_error,
)
from empiremuse.utils.music.lastfm_tools import LastFmException
from empiremuse.utils.safe_error import safe_error

if TYPE_CHECKING:
    pass

from empiremuse.utils.music.models.tracks import (
    LavalinkPlaylist,
    LavalinkTrack,
    PartialTrack,
)
from empiremuse.utils.music.models.helpers import (
    exclude_tags,
    exclude_tags_2,
    lavalink_text,
    log_lavalink_failure,
    logger,
    native_sources,
    providers_dict,
)


def _spotify_url(obj: object) -> str:
    """Extrai com segurança a URL do Spotify de um objeto da API."""
    if isinstance(obj, dict):
        urls = obj.get("external_urls")
        if isinstance(urls, dict):
            return urls.get("spotify") or ""
    return ""


async def _aq_source_tracks(self, req):
    """Busca recomendações na fonte da faixa (Spotify, Deezer ou SoundCloud)."""
    exception = req.exception
    t = req.t
    track_data = req.track_data
    tracks = req.tracks
    tracks_search = req.tracks_search

    if (
        track_data.info["sourceName"] == "spotify"
        and "spotify" not in self.node.info["sourceManagers"]
        and self.bot.spotify
    ):
        result = None

        for i in range(3):
            try:
                result = await self.bot.spotify.track_search(tracks_search[0].author, limit=10)
                exception = None
                break
            except Exception as e:
                is_transient = (
                    isinstance(e, (ClientConnectionError, asyncio.TimeoutError))
                    or (isinstance(e, ClientResponseError) and (not e.status or e.status >= 500))
                    or (isinstance(e, ClientError) and not isinstance(e, ClientResponseError))
                )
                logger.warning("Falha ao obter músicas recomendadas do Spotify (%s).", type(e).__name__)
                logger.debug("Detalhe da falha no Spotify:", exc_info=True)
                exception = e
                if is_transient:
                    self.set_command_log(
                        emoji="⚠️",
                        text=f"Falha ao obter músicas recomendadas do spotify, tentativa {i + 1} de 3.",
                        controller=True,
                    )
                    self.update = True
                    if i < 2:
                        await asyncio.sleep(5)
                else:
                    break

        if result:
            tracks = []
            items = (result.get("tracks") if isinstance(result, dict) else {}) or {}
            if isinstance(items, dict):
                items = items.get("items") or []
            if not isinstance(items, list):
                items = []

            for t in items:
                if not isinstance(t, dict):
                    continue

                uri = _spotify_url(t)
                identifier = t.get("id") or ""
                if not uri or not identifier:
                    continue

                album = t.get("album") or {}
                try:
                    images = album.get("images") if isinstance(album, dict) else []
                    thumb = images[0]["url"] if images and isinstance(images[0], dict) else ""
                except IndexError, KeyError, TypeError, AttributeError:
                    thumb = ""

                artists = t.get("artists") or []
                first_artist = (
                    artists[0].get("name") if artists and isinstance(artists[0], dict) else None
                ) or "Unknown Artist"

                partial_track = PartialTrack(
                    uri=uri,
                    author=first_artist,
                    title=t.get("name") or "",
                    thumb=thumb,
                    duration=t.get("duration_ms") or 0,
                    source_name="spotify",
                    identifier=identifier,
                    requester=self.bot.user.id,
                    autoplay=True,
                )

                partial_track.info["extra"]["authors"] = [
                    fix_characters(i["name"])
                    for i in artists
                    if isinstance(i, dict)
                    and i.get("name")
                    and f"feat. {i['name'].lower()}" not in (t.get("name") or "").lower()
                ]

                authors_md = [
                    md_link(a["name"], _spotify_url(a)) for a in artists if isinstance(a, dict) and a.get("name")
                ]
                partial_track.info["extra"]["authors_md"] = ", ".join(authors_md)

                if isinstance(album, dict) and album.get("name") and album.get("name") != t.get("name"):
                    partial_track.info["extra"]["album"] = {
                        "name": album["name"],
                        "url": _spotify_url(album),
                    }

                tracks.append(partial_track)

    elif (
        track_data.info["sourceName"] == "deezer"
        and (self.bot.pool.config["FORCE_USE_DEEZER_CLIENT"] or "deezer" not in self.node.info["sourceManagers"])
        and (artist_id := track_data.info["extra"].get("artist_id"))
    ):
        try:
            try:
                result = await self.bot.deezer.get_artist_radio_info(artist_id)
            except Exception:
                logger.exception("Falha em _aq_source_tracks")
                result = None

            if result:
                tracks = []

                for n, t in enumerate(result):
                    partial_track = PartialTrack(
                        uri=f"https://www.deezer.com/track/{t['id']}",
                        author=t["artist"]["name"],
                        title=t["title"],
                        thumb=t["album"]["cover_big"],
                        duration=t["duration"] * 1000,
                        source_name="deezer",
                        identifier=t["id"],
                        requester=self.bot.user.id,
                        autoplay=True,
                    )

                    partial_track.info["isrc"] = t.get("isrc")
                    artists = t.get("contributors") or [t["artist"]]

                    partial_track.info["extra"]["authors"] = [
                        fix_characters(a["name"]) for a in artists if isinstance(a, dict) and a.get("name")
                    ]
                    authors_md = [
                        md_link(a["name"], f"https://www.deezer.com/artist/{a['id']}")
                        for a in artists
                        if isinstance(a, dict) and a.get("name") and a.get("id")
                    ]
                    partial_track.info["extra"]["authors_md"] = ", ".join(authors_md)
                    partial_track.info["extra"]["artist_id"] = t["artist"]["id"]

                    if t["title"] != t["album"]["title"]:
                        partial_track.info["extra"]["album"] = {
                            "name": t["album"]["title"],
                            "url": t["album"]["tracklist"],
                        }

                    tracks.append(partial_track)
        except Exception:
            logger.exception("Falha em _aq_source_tracks")

    elif track_data.info["sourceName"] == "soundcloud":
        try:
            info = await self.bot.loop.run_in_executor(
                None,
                lambda: self.bot.pool.ytdl.extract_info(f"{track_data.uri}/recommended", download=False),
            )
        except AttributeError:
            pass

        else:
            tracks = [
                PartialTrack(
                    uri=i["url"],
                    title=i["title"],
                    requester=self.bot.user.id,
                    source_name="soundcloud",
                    identifier=i["id"],
                    autoplay=True,
                )
                for i in info.get("entries", [])
            ]

    req.exception = exception
    req.t = t
    req.tracks = tracks


async def _aq_lastfm_tracks(self, req):
    """Busca recomendações pelo Last.fm e pelos provedores de busca do nó."""
    exception = req.exception
    t = req.t
    track_data = req.track_data
    tracks = req.tracks
    tracks_search = req.tracks_search
    tracks_ytsearch = req.tracks_ytsearch

    if not tracks:
        if self.bot.last_fm and not self.lastfm_artists:
            if track_data.ytid:
                if (
                    track_data.author.endswith(" - topic")
                    and not track_data.author.endswith("Release - topic")
                    and not track_data.title.startswith(track_data.author[:-8])
                ):
                    artist = track_data.author[:-8]
                else:
                    try:
                        artist = track_data.title.split(" - ", maxsplit=1)[0]
                    except ValueError:
                        artist = track_data.author
            else:
                artist = track_data.author

            try:
                self.lastfm_artists = [
                    a["name"]
                    for a in await self.bot.last_fm.get_similar_artists(artist)
                    if a["name"].lower() not in track_data.author.lower()
                ]
                self.lastfm_artists.insert(0, track_data.author)
            except LastFmException as e:
                logger.warning("Last.FM recommendations error: %s (código %s)", type(e).__name__, e.code)
            except Exception as e:
                if isinstance(e, ClientError):
                    # O str do erro do aiohttp traz a URL com api_key e sk: só o status vai para o log.
                    logger.warning("Last.FM recommendations error: %s", safe_error(e))
                else:
                    logger.exception("Falha em _aq_lastfm_tracks")

        try:
            author = self.lastfm_artists.pop(0)
        except Exception:
            author = track_data.author

        if track_data.info["sourceName"] == "youtube" and self.native_yt:
            queries = [f"https://www.youtube.com/watch?v={track_data.ytid}&list=RD{track_data.ytid}"]

            if p_dict := providers_dict.get(track_data.info["sourceName"]):
                providers = [p_dict] + [p for p in self.node.search_providers if p != p_dict]
            else:
                providers = self.node.search_providers

            queries.extend([f"{sp}:{author.split(',')[0]}" for sp in providers])

        # elif track_data.info["sourceName"] == "spotify" and "spotify" in self.node.info["sourceManagers"] and (spotify_tracks:=[t.identifier for t in tracks_search if t.info["sourceName"] == "spotify"]):
        #    queries = ["sprec:seed_tracks=" + ",".join(set(spotify_tracks[:5]))]

        elif (
            track_data.info["sourceName"] == "deezer"
            and "deezer" in self.node.info["sourceManagers"]
            and (deezer_tracks := [t.identifier for t in tracks_search if t.info["sourceName"] == "deezer"])
        ):
            queries = [f"dzrec:{deezer_tracks[0]}"]

        else:
            if p_dict := providers_dict.get(track_data.info["sourceName"]):
                providers = [p_dict] + [p for p in self.node.search_providers if p != p_dict]
            else:
                providers = self.node.search_providers

            queries = [f"{sp}:{author.split(',')[0]}" for sp in providers]

        for query in queries:
            if query.startswith("jssearch"):
                continue

            try:
                tracks = await self.node.get_tracks(
                    query,
                    track_cls=LavalinkTrack,
                    playlist_cls=LavalinkPlaylist,
                    autoplay=True,
                    requester=self.bot.user.id,
                )
            except wavelink.TrackNotFound as e:
                logger.debug("Autoplay: sem faixas para %s: %s", query, e)
                continue
            except Exception as e:
                if [
                    err for err in ("Could not find tracks from mix", "Could not read mix page") if err in str(e)
                ] and self.native_yt:
                    try:
                        tracks_ytsearch = await self.node.get_tracks(
                            f'{query}:"{track_data.author}"',
                            track_cls=LavalinkTrack,
                            playlist_cls=LavalinkPlaylist,
                            autoplay=True,
                            requester=self.bot.user.id,
                        )
                        break
                    except Exception as e:
                        exception = e
                        continue
                else:
                    logger.exception("Falha em _aq_lastfm_tracks")
                    exception = e
                    await asyncio.sleep(1.5)
                    continue

            try:
                tracks = tracks.tracks
            except Exception:
                pass

            if not tracks:
                continue

            break

        if not any(i in track_data.title.lower() for i in exclude_tags_2):
            final_tracks = []
            for t in tracks:
                if not any((i in t.title.lower()) for i in exclude_tags_2) and not track_data.uri.startswith(t.uri):
                    final_tracks.append(t)
            tracks = final_tracks or tracks

    req.exception = exception
    req.t = t
    req.tracks = tracks
    req.tracks_ytsearch = tracks_ytsearch


async def _aq_report_failure(self, req):
    """Avisa no canal que o autoplay não encontrou músicas."""
    exception = req.exception

    self.locked = False

    if exception:
        if isinstance(exception, wavelink.TrackLoadError):
            error_code = log_lavalink_failure(
                "Falha ao obter dados do autoplay",
                message=exception.message,
                cause=exception.cause,
                severity=exception.severity,
                node=self.node.identifier,
            )
            error_msg = (
                f"**Mensagem:** {lavalink_text(exception.message)}\n"
                f"**Nível:** {md_code(exception.severity)}\n"
                f"**Servidor de música:** {md_code(self.node.identifier)}{code_suffix(error_code)}"
            )
        else:
            error_code = log_unexpected_error(logger, exception, "autoplay")
            error_msg = f"**Ocorreu um erro inesperado.**{code_suffix(error_code)}"
    else:
        error_msg = "Não houve resultados relacionados as músicas tocadas..."

    try:
        embed = disnake.Embed(
            description=f"**Falha ao obter dados do autoplay:**\n{error_msg}", color=disnake.Colour.red()
        )
        await self.text_channel.send(embed=embed, delete_after=10)
    except Exception:
        logger.exception("Falha em _aq_report_failure")
    await asyncio.sleep(7)
    return True
    return False


def _aq_queue_results(self, req):
    """Filtra os resultados e coloca as faixas na fila do autoplay."""
    t = req.t
    track = req.track
    tracks = req.tracks
    tracks_search = req.tracks_search

    try:
        tracks = tracks.tracks
    except AttributeError:
        pass

    try:
        tracks = [t for t in tracks if not [u for u in tracks_search if t.uri.startswith(u.uri)]]
    except Exception:
        pass

    if track:
        info = {"title": track.title, "uri": track.uri}

        tracks_final = []

        for t in tracks:
            if t.is_stream:
                continue

            # if t.duration < 90000:
            #    continue

            if track.ytid and track.ytid == t.ytid:
                continue

            if not isinstance(t, PartialTrack):
                t.info["extra"].update({"autoplay": True, "requester": self.bot.user.id})
                t.playlist = None

            t.info["extra"]["related"] = info
            tracks_final.append(t)

        tracks.clear()
        self.queue_autoplay.extend(tracks_final)

    req.t = t
    req.tracks = tracks


# Sentinelas de `_rt_query_results` (um resultado vazio ou `None` da busca é um resultado válido).
_RT_RESTARTED = object()
_RT_QUERY_FAILED = object()


def _rt_cache_key(track):
    return f"{track.info['sourceName']}:{track.author}-{track.single_title}"


def _rt_provider_queries(self, track):
    search_queries = []
    for sp in self.node.partial_providers:
        if track.info["sourceName"] == "youtube" and sp.startswith(("ytsearch:", "ytmsearch:")) and not self.native_yt:
            continue

        if "{isrc}" in sp:
            if isrc := track.info.get("isrc"):
                search_queries.append(sp.replace("{isrc}", isrc))
            continue
        search_queries.append(sp.replace("{title}", track.single_title).replace("{author}", ", ".join(track.authors)))
    return search_queries


def _rt_search_queries(self, track, force):
    """Consultas de busca da faixa parcial; `None` quando ela não deve ser resolvida agora."""

    if track.info["sourceName"] == "http":
        search_queries = [track.uri or track.search_uri]
    elif (
        track.info["sourceName"] == "youtube"
        and not force
        and (not self.native_yt or not self.node.prefer_youtube_native_playback)
    ):
        return None
    else:
        search_queries = []

    if not search_queries:
        if (
            not force
            and track.info["sourceName"] in self.node.info.get("sourceManagers", [])
            and (not self.node.only_use_native_search_providers or track.info["sourceName"] in native_sources)
        ):
            search_queries = [track.uri]
        else:
            search_queries = _rt_provider_queries(self, track)

    return search_queries


def _rt_drop_youtube_providers(self):
    cog = self.bot.get_cog("Music")
    cog.remove_provider(self.node.search_providers, ["ytsearch", "ytmsearch"])
    cog.remove_provider(
        self.node.partial_providers,
        [
            'ytsearch:"{isrc}"',
            'ytsearch:"{title} - {author}"',
            'ytmsearch:"{isrc}"',
            'ytmsearch:"{title} - {author}"',
        ],
    )
    self.native_yt = False


def _rt_cached_result(self, track):
    """Resultado guardado para esta faixa por outro servidor, ou `None` (vazio conta como ausente)."""

    return self.bot.pool.partial_track_cache.get(_rt_cache_key(track)) or None


async def _rt_query_results(self, track, query, req):
    """Resultado da busca de `query` (do cache na 1ª consulta, senão do nó), `_RT_QUERY_FAILED` ou `_RT_RESTARTED`."""

    if req.cached is not None:
        result, req.cached = req.cached, None
        return result

    try:
        result = await self.node.get_tracks(
            query,
            track_cls=LavalinkTrack,
            playlist_cls=LavalinkPlaylist,
            check_title=60 if query.startswith(("ytmsearch", "ytsearch", "scsearch")) else 75,
        )
    except Exception as e:
        if track.info["sourceName"] == "youtube" and any(
            msg in str(e)
            for msg in (
                "This video is not available",
                "YouTube WebM streams are currently not supported.",
                "Video returned by YouTube isn't what was requested",
                "The video returned is not what was requested.",
            )
        ):
            _rt_drop_youtube_providers(self)
            await self.resolve_track(track)
            return _RT_RESTARTED
        req.exceptions.append(e)
        return _RT_QUERY_FAILED

    try:
        result = result.tracks
    except AttributeError:
        pass

    return result


NO_STREAM_ERROR = (
    "No playable stream"  # o que o nó diz quando o candidato escolhido não tem stream tocável (ERR-65FEA7)
)
REJECTED_CANDIDATES_MAX = 3  # candidatos sem stream que uma faixa pode trocar antes de falhar


def _rt_rejected(track) -> list:
    """Os candidatos já recusados desta parcial (ids), guardados na própria faixa."""
    return track.info.setdefault("extra", {}).setdefault("rejected", [])


def reject_no_stream(track, message) -> bool:
    """O candidato escolhido para ``track`` não tem stream ("No playable stream"): recusa-o e solta a escolha para ``resolve_track`` ir ao próximo.

    ``True`` se vale tentar de novo (até ``REJECTED_CANDIDATES_MAX`` por faixa); qualquer outro erro, ou o teto, é ``False`` e a faixa falha como antes.
    """
    rejected = _rt_rejected(track)
    if not track.id or NO_STREAM_ERROR not in str(message or "") or len(rejected) >= REJECTED_CANDIDATES_MAX:
        return False
    rejected.append(track.id)
    track.id = None
    track.info.pop("id", None)
    return True


def _rt_pick_candidate(track, result, req):
    """Primeira faixa de `result` que serve para a parcial (sem stream, sem tags excluídas, duração próxima, e que o nó já não recusou)."""

    try:
        if result[0].info["sourceName"] == "bandcamp":
            req.check_duration = False
    except Exception:
        pass

    has_exclude_tags = any(tag for tag in exclude_tags if tag.lower() in track.title.lower())

    if req.first_result is None and result:
        req.first_result = result

    req.tracks.extend(result)

    rejected = _rt_rejected(track)

    for t in result:
        if t.is_stream or t.id in rejected:
            continue

        if not has_exclude_tags and any(tag for tag in exclude_tags if tag.lower() in t.title.lower()):
            continue

        if req.check_duration and not ((t.duration - 10000) < track.duration < (t.duration + 10000)):
            continue

        return t

    return None


def _rt_cache_selected_result(self, track, req):
    """Guarda para os outros servidores só o resultado que levou à faixa escolhida (nunca o que foi rejeitado)."""

    source = req.selected_result if req.selected_result is not None else req.first_result
    self.bot.pool.partial_track_cache[_rt_cache_key(track)] = source


def _rt_apply_selected_track(track, selected_track):
    track.id = selected_track.id
    track.info["id"] = selected_track.id
    track.info["length"] = selected_track.duration
    if track.info["sourceName"] == "last.fm":
        track.info["pluginInfo"] = selected_track.info.get("pluginInfo", {})
        track.info["author"] = selected_track.author
        track.info["title"] = selected_track.title
        track.info["sourceName"] = selected_track.info["sourceName"]
        track.info["uri"] = selected_track.info["uri"]
    else:
        track.info["sourceNameOrig"] = selected_track.info["sourceName"]
        if not track.info["author"]:
            track.info["author"] = selected_track.author
    if not track.duration:
        track.info["duration"] = selected_track.duration
    if not track.thumb:
        track.info["artworkUrl"] = selected_track.thumb


async def _rt_report_failure(self, track, e):
    error_code = log_unexpected_error(
        logger, e, f"obtenção de informação da PartialTrack {track.uri or track.search_uri}"
    )
    embed = disnake.Embed(
        description=f"**Falha ao obter informação de PartialTrack:\n{md_link(track.title, track.uri or track.search_uri, code=False)}**"
        f"{error_code_text(error_code)}\n"
        f"**Servidor de música:** {md_code(self.node.identifier)}",
        color=disnake.Colour.red(),
    )
    await self.report_error(embed, track)
