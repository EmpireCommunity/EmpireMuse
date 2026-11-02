# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import disnake

import empiremuse.wavelink as wavelink
from empiremuse.utils.music.converters import fix_characters
from empiremuse.utils.music.errors import (
    code_suffix,
    error_code_text,
    log_unexpected_error,
)
from empiremuse.utils.music.lastfm_tools import LastFmException

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
from empiremuse.utils.music.models.autoqueue_steps import (
    _aq_source_tracks,
    _aq_lastfm_tracks,
    _aq_report_failure,
    _aq_queue_results,
    _rt_apply_selected_track,
    _rt_cache_selected_result,
    _rt_cached_result,
    _rt_pick_candidate,
    _rt_query_results,
    _rt_report_failure,
    _rt_search_queries,
    _RT_QUERY_FAILED,
    _RT_RESTARTED,
)
from empiremuse.utils.music.models.step_state import AutoqueueState, ResolveTrackState


class PlayerAutoqueueMixin:
    async def get_autoqueue_tracks(self):
        if getattr(self, "radio_id", None) is not None:
            return None  # rádio nunca toca por sugestão: só o que o runner mandou (nem por outro caminho que chame o autoplay)
        req = AutoqueueState()
        try:
            return self.queue_autoplay.popleft()
        except Exception:
            pass

        if self.locked:
            return

        req.tracks_search = []

        for req.t in reversed(self.failed_tracks + self.played):
            if len(req.tracks_search) > 4:
                break

            req.tracks_search.insert(0, req.t)

        if current_track := self.current or self.last_track:
            req.tracks_search.insert(0, current_track)

        req.track = None
        req.tracks = []
        req.tracks_ytsearch = []

        req.exception = None

        if req.tracks_search:
            self.locked = True

            for req.track_data in req.tracks_search:
                await _aq_source_tracks(self, req)

                await _aq_lastfm_tracks(self, req)

                req.track = req.track_data

            if not req.tracks:
                try:
                    req.tracks_ytsearch = req.tracks_ytsearch.tracks
                except AttributeError:
                    pass
                req.tracks = req.tracks_ytsearch
                req.tracks.reverse()

            if not req.tracks:
                if await _aq_report_failure(self, req):
                    return

        _aq_queue_results(self, req)

        self.locked = False

        try:
            return self.queue_autoplay.popleft()
        except Exception:
            try:
                return self.played.popleft()
            except Exception:
                return None

    async def resolve_track(self, track: PartialTrack, force=False):
        if track.id:
            return

        req = ResolveTrackState(check_duration=bool(track.duration))

        try:
            search_queries = _rt_search_queries(self, track, force)

            if search_queries is None:
                return

            req.cached = _rt_cached_result(self, track)

            for query in search_queries:
                result = await _rt_query_results(self, track, query, req)

                if result is _RT_RESTARTED:
                    return

                if result is _RT_QUERY_FAILED:
                    continue

                req.selected_track = _rt_pick_candidate(track, result, req)

                if req.selected_track:
                    req.selected_result = result
                    break

            if not req.selected_track:
                try:
                    req.selected_track = req.tracks[0]
                except Exception:
                    if req.exceptions:
                        logger.warning(
                            "Falha ao resolver PartialTrack (%s)", ", ".join(type(e).__name__ for e in req.exceptions)
                        )
                        logger.debug("Exceções ao resolver PartialTrack: %r", req.exceptions)
                    return

            _rt_cache_selected_result(self, track, req)
            _rt_apply_selected_track(track, req.selected_track)

        except Exception as e:
            await _rt_report_failure(self, track, e)
