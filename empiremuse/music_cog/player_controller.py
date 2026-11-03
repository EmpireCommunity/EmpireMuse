# -*- coding: utf-8 -*-
import asyncio
from typing import Union, Optional

import disnake
from disnake.ext import commands

import empiremuse.wavelink as wavelink
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


class PlayerControllerMixin:
    async def player_controller(self, interaction: disnake.MessageInteraction, control: str, **kwargs):
        req = ControllerState(interaction=interaction, control=control)
        if not self.bot.bot_ready or not self.bot.is_ready():
            await req.interaction.send("Ainda estou inicializando...", ephemeral=True)
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
                    await req.interaction.send("Você não pode interagir aqui!", ephemeral=True)
                    return

                req.cmd = self.bot.get_slash_command("fav_manager")
                await self.process_player_interaction(req.interaction, req.cmd, req.cmd_kwargs)
                return

            if req.control in (PlayerControls.add_song, PlayerControls.enqueue_fav):
                if not req.interaction.user.voice:
                    raise GenericError("**Você deve entrar em um canal de voz para usar esse botão.**")

                req.user_data = await self.bot.get_global_data(id_=req.interaction.user.id, db_name=DBModel.users)

                req.modal_components = []
                # o modal é a PRIMEIRA resposta: um tradutor só (sem esperar o banco), usado em todos os textos dele
                req.tr = await self.bot.pool.i18n.for_guild_within(req.interaction.guild)

                _pc_modal_fav_options(self, req)

                await _pc_modal_integration_options(self, req)

                await _pc_modal_send(self, req)

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
                raise GenericError(f"Você deve estar no canal <#{req.vc.id}> para usar os botões do player.")

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
                        '**Você tem uma interação em aberto!**\n`Se for uma mensagem oculta, evite clicar em "ignorar".`'
                    )

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
