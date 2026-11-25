# -*- coding: utf-8 -*-
"""Etapas de `LavalinkPlayer.invoke_np` (montagem e envio da mensagem do player)."""

from __future__ import annotations

import asyncio
import itertools
import time
import weakref

import disnake

from empiremuse.utils.music.jukebox_ui import autoplay_label
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
from empiremuse.utils.music import card_facts, v2
from empiremuse.utils.music.models import np_v2
from empiremuse.utils import jukebox_panel
from empiremuse.rules.skins import JUKEBOX_IDS
from empiremuse.utils.music.private_call import skin_view
from empiremuse.utils.jukebox_panel import player_hides_details

DISC_MOTION_TTL_S = 60.0  # a animação do disco é relida do banco no máximo a cada isto (ou num redesenho pedido)

_DISC_MOTION: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # player -> (disc_motion, lido em)
_clock = time.monotonic  # trocável nos testes


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


def payload_key(value):
    """A chave estável do payload: embeds por ``to_dict()`` e componentes por ``to_component_dict()`` (o ``==`` do disnake
    compara botões e menus por identidade, então dois ``load()`` iguais nunca seriam iguais); o resto como está."""
    if isinstance(value, disnake.Embed):
        return ("embed", value.to_dict())
    to_component = getattr(value, "to_component_dict", None)
    if callable(to_component):
        return ("component", to_component())
    if isinstance(value, dict):
        return {name: payload_key(item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [payload_key(item) for item in value]
    return value


def _skin_object(self):
    """O objeto da skin do modo atual (``None`` para skin personalizada)."""
    if self.static:
        if self.skin_static.startswith("> custom_skin: "):
            return None
        return self.bot.pool.player_static_skins[self.skin_static]
    if self.skin.startswith("> custom_skin: "):
        return None
    return self.bot.pool.player_skins[self.skin]


def _load_skin(self):
    """O payload da skin e o objeto dela (``None`` para skin personalizada)."""
    skin = _skin_object(self)
    view = skin_view(self)  # call privada: Clássicas e personalizadas veem a faixa mascarada
    if skin is not None:
        return skin.load(view), skin
    if self.static:
        data = skin_converter(self.custom_skin_static_data[self.skin_static[15:]], player=view, guild=self.guild)
    else:
        data = skin_converter(self.custom_skin_data[self.skin[15:]], player=view, guild=self.guild)
    return data, None


def _will_edit(self, req) -> bool:
    """O ``invoke_np`` vai editar a mensagem que já está no canal (e não mandar outra)? Mesma conta do ``invoke_np``."""
    if req.interaction:
        return True
    return bool(self.message) and bool(
        self.ignore_np_once or self.has_thread or self.static or not req.force or self.is_last_message()
    )


def _station_logo(self):
    directory = getattr(getattr(self.bot.pool, "radio", None), "directory", None)
    if directory is None:
        return None

    async def read(uuid):
        return (await directory.logo_png(uuid)).png

    return read


async def _disc_motion(self, *, fresh: bool = False):
    """A animação do disco do servidor, sem uma leitura do banco a cada atualização do player (``fresh`` = reler)."""
    now = _clock()
    cached = _DISC_MOTION.get(self)
    if cached is not None and not fresh and now - cached[1] < DISC_MOTION_TTL_S:
        return cached[0]
    global_data = await self.bot.get_global_data(self.guild_id, db_name=DBModel.guilds)
    value = global_data.get("disc_motion")
    _DISC_MOTION[self] = (value, now)
    return value


def _refresh_file(data) -> bool:
    """Remonta o ``File`` do disco dos mesmos bytes (um envio anterior o deixa no fim do buffer). ``False`` = sem ``File``."""
    if data.get("files"):
        return np_v2.refresh_files(data)
    return False


async def idle_payload(self, kwargs):
    """A mensagem de ocioso na skin do player. Skins antigas e personalizadas: ``kwargs`` como está. Jukebox em V2: o ocioso
    V2. Jukebox Texto: o título ``jbx.idle.title`` no autor, sem anexo."""
    skin = _skin_object(self)
    if getattr(skin, "v2", False):  # Jukebox em V2: o ocioso também é V2 (nunca um embed numa mensagem que pode ser V2)
        np_v2.forget(self)  # o disco sai da mensagem (``attachments=[]``) e o próximo desenho o manda de novo
        return skin.idle(self, kwargs)
    if getattr(skin, "name", None) not in JUKEBOX_IDS:
        return kwargs
    embed = kwargs["embed"]
    data = {name: value for name, value in kwargs.items() if name != "embed"}
    data["embeds"] = [embed]
    data["attachments"] = []  # um disco que estava na mensagem sai (um envio novo ignora ``attachments``)
    embed.set_author(name=fit(self.tr("jbx.idle.title"), card_facts.AUTHOR_MAX))
    return data


async def _np_v2(self, req, skin):
    """A skin em Components V2 (``muse_jukebox``): o disco e o payload V2 vêm do ``np_v2``."""
    redraw = not self.last_data
    new_message = redraw or not _will_edit(self, req)
    return await np_v2.render(
        self,
        req,
        skin,
        redraw=redraw,
        new_message=new_message,
        key_of=payload_key,
        defer=_defer,
        disc_motion=_disc_motion,
        station_logo=_station_logo(self),
    )


async def _np_data(self, req):
    """Carrega os dados da skin e confere se a mensagem mudou (pela chave estável ``payload_key``)."""
    interaction = req.interaction

    if getattr(_skin_object(self), "v2", False):
        return await _np_v2(self, req, _skin_object(self))

    try:
        data, skin = _load_skin(self)
    except OverflowError:
        req.superseded = True
        await self.process_next()
        return True

    key = payload_key(data)

    if key == self.last_data:
        await _defer(interaction)
        return True

    self.last_data = key

    if not data.get("flags"):
        try:
            if self.static and isinstance(self.text_channel.parent, disnake.ForumChannel):
                # call privada: o título real nunca vai ao conteúdo, em nenhuma skin (D10)
                private = jukebox_panel.player_hides_details(self)
                title = self.tr("jbx.hidden_track") if private else self.current.title
                data["content"] = f"`{'▶️' if not self.paused else '⏸️'} {fix_characters(title, 50)}` |\n\n" + (
                    data.get("content") or ""
                )
        except Exception:
            pass

    req.data = data
    return False


async def _defer(interaction):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except Exception:
        pass


def _send_kwargs(data):
    """``attachments`` só vale na edição (``TextChannel.send`` não o aceita); uma mensagem nova leva só o ``file``."""
    return {name: value for name, value in data.items() if name != "attachments"}


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
                    await self.message.edit(
                        allowed_mentions=self.allowed_mentions, **v2.adapt_for(self.message, data, where="np_plain")
                    )
                except disnake.Forbidden:
                    self.message = None
                    self.text_channel = None
                except Exception:
                    self.message = await self.text_channel.send(
                        allowed_mentions=self.allowed_mentions, **_send_kwargs(data)
                    )
                    req.published = True
                else:
                    req.published = True

    else:
        try:
            self.message_updater_task.cancel()
        except Exception:
            pass
        self.message = await self.text_channel.send(allowed_mentions=self.allowed_mentions, **_send_kwargs(data))
        req.published = True

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
                            autoplay_label(self),
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

    private = player_hides_details(self)  # call privada, fora do chat dela: sem o select da fila nem a letra (D10)

    if self.static and not private:
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

    if self.current.ytid and self.node.lyric_support and not private:
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
                label="Jukebox Thread",
                emoji=app_emoji(self.bot, "comment"),
                value=PlayerControls.jukebox_thread,
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

    data = v2.adapt_for(
        interaction.message, data, where="np_interaction", expected=_in_thread(self, interaction.message)
    )
    try:
        if interaction.response.is_done():
            await interaction.message.edit(allowed_mentions=self.allowed_mentions, **data)
        else:
            await interaction.response.edit_message(allowed_mentions=self.allowed_mentions, **data)
        self.updating = False
    except Exception:
        logger.exception("Falha em _np_interaction_edit")
    else:
        req.published = True
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
            await self.message.edit(
                allowed_mentions=self.allowed_mentions,
                **v2.adapt_for(self.message, data, where="np_edit", expected=_in_thread(self, self.message)),
            )
            req.published = True
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

                _refresh_file(data)  # a edição que falhou já leu o anexo até o fim
                await self.message.edit(
                    allowed_mentions=self.allowed_mentions,
                    **v2.adapt_for(self.message, data, where="np_edit", expected=_in_thread(self, self.message)),
                )
                req.published = True
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
                f"{(interaction.author.mention + ' ') if interaction else ''}houve um erro na interação{code_suffix(error_code, self.tr)}.",
                "⚠️",
                controller=True,
            )
            self.update = True
            return True
    return False


def needs_replacement(self, req) -> bool:
    """V2 -> V1: a mensagem do player é V2 e o payload é de embed. A flag V2 não desliga, então só uma mensagem nova serve."""
    message = req.interaction.message if req.interaction else self.message
    if _in_thread(self, message):
        return False  # apagar a mensagem-mãe de um tópico ou a inicial de um post de fórum o deixaria órfão: a edição converte para V2
    return v2.is_v2(message) and not np_v2.is_v2_payload(req.data)


def _in_thread(self, message) -> bool:
    """A mensagem é a mãe de um tópico de pedidos ou a inicial de um post de fórum: apagá-la deixaria o tópico órfão."""
    if getattr(self, "has_thread", False) or getattr(message, "thread", None) is not None:
        return True  # a mensagem-mãe de um tópico de pedidos
    channel = getattr(message, "channel", None)
    # a mensagem inicial de um post de fórum (tem o id do próprio post); as outras mensagens de um tópico podem ser trocadas
    return isinstance(channel, disnake.Thread) and getattr(message, "id", None) == getattr(channel, "id", object())


async def _np_replace(self, req):
    """A mensagem nova vai ANTES de a velha ser apagada (estático: o painel reenvia e apaga a antiga). Se o envio falha, a
    velha continua no lugar e o updater tenta de novo."""
    old = self.message
    if req.interaction is not None:
        await _defer(req.interaction)
        old = old or req.interaction.message
        req.interaction = None
    req.replace = True
    np_v2.forget(self)
    self.message = None
    await _np_send_new(self, req)
    if self.message is None:
        self.message = old
        self.last_data = {}
        return
    if not self.static and old is not None:
        try:
            await old.delete()
        except disnake.HTTPException:
            pass  # já tinha sumido


async def _np_send_new(self, req):
    """Envia uma mensagem nova do player e atualiza o canal da Jukebox."""
    data = req.data
    np_v2.ensure_files(self, req, data)  # a Thumbnail V2 de uma mensagem nova leva o disco junto

    panel = getattr(self.bot.pool, "jukebox_panel", None)
    if not self.message and self.static and panel is not None:
        # canal da Jukebox único: a mensagem é do bot do painel (o autor); quem manda uma nova é ele, nunca o bot deste player
        try:
            message = await panel.resend(
                self.guild_id, **({"force": True, "as_v2": False} if getattr(req, "replace", False) else {})
            )
            if message is not None:
                # o player já é dono da mensagem (``resend(force=True)`` apagou a velha): se a edição falhar, a próxima tentativa
                # edita ESTA, nunca restaura a referência apagada
                self.message = message
                try:
                    # a mensagem do painel pode ser uma que já existia: só fica o anexo que este payload manda
                    await message.edit(
                        allowed_mentions=self.allowed_mentions,
                        **v2.adapt_for(message, {**data, "attachments": []}, where="np_painel"),
                    )
                except Exception:
                    self.last_data = {}
                    self.update = True
                    raise
                await self.channel_cleanup()
                req.published = True
        except Exception:
            logger.exception("Falha em _np_send_new")
        req.data = data
        return  # sem mensagem do painel o player espera a próxima atualização; um bot que não é o autor nunca manda a dele

    if not self.message:
        try:
            self.message = await self.text_channel.send(allowed_mentions=self.allowed_mentions, **_send_kwargs(data))
        except Exception:
            logger.exception("Falha em _np_send_new")
        else:
            req.published = True
            if self.static:
                await self.channel_cleanup()
                data = await self.bot.get_data(self.guild_id, db_name=DBModel.guilds)
                data["player_controller"]["channel"] = str(self.text_channel.id)
                data["player_controller"]["message_id"] = str(self.message.id)
                await self.bot.update_data(self.guild_id, data, db_name=DBModel.guilds)

    req.data = data
