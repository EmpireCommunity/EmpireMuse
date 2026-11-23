# -*- coding: utf-8 -*-
import asyncio
import logging
from typing import Union, Optional

import disnake
from disnake.ext import commands

import empiremuse.wavelink as wavelink
from empiremuse.utils.music.guild_text import guild_text
from empiremuse.rules.errors import RuleError
from empiremuse.rules.favorites import save_track
from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    is_dj,
    can_connect,
    check_player_perm,
)
from empiremuse.utils.music.converters import (
    fix_characters,
    URL_REG,
)
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.user_limits import quota_for, track_fav_error_text
from empiremuse.utils.user_docs import change_user_doc, normalize_integration_links
from empiremuse.utils.music.interactions import (
    SelectInteraction,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    send_idle_embed,
    PlayerControls,
    music_source_emoji_url,
)
from empiremuse.music_cog.controller_embed import (
    _pc_embed_resolve,
    _pc_embed_playlist,
    _pc_embed_find_track,
    _pc_embed_track,
    _pc_fav_embed_save,
    _pc_fav_embed_reply,
)
from empiremuse.music_cog.controller_buttons import (
    _pc_modal_fav_options,
    _pc_modal_integration_options,
    _pc_modal_send,
    _pc_lastfm,
    _pc_player_checks,
    _pc_help_button,
    _pc_favorite_choices,
    _pc_favorite_select,
    _pc_favorite_save,
    _pc_lyrics,
    _pc_command_args,
)
from empiremuse.music_cog.controller_state import ControllerState

logger = logging.getLogger(__name__)


class PlayerControllerMixin:
    async def player_controller(self, interaction: disnake.MessageInteraction, control: str, **kwargs):
        req = ControllerState(interaction=interaction, control=control)
        if not self.bot.bot_ready or not self.bot.is_ready():
            await req.interaction.send(
                await guild_text(self.bot, req.interaction.guild, "legacy.controller.starting"), ephemeral=True
            )
            return

        if not req.interaction.guild_id:
            await req.interaction.response.edit_message(components=None)
            return

        req.cmd_kwargs = {}

        req.cmd = None

        if req.control in (
            PlayerControls.embed_forceplay,
            PlayerControls.embed_enqueue_track,
            PlayerControls.embed_enqueue_playlist,
        ):
            try:
                if await _pc_embed_resolve(self, req):
                    return

                if req.control == PlayerControls.embed_enqueue_playlist:
                    await _pc_embed_playlist(self, req)

                else:
                    await _pc_embed_find_track(self, req)

                    await _pc_embed_track(self, req)

            except Exception as e:
                self.bot.dispatch("interaction_player_error", req.interaction, e)
                if not isinstance(e, GenericError):
                    await asyncio.sleep(5)
            try:
                await self.player_interaction_concurrency.release(req.interaction)
            except Exception:
                pass
            return

        if req.control == PlayerControls.embed_add_fav:
            if await _pc_fav_embed_save(self, req):
                return

            await _pc_fav_embed_reply(self, req)
            return

        if not req.interaction.guild:
            await req.interaction.response.edit_message(components=None)
            return

        try:
            if req.control == "musicplayer_jukebox_channel":
                jukebox_cmd = self.bot.get_slash_command("jukebox")
                req.cmd = jukebox_cmd.children.get("setup") if jukebox_cmd else None
                req.cmd_kwargs = {"target": req.interaction.channel}
                await self.process_player_interaction(req.interaction, req.cmd, req.cmd_kwargs)
                return

            if req.control == PlayerControls.fav_manager:
                if str(req.interaction.user.id) not in req.interaction.message.content:
                    await req.interaction.send(
                        await guild_text(self.bot, req.interaction.guild, "legacy.controller.not_yours"), ephemeral=True
                    )
                    return

                req.cmd = self.bot.get_slash_command("fav_manager")
                await self.process_player_interaction(req.interaction, req.cmd, req.cmd_kwargs)
                return

            if req.control in (PlayerControls.add_song, PlayerControls.enqueue_fav):
                if not req.interaction.user.voice:
                    raise GenericError(
                        await guild_text(self.bot, req.interaction.guild, "legacy.controller.join_voice_button")
                    )

                # antes de ler: vale para a normalização das integrações, feita depois do modal
                read_at = self.bot.pool.erasure_tombstones.now()
                req.user_data = await self.bot.get_global_data(id_=req.interaction.user.id, db_name=DBModel.users)

                req.modal_components = []
                # o modal é a PRIMEIRA resposta: um tradutor só (sem esperar o banco), usado em todos os textos dele
                req.tr = await self.bot.pool.i18n.for_guild_within(req.interaction.guild)

                _pc_modal_fav_options(self, req)

                legacy_integrations = _pc_modal_integration_options(self, req)

                await _pc_modal_send(self, req)

                if legacy_integrations:
                    # depois do modal: relido e normalizado sob o lock do usuário, nada do documento lido antes volta ao banco
                    try:
                        await change_user_doc(
                            self.bot, req.interaction.user.id, normalize_integration_links, read_at=read_at
                        )
                    except Exception as exc:
                        # o modal já é a resposta desta interação: não há como avisar o membro, e o formato antigo segue
                        # válido para leitura. Só o tipo vai ao log.
                        logger.warning("Integrações não normalizadas depois do modal (%s)", type(exc).__name__)

                return

            if req.control == PlayerControls.lastfm_scrobble:
                await _pc_lastfm(self, req)
                return

            if await _pc_player_checks(self, req):
                return

            if req.control == PlayerControls.help_button:
                await _pc_help_button(self, req)
                return

            if not req.interaction.author.voice or req.interaction.author.voice.channel != req.vc:
                raise GenericError(
                    await guild_text(
                        self.bot, req.interaction.guild, "legacy.controller.join_channel", channel=req.vc.id
                    )
                )

            if req.control == PlayerControls.miniqueue:
                await is_dj().predicate(req.interaction)
                req.player.mini_queue_enabled = not req.player.mini_queue_enabled
                req.player.set_command_log(
                    emoji="📑",
                    text=f"{req.interaction.author.mention} {'ativou' if req.player.mini_queue_enabled else 'desativou'} "
                    f"a mini-fila do player.",
                )
                await req.player.invoke_np(interaction=req.interaction)
                return

            if req.control != PlayerControls.queue:
                try:
                    await self.player_interaction_concurrency.acquire(req.interaction)
                except commands.MaxConcurrencyReached:
                    raise GenericError(
                        await guild_text(self.bot, req.interaction.guild, "legacy.controller.open_interaction")
                    )

            jukebox = getattr(getattr(self.bot.pool, "radio", None), "jukebox", None)
            station = jukebox.station_view(req.player) if jukebox is not None and req.player is not None else None
            if req.control == PlayerControls.add_favorite and (
                getattr(req.player, "radio_mode", None) is not None or (station is not None and station.playing)
            ):
                # modo rádio da Jukebox (T-434 §4.2): a estrela põe o ao vivo nas MINHAS favoritas (nunca grava URL); com uma faixa da
                # estação tocando, a estação vai às favoritas do membro neste servidor (J2b). Resposta só para quem clicou
                try:
                    toggle = jukebox.toggle_favorite if station is None else jukebox.toggle_station_favorite
                    # confirma antes da gravação: a leitura e a escrita podem passar dos 3 s da interação
                    await req.interaction.response.defer(ephemeral=True, with_message=True)
                    key, fields = await toggle(req.interaction, req.player)
                    await req.interaction.edit_original_message(content=req.player.tr(key, **fields))
                finally:
                    try:
                        await self.player_interaction_concurrency.release(req.interaction)
                    except Exception:
                        pass
                return

            if req.control == PlayerControls.add_favorite:
                if await _pc_favorite_choices(self, req):
                    return

                if await _pc_favorite_select(self, req):
                    return

                await _pc_favorite_save(self, req)
                return

            if req.control == PlayerControls.lyrics:
                await _pc_lyrics(self, req)
                return

            _pc_command_args(self, req)

            await self.process_player_interaction(interaction=req.interaction, command=req.cmd, kwargs=req.cmd_kwargs)

            try:
                await self.player_interaction_concurrency.release(req.interaction)
            except Exception:
                pass

        except Exception as e:
            try:
                await self.player_interaction_concurrency.release(req.interaction)
            except Exception:
                pass
            self.bot.dispatch("interaction_player_error", req.interaction, e)
