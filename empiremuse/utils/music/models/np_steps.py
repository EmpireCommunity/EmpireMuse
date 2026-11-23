# -*- coding: utf-8 -*-
"""Etapas de `LavalinkPlayer.invoke_np` (montagem e envio da mensagem do player)."""

from __future__ import annotations

import asyncio
import io
import itertools
import random
import time
import weakref
from itertools import cycle

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
from empiremuse.utils.music import card_facts
from empiremuse.rules.skins import JUKEBOX_IDS
from empiremuse.utils.render.state import RENDER_STATES
from empiremuse.utils.safe_error import safe_error

ATTACHMENT_PREFIX = "attachment://"
FALLBACK_SKIN = "muse_text"  # a skin de texto que entra quando o cartão não sai (contrato §2.2)
DISC_MOTION_TTL_S = 60.0  # a animação do disco é relida do banco no máximo a cada isto (ou num redesenho pedido)

_DISC_MOTION: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # player -> (disc_motion, lido em)


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
    if skin is not None:
        return skin.load(self), skin
    if self.static:
        data = skin_converter(self.custom_skin_static_data[self.skin_static[15:]], player=self, guild=self.guild)
    else:
        data = skin_converter(self.custom_skin_data[self.skin[15:]], player=self, guild=self.guild)
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
    now = time.monotonic()
    cached = _DISC_MOTION.get(self)
    if cached is not None and not fresh and now - cached[1] < DISC_MOTION_TTL_S:
        return cached[0]
    global_data = await self.bot.get_global_data(self.guild_id, db_name=DBModel.guilds)
    value = global_data.get("disc_motion")
    _DISC_MOTION[self] = (value, now)
    return value


async def _card_spec(self, skin, *, fresh: bool = False):
    """O ``CardSpec`` projetado pela skin: os fatos do player e o que vem da configuração (tema, idioma, animação)."""
    settings = card_facts.CardSettings(
        skin=skin.name,
        theme=skin.theme,
        locale=str(getattr(self.tr, "locale", "pt-BR")),
        disc_motion=await _disc_motion(self, fresh=fresh),
    )
    return skin.project(card_facts.build(self, settings))


def _card_failed(skin, error: BaseException) -> None:
    """Falha do cartão: só o id da skin e o resumo do erro (sem traceback: o ``CardSpec`` leva a URL da capa)."""
    logger.warning(
        "Cartão do player não saiu, vai o texto (skin %s: %s)", getattr(skin, "name", "?"), safe_error(error)
    )


def drop_card(self, data):
    """Um payload sem cartão numa mensagem que pode ter um: a edição tira o anexo antigo (``attachments=[]``; um envio
    novo o ignora, ``_send_kwargs``) e o estado de render esquece o anexo."""
    data["attachments"] = []
    RENDER_STATES.forget(self)
    return data


def without_card(self, data):
    """``drop_card`` só se a mensagem do player está com um cartão (as skins antigas seguem como estão)."""
    if RENDER_STATES.get(self).last is None:
        return data
    return drop_card(self, data)


def _shown_marker(key, rendered_key, spec):
    """O que a mensagem mostra depois de uma edição com cartão: o payload, o anexo e o título no autor."""
    return ("card", key, rendered_key, card_facts.author_title(spec))


def _text_fallback(self, data, *, retry: bool):
    """A ``muse_text`` no lugar do cartão, sem o anexo antigo; ``retry`` (vaga ocupada) = o updater tenta de novo."""
    fallback = self.bot.pool.player_skins.get(FALLBACK_SKIN)
    if fallback is not None:
        try:
            data = fallback.load(self)
        except Exception as error:  # noqa: BLE001 - sem a muse_text, vai o texto da própria skin (sem imagem)
            _card_failed(fallback, error)
    drop_card(self, data)
    if retry:
        RENDER_STATES.replace(self, upgrade_due=True)
        self.update = True
    return data


def _put_card(self, data, spec, rendered):
    """O cartão no ``embeds[0]``: a imagem, o título real no autor (sem autor em call privada) e o anexo, se novo."""
    from empiremuse.utils.render.cards.pipeline import attachment_file

    embed = data["embeds"][0]
    embed.set_image(url=f"{ATTACHMENT_PREFIX}{rendered.filename}")
    title = card_facts.author_title(spec)
    if title:
        author = embed.author
        embed.set_author(name=title, url=author.url or None, icon_url=author.icon_url or None)
    else:
        embed.remove_author()
    file = attachment_file(rendered, card_facts.alt_text(spec, self.tr))
    if file is not None:
        data["file"] = file
        data["attachments"] = []


async def _np_render(self, req, skin, data, key):
    """O cartão das skins de imagem (S5). ``True`` = nada a editar; senão ``req.data`` fica pronto (cartão ou, se o
    cartão não sai, a ``muse_text`` sem o anexo antigo). Qualquer falha (inclusive montar o ``CardSpec``) cai no texto."""
    from empiremuse.utils.render.cards.pipeline import card_pipeline

    redraw = not self.last_data
    if redraw or not _will_edit(self, req):
        RENDER_STATES.forget(self)  # mensagem nova ou redesenho pedido: o anexo antigo não vale
    shown = RENDER_STATES.get(self)
    try:
        spec = await _card_spec(self, skin, fresh=redraw)
        unchanged = shown.last is not None and shown.last.spec == spec and not shown.upgrade_due
        if unchanged and self.last_data == _shown_marker(key, shown.last, spec):
            return True
        pipe = card_pipeline(self.bot.config["COVER_HOSTS"])
        rendered = await pipe.render(self, skin.card, spec, self.tr, station_logo=_station_logo(self))
    except Exception as error:  # noqa: BLE001 - o player nunca fica sem mensagem (contrato §2.2)
        _card_failed(skin, error)
        rendered = None
    if rendered is not None and rendered.stale:
        return True  # um pedido mais novo deste player vai editar a mensagem
    if rendered is None or rendered.key is None:
        req.data = _text_fallback(self, data, retry=rendered is not None and rendered.busy)
        self.last_data = payload_key(req.data)
        return False
    if RENDER_STATES.get(self).upgrade_due:
        self.update = True  # GIF adiado, capa a caminho ou vaga ocupada: o updater redesenha (auto_update 0 não volta)
    marker = _shown_marker(key, rendered.key, spec)
    if rendered.data is None and marker == self.last_data:
        return True  # o anexo mantido e o mesmo texto: a mesma edição de novo só gastaria uma chamada
    _put_card(self, data, spec, rendered)
    self.last_data = marker
    req.data = data
    return False


def _refresh_file(data) -> bool:
    """Remonta o ``File`` do cartão dos mesmos bytes (um envio anterior o deixa no fim do buffer). ``False`` = sem ``File``."""
    file = data.get("file")
    read = getattr(getattr(file, "fp", None), "getvalue", None)
    if not callable(read):
        return False
    data["file"] = disnake.File(io.BytesIO(read()), filename=file.filename, description=file.description)
    return True


def _card_for_new_message(self, data):
    """O cartão numa mensagem nova (inclusive depois de uma edição que falhou): o ``File`` é remontado dos mesmos bytes
    (o envio anterior o deixa no fim do buffer). Sem ``File`` (anexo mantido da mensagem antiga, que não vale na nova), sai
    sem a imagem e o updater redesenha o cartão com um anexo novo."""
    embeds = data.get("embeds") or []
    image = embeds[0].image.url if embeds and isinstance(embeds[0], disnake.Embed) else None
    if not (image or "").startswith(ATTACHMENT_PREFIX):
        return
    if _refresh_file(data):
        return
    embeds[0].set_image(url=None)
    data.pop("file", None)
    RENDER_STATES.forget(self)
    self.last_data = {}
    self.update = True


async def idle_payload(self, kwargs):
    """A mensagem de ocioso na skin do player. Skins antigas e personalizadas: ``kwargs`` como está. Jukebox Texto: o
    título ``jbx.idle.title`` no autor. Skins de imagem: o cartão no estado ocioso (logo no rótulo, sem miniatura) com o
    texto e os componentes de ocioso de sempre; sem cartão, o ocioso em texto."""
    skin = _skin_object(self)
    if getattr(skin, "name", None) not in JUKEBOX_IDS:
        return without_card(self, kwargs)  # a skin mudou de uma de imagem para uma antiga: o cartão sai da mensagem
    embed = kwargs["embed"]
    data = {name: value for name, value in kwargs.items() if name != "embed"}
    data["embeds"] = [embed]
    rendered = None
    if getattr(skin, "card", None):
        from empiremuse.utils.render.cards.pipeline import card_pipeline

        RENDER_STATES.forget(self)  # o ocioso sempre leva um anexo novo (a mensagem pode ser outra)
        try:
            spec = await _card_spec(self, skin)
            pipe = card_pipeline(self.bot.config["COVER_HOSTS"])
            rendered = await pipe.render(self, skin.card, spec, self.tr, station_logo=_station_logo(self))
        except Exception as error:  # noqa: BLE001 - sem cartão, o ocioso em texto
            _card_failed(skin, error)
    if rendered is None or rendered.data is None:
        drop_card(self, data)
        embed.set_author(name=fit(self.tr("jbx.idle.title"), card_facts.AUTHOR_MAX))
        return data
    embed.set_thumbnail(url=None)
    _put_card(self, data, spec, rendered)
    return data


async def _np_data(self, req):
    """Carrega os dados da skin e confere se a mensagem mudou (pela chave estável ``payload_key``)."""
    interaction = req.interaction

    try:
        data, skin = _load_skin(self)
    except OverflowError:
        await self.process_next()
        return True

    key = payload_key(data)

    if getattr(skin, "card", None):
        if await _np_render(self, req, skin, data, key):
            await _defer(interaction)
            return True
        return False

    if RENDER_STATES.get(self).last is not None:
        drop_card(self, data)  # de uma skin de imagem para uma de texto na mesma mensagem: o cartão sai
    elif key == self.last_data:
        await _defer(interaction)
        return True

    self.last_data = key

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
                    await self.message.edit(allowed_mentions=self.allowed_mentions, **data)
                except disnake.Forbidden:
                    self.message = None
                    self.text_channel = None
                except Exception:
                    self.message = await self.text_channel.send(
                        allowed_mentions=self.allowed_mentions, **_send_kwargs(data)
                    )

    else:
        try:
            self.message_updater_task.cancel()
        except Exception:
            pass
        self.message = await self.text_channel.send(allowed_mentions=self.allowed_mentions, **_send_kwargs(data))

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

                _refresh_file(data)  # a edição que falhou já leu o anexo até o fim
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
                f"{(interaction.author.mention + ' ') if interaction else ''}houve um erro na interação{code_suffix(error_code, self.tr)}.",
                "⚠️",
                controller=True,
            )
            self.update = True
            return True
    return False


async def _np_send_new(self, req):
    """Envia uma mensagem nova do player e atualiza o canal da Jukebox."""
    data = req.data
    _card_for_new_message(self, data)

    panel = getattr(self.bot.pool, "jukebox_panel", None)
    if not self.message and self.static and panel is not None:
        # canal da Jukebox único: a mensagem é do bot do painel (o autor); quem manda uma nova é ele, nunca o bot deste player
        try:
            message = await panel.resend(self.guild_id)
            if message is not None:
                # a mensagem do painel pode ser uma que já existia: só fica o anexo que este payload manda
                await message.edit(allowed_mentions=self.allowed_mentions, **{**data, "attachments": []})
                self.message = message
                await self.channel_cleanup()
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
            if self.static:
                await self.channel_cleanup()
                data = await self.bot.get_data(self.guild_id, db_name=DBModel.guilds)
                data["player_controller"]["channel"] = str(self.text_channel.id)
                data["player_controller"]["message_id"] = str(self.message.id)
                await self.bot.update_data(self.guild_id, data, db_name=DBModel.guilds)

    req.data = data
