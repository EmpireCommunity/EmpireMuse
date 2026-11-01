# -*- coding: utf-8 -*-
"""Etapas de `LavalinkPlayer.invoke_np` (montagem e envio da mensagem do player)."""

from __future__ import annotations

import asyncio
import itertools
import random
from itertools import cycle

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.converters import fix_characters, time_format, get_button_style
from empiremuse.utils.music.errors import (
    code_suffix,
    log_unexpected_error,
)
from empiremuse.utils.music.skin_utils import skin_converter
from empiremuse.utils.others import PlayerControls

from empiremuse.utils.music.models.helpers import (
    logger,
)
from empiremuse.utils.music.app_emojis import emoji as app_emoji


async def _np_guards(self, req):
    """Sai cedo quando não há o que atualizar (player fechando, sem canal, sem faixa)."""
    force = req.force
    interaction = req.interaction

    if self.is_closing:
        return True

    if not self.text_channel:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass
        return True

    if not self.current:
        if not force:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception:
                pass
        else:
            await self.process_idle_message()
        return True

    if self.updating and not force:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass
        return True

    if self.has_lyrics:
        return True
    return False


async def _np_data(self, req):
    """Carrega os dados da skin e confere se a mensagem mudou."""
    interaction = req.interaction

    try:
        if self.static:
            if self.skin_static.startswith("> custom_skin: "):
                data = skin_converter(
                    self.custom_skin_static_data[self.skin_static[15:]], player=self, guild=self.guild
                )
            else:
                data = self.bot.pool.player_static_skins[self.skin_static].load(self)

        else:
            if self.skin.startswith("> custom_skin: "):
                data = skin_converter(self.custom_skin_data[self.skin[15:]], player=self, guild=self.guild)
            else:
                data = self.bot.pool.player_skins[self.skin].load(self)
    except OverflowError:
        await self.process_next()
        return True

    if data == self.last_data:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass
        return True

    self.last_data = data

    if not data.get("flags"):
        try:
            if self.static and isinstance(self.text_channel.parent, disnake.ForumChannel):
                data["content"] = (
                    f"`{'▶️' if not self.paused else '⏸️'} {fix_characters(self.current.title, 50)}` |\n\n"
                    + (data.get("content") or "")
                )
        except Exception:
            pass

    req.data = data
    return False


async def _np_plain(self, req):
    """Envia a mensagem do player sem controles (modo sem botões)."""
    data = req.data

    if self.temp_embed:
        data["embeds"].insert(0, self.temp_embed)
        self.temp_embed = None

    self.updating = True

    if self.keep_connected:
        if not self.is_last_message():
            await self.destroy_message()
        elif self.text_channel:
            if not self.text_channel.permissions_for(self.guild.me).send_messages:
                self.text_channel = None
                self.message = None
            else:
                try:
                    await self.message.edit(allowed_mentions=self.allowed_mentions, **data)
                except disnake.Forbidden:
                    self.message = None
                    self.text_channel = None
                except Exception:
                    self.message = await self.text_channel.send(allowed_mentions=self.allowed_mentions, **data)

    else:
        try:
            self.message_updater_task.cancel()
        except Exception:
            pass
        self.message = await self.text_channel.send(allowed_mentions=self.allowed_mentions, **data)

    self.updating = False


def _np_buttons_default(self, req):
    """Monta os botões e o menu padrão quando a skin não define controles."""
    data = req.data

    data["components"] = [
        disnake.ui.Button(
            emoji=app_emoji(self.bot, "playpause"),
            custom_id=PlayerControls.pause_resume,
            style=get_button_style(self.paused),
        ),
        disnake.ui.Button(emoji=app_emoji(self.bot, "backward"), custom_id=PlayerControls.back),
        disnake.ui.Button(emoji=app_emoji(self.bot, "stop"), custom_id=PlayerControls.stop),
        disnake.ui.Button(emoji=app_emoji(self.bot, "skip"), custom_id=PlayerControls.skip),
        disnake.ui.Button(
            emoji=app_emoji(self.bot, "music_queue"),
            custom_id=PlayerControls.queue,
            disabled=not (self.queue or self.queue_autoplay),
        ),
        disnake.ui.Select(
            placeholder=fit(self.tr("player.more_options.placeholder"), 150),
            custom_id="musicplayer_dropdown_inter",
            min_values=0,
            max_values=1,
            required=False,
            options=[
                disnake.SelectOption(
                    label=fit(self.tr("player.add_song.label"), 100),
                    emoji=app_emoji(self.bot, "add_music"),
                    value=PlayerControls.add_song,
                    description=fit(self.tr("player.add_a_song_playlist_to.description"), 100),
                ),
                disnake.SelectOption(
                    label=fit(self.tr("player.add_to_your_favorites.label"), 100),
                    emoji=app_emoji(self.bot, "loved"),
                    value=PlayerControls.add_favorite,
                    description=fit(self.tr("player.add_the_current_song_to.description"), 100),
                ),
                disnake.SelectOption(
                    label=fit(self.tr("player.play_from_the_start.label"), 100),
                    emoji=app_emoji(self.bot, "fast_backward"),
                    value=PlayerControls.seek_to_start,
                    description=fit(self.tr("player.go_back_to_the_start.description"), 100),
                ),
                disnake.SelectOption(
                    label=fit(self.tr("player.volume.label", volume=str(self.volume)), 100),
                    emoji=app_emoji(self.bot, "volume"),
                    value=PlayerControls.volume,
                    description=fit(self.tr("player.adjust_the_volume.description"), 100),
                ),
                disnake.SelectOption(
                    label="Misturar",
                    emoji=app_emoji(self.bot, "shuffle"),
                    value=PlayerControls.shuffle,
                    description=fit(self.tr("player.shuffle_the_queue_songs.description"), 100),
                ),
                disnake.SelectOption(
                    label="Readicionar",
                    emoji=app_emoji(self.bot, "playlist_add"),
                    value=PlayerControls.readd,
                    description=fit(self.tr("player.add_the_played_songs_back.description"), 100),
                ),
                disnake.SelectOption(
                    label=fit(self.tr("player.repeat.label"), 100),
                    emoji=app_emoji(self.bot, "repeat_all"),
                    value=PlayerControls.loop_mode,
                    description=fit(self.tr("player.enable_disable_repeating_the_song.description"), 100),
                ),
                disnake.SelectOption(
                    label=(
                        fit(
                            self.tr("player.disable_the_nightcore_effect.label")
                            if self.nightcore
                            else self.tr("player.enable_the_nightcore_effect.label"),
                            100,
                        )
                    ),
                    emoji=app_emoji(self.bot, "sound_spectrum"),
                    value=PlayerControls.nightcore,
                    description=fit(self.tr("player.effect_that_raises_the_speed.description"), 100),
                ),
                disnake.SelectOption(
                    label=(
                        fit(
                            self.tr("player.disable_autoplay.label")
                            if self.autoplay
                            else self.tr("player.enable_autoplay.label"),
                            100,
                        )
                    ),
                    emoji=app_emoji(self.bot, "refresh"),
                    value=PlayerControls.autoplay,
                    description=fit(self.tr("player.automatic_song_playback_system_when.description"), 100),
                ),
                disnake.SelectOption(
                    label="Last.fm scrobble",
                    emoji=app_emoji(self.bot, "lastfm"),
                    value=PlayerControls.lastfm_scrobble,
                    description=fit(self.tr("player.enable_disable_scrobbling_logging_of.description"), 100),
                ),
                disnake.SelectOption(
                    label=(
                        fit(
                            self.tr("player.disable_restricted_mode.label")
                            if self.restrict_mode
                            else self.tr("player.enable_restricted_mode.label"),
                            100,
                        )
                    ),
                    emoji=app_emoji(self.bot, "lock"),
                    value=PlayerControls.restrict_mode,
                    description="Apenas DJ's/Staff's podem usar comandos restritos.",
                ),
            ],
        ),
    ]


def _np_buttons_extra(self, req):
    """Acrescenta ao menu padrão as opções que dependem do estado do player."""
    data = req.data

    if self.static:
        if queue := self.queue or self.queue_autoplay:
            data["components"].append(
                disnake.ui.Select(
                    placeholder=fit(self.tr("player.next_songs.placeholder"), 150),
                    custom_id="musicplayer_queue_dropdown",
                    min_values=0,
                    max_values=1,
                    required=False,
                    options=[
                        disnake.SelectOption(
                            label=fix_characters(f"{n + 1}. {t.single_title}", 47),
                            description=fix_characters(
                                f"[{time_format(t.duration) if not t.is_stream else '🔴 Live'}]. {t.authors_string}",
                                47,
                            ),
                            value=f"{n:02d}.{t.title[:96]}",
                        )
                        for n, t in enumerate(itertools.islice(queue, 25))
                    ],
                )
            )

    if self.current.ytid and self.node.lyric_support:
        data["components"][5].options.append(
            disnake.SelectOption(
                label="Visualizar letras",
                emoji=app_emoji(self.bot, "lyrics"),
                value=PlayerControls.lyrics,
                description=fit(self.tr("player.get_the_lyrics_of_the.description"), 100),
            )
        )

    if self.mini_queue_feature:
        data["components"][5].options.append(
            disnake.SelectOption(
                label=fit(self.tr("player.player_mini_queue.label"), 100),
                emoji=app_emoji(self.bot, "music_queue"),
                value=PlayerControls.miniqueue,
                description=fit(self.tr("player.enable_disable_the_player_s.description"), 100),
            )
        )

    if isinstance(self.last_channel, disnake.VoiceChannel):
        data["components"][5].options.append(
            disnake.SelectOption(
                label=fit(self.tr("player.automatic_status.label"), 100),
                emoji=app_emoji(self.bot, "speaker"),
                value=PlayerControls.set_voice_status,
                description=fit(self.tr("player.set_up_the_voice_channel.description"), 100),
            )
        )

    if not self.static and not self.has_thread:
        data["components"][5].options.append(
            disnake.SelectOption(
                label="Song-Request Thread",
                emoji=app_emoji(self.bot, "comment"),
                value=PlayerControls.song_request_thread,
                description=fit(self.tr("player.create_a_temporary_thread_to.description"), 100),
            )
        )


async def _np_interaction_edit(self, req):
    """Atualiza a mensagem a partir de uma interação."""
    data = req.data
    interaction = req.interaction

    try:
        del data["flags"]
    except KeyError:
        pass

    try:
        if interaction.response.is_done():
            await interaction.message.edit(allowed_mentions=self.allowed_mentions, **data)
        else:
            await interaction.response.edit_message(allowed_mentions=self.allowed_mentions, **data)
        self.updating = False
    except Exception:
        logger.exception("Falha em _np_interaction_edit")
    else:
        self.start_message_updater_task()
    return True
    return False


async def _np_message_edit(self, req):
    """Edita a mensagem existente, recuperando o canal em caso de falha."""
    data = req.data
    interaction = req.interaction

    self.ignore_np_once = False

    try:
        try:
            if data.get("flags"):
                data["content"] = None
                data["embed"] = None
            await self.message.edit(allowed_mentions=self.allowed_mentions, **data)
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            self.updating = False
            return True
        except Exception:
            logger.exception("Falha em _np_message_edit")

            try:
                self.text_channel = self.bot.get_channel(self.text_channel.id) or await self.bot.fetch_channel(
                    self.text_channel.id
                )
            except AttributeError, disnake.Forbidden, disnake.HTTPException:
                self.text_channel = None

            if not self.text_channel:
                self.message = None
                return True

            if isinstance(self.text_channel, disnake.Thread):
                if (
                    not self.text_channel.parent.permissions_for(self.guild.me).send_messages_in_threads
                    or not self.text_channel.permissions_for(self.guild.me).read_messages
                ):
                    self.text_channel = None
                    return True

                if self.text_channel.locked:
                    if not self.text_channel.parent.permissions_for(self.guild.me).manage_threads:
                        self.text_channel = None
                        self.message = None
                        return True
                    else:
                        await self.text_channel.edit(archived=False, locked=False)

                elif self.text_channel.archived:
                    if self.text_channel.owner_id == self.bot.user.id:
                        await self.text_channel.edit(archived=False)
                    else:
                        await self.text_channel.send(self.tr("player.unarchiving_the_thread"), delete_after=2)

                await self.message.edit(allowed_mentions=self.allowed_mentions, **data)
                await asyncio.sleep(0.5)

                # elif ((
                #              self.text_channel.archive_timestamp - disnake.utils.utcnow()).total_seconds() / 60) < (
                # thread_archive_time[self.text_channel.auto_archive_duration]):
                #    await self.text_channel.send("Evitando o tópico auto-arquivar...", delete_after=2)

            elif (
                not self.text_channel.permissions_for(self.guild.me).send_messages
                or not self.text_channel.permissions_for(self.guild.me).read_messages
            ):
                return True

        self.start_message_updater_task()
        self.updating = False
        return True
    except Exception as e:
        self.updating = False
        error_code = log_unexpected_error(logger, e, "atualização da mensagem do player")
        if self.static or self.has_thread:
            self.set_command_log(
                f"{(interaction.author.mention + ' ') if interaction else ''}houve um erro na interação{code_suffix(error_code)}.",
                "⚠️",
                controller=True,
            )
            self.update = True
            return True
    return False


async def _np_send_new(self, req):
    """Envia uma mensagem nova do player e atualiza o canal de pedidos."""
    data = req.data

    panel = getattr(self.bot.pool, "request_panel", None)
    if not self.message and self.static and panel is not None:
        # canal de pedidos único: a mensagem é do bot do painel (o autor); quem manda uma nova é ele, nunca o bot deste player
        try:
            message = await panel.resend(self.guild_id)
            if message is not None:
                await message.edit(allowed_mentions=self.allowed_mentions, **data)
                self.message = message
                await self.channel_cleanup()
        except Exception:
            logger.exception("Falha em _np_send_new")
        req.data = data
        return  # sem mensagem do painel o player espera a próxima atualização; um bot que não é o autor nunca manda a dele

    if not self.message:
        try:
            self.message = await self.text_channel.send(allowed_mentions=self.allowed_mentions, **data)
        except Exception:
            logger.exception("Falha em _np_send_new")
        else:
            if self.static:
                await self.channel_cleanup()
                data = await self.bot.get_data(self.guild_id, db_name=DBModel.guilds)
                data["player_controller"]["channel"] = str(self.text_channel.id)
                data["player_controller"]["message_id"] = str(self.message.id)
                await self.bot.update_data(self.guild_id, data, db_name=DBModel.guilds)

    req.data = data
