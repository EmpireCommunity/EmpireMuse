# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import datetime
import traceback
from collections import deque
from itertools import cycle
from time import time
from typing import Optional, Union, TYPE_CHECKING

import disnake

import empiremuse.wavelink as wavelink
from empiremuse.i18n.payload import fit_payload
from empiremuse.rules.voice_status import DEFAULT_VOICE_TEMPLATE
from empiremuse.utils.music.converters import time_format, md_link, md_code
from empiremuse.utils.music.skin_utils import skin_converter
from empiremuse.utils.others import string_to_file
from empiremuse.utils.usage import usage_of

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

from empiremuse.utils.music.models.tracks import (
    PartialTrack,
    LavalinkTrack,
)
from empiremuse.utils.music.models.player_filters import PlayerFiltersMixin
from empiremuse.utils.music.models.player_state import PlayerStateMixin
from empiremuse.utils.music.models.player_message import PlayerMessageMixin
from empiremuse.utils.music.models.player_voice import PlayerVoiceMixin
from empiremuse.utils.music.models.player_events import PlayerEventsMixin
from empiremuse.utils.music.models.player_autoqueue import PlayerAutoqueueMixin
from empiremuse.utils.music.models.player_playback import PlayerPlaybackMixin


class LavalinkPlayer(
    PlayerFiltersMixin,
    PlayerStateMixin,
    PlayerMessageMixin,
    PlayerVoiceMixin,
    PlayerEventsMixin,
    PlayerAutoqueueMixin,
    PlayerPlaybackMixin,
    wavelink.Player,
):
    bot: BotCore

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.version = 1.1
        self.volume = 100
        self.guild: disnake.Guild = kwargs.pop("guild")
        # O tradutor do servidor (``player_lifecycle``/``player_resume`` o acertam pelo idioma gravado; ``apply_language`` o refaz).
        self.tr = self.bot.pool.i18n.for_locale(
            self.bot.pool.i18n.resolve(None, getattr(self.guild, "preferred_locale", None))
        )
        self.text_channel: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.Thread] = kwargs.pop("channel")
        self.message: Optional[disnake.Message] = kwargs.pop("message", None)
        self.static: bool = kwargs.pop("static", False)
        self.skin: str = kwargs.pop("skin", None) or self.bot.pool.default_skin
        self.skin_static: str = kwargs.pop("skin_static", None) or self.bot.pool.default_static_skin
        self.custom_skin_data = kwargs.pop("custom_skin_data", {})
        self.custom_skin_static_data = kwargs.pop("custom_skin_static_data", {})
        self.queue: deque = deque()
        self.played: deque = deque(maxlen=20)
        self.queue_autoplay: deque = deque(maxlen=30)
        self.failed_tracks: deque = deque(maxlen=30)
        self.command_log_list = deque(maxlen=10)
        self.autoplay: bool = kwargs.pop("autoplay", False)
        self.nightcore: bool = False
        self.loop = False
        self.last_track: Optional[LavalinkTrack] = None
        self.locked: bool = False
        self.is_previows_music: bool = False
        self.interaction_cooldown: bool = False
        self.votes: set = set()
        self.dj: set = set()
        self.player_creator: Optional[int] = kwargs.pop("player_creator", None)
        self.filters: dict = {}
        self.idle_task: Optional[asyncio.Task] = None
        self.members_timeout_task: Optional[asyncio.Task] = None
        self.reconnect_voice_channel_task: Optional[asyncio.Task] = None
        self.idle_endtime: Optional[datetime.datetime] = None
        self.idle_start_timestamp: Optional[int] = None
        self.hint_rate = self.bot.config["HINT_RATE"]
        self.command_log: str = ""
        self.command_log_emoji: str = ""
        self.is_closing: bool = False
        self.last_message_id: Optional[int] = kwargs.pop("last_message_id", None)
        self.keep_connected: bool = kwargs.pop("keep_connected", False)
        self.update: bool = False
        self.updating: bool = False
        self.auto_update: int = 0
        self.live_lyrics_enabled = False
        self.has_lyrics = False
        self.np_original_data = None
        self.lyric_task: Optional[asyncio.Task] = None
        self.listen_along_invite = kwargs.pop("listen_along_invite", "")
        self.message_updater_task: Optional[asyncio.Task] = None
        # limitar apenas para dj's e staff's
        self.restrict_mode = kwargs.pop("restrict_mode", False)
        self.ignore_np_once = False  # não invocar player controller em determinadas situações
        self.allowed_mentions = disnake.AllowedMentions(users=False, everyone=False, roles=False)
        self.uptime = kwargs.pop("uptime", None) or int(disnake.utils.utcnow().timestamp())
        # ativar/desativar modo controller (apenas para uso em skins)
        self.controller_mode = True
        self.mini_queue_feature = False
        self.mini_queue_enabled = False
        self.is_resuming = False
        self.is_purging = False
        self.auto_pause = False
        self._session_resuming = kwargs.pop("session_resuming", False)
        self._last_channel: Optional[disnake.VoiceChannel] = None
        self._last_channel_id: Optional[int] = None
        self._rpc_update_task: Optional[asyncio.Task] = None
        self._new_node_task: Optional[asyncio.Task] = None
        self._queue_updater_task: Optional[asyncio.Task] = None
        self.auto_skip_track_task: Optional[asyncio.Task] = None
        self.track_load_task: Optional[asyncio.Task] = None
        self.native_yt: bool = True
        self.stage_title_event = False
        self.stage_title_template: str = kwargs.pop("stage_title_template", None) or DEFAULT_VOICE_TEMPLATE
        self.last_stage_title = ""
        self.voice_status_permanent = (
            True  # cópia do permanente do servidor; o ``apply_permanent_voice_status`` a atualiza
        )
        self.voice_status_pending: asyncio.Task | None = None  # o envio guardado do throttle (ver ``player_voice``)

        self.channel_purged = False

        self.temp_embed: Optional[disnake.Embed] = None
        self.prefix_info = kwargs.pop("prefix", "")

        self.start_time = disnake.utils.utcnow()
        self.start_timestamp = self.start_time.timestamp()

        self.lastfm_artists = []

        self.event_queue = asyncio.Queue()

        self.extra_info = {}

        self.initial_hints = [
            f"É possível alterar a skin/aparência do player usando o comando /change_skin ou {self.prefix_info}skin "
            f"(Apenas membros com permissão de gerenciar servidor podem usar esse comando).",
            "Dê uma nova cara para o player de música no servidor criando skins personalizadas. Experimente usando "
            f"o comando /custom_skin ou {self.prefix_info}customskin (Apenas membros com permissão de administrador "
            "podem usar esse comando).",
            "É possível definir o status automático no canal de voz com informações sobre "
            "a música que está sendo tocada no momento. Experimente usando o comando /set_voice_status ou "
            f"{self.prefix_info}stageannounce (Apenas membros com permissão de gerenciar servidor podem usar esse recurso).",
            f"Caso a música esteja com lag, audio travando etc. Experimente alterar a região do canal de voz "
            f"(caso esteja em um palco/stage use o comando {self.prefix_info}stageregion).",
        ]

        self.retry_setup_hints = False

        hint_platforms = ["youtube, soundcloud"]

        if (self.bot.spotify and not self.bot.spotify.disabled) or "spotify" in self.node.info["sourceManagers"]:
            hint_platforms.append("spotify")

        hint_platforms.append("deezer")

        if self.bot.config["LASTFM_KEY"] and self.bot.config["LASTFM_SECRET"]:
            self.initial_hints.append(
                f"Você pode vincular uma conta do last.fm para registrar as músicas que você ouvir por aqui na sua "
                f"lista de músicas tocadas. Experimente usando o comando /lastfm ou {self.prefix_info}lastfm."
            )

        if hint_platforms:
            self.initial_hints.append(
                "Você pode adicionar/integrar link de perfis/canais do " + " e ".join(hint_platforms) + " para tocar "
                f"playlists pública que tem no canal/perfil via comando {self.prefix_info}play (sem incluir "
                "nome/link) ou no comando /play (via preenchimento automático da busca). Experimente usando o "
                f'comando /fav_manager ou {self.prefix_info}favmanager selecionando a opção "integrações".'
            )

        try:
            self.initial_hints.extend(kwargs.pop("extra_hints"))
        except Exception:
            pass

        self.lastfm_users = {}

        self.hints: cycle = []
        self.current_hint: str = ""
        self.last_data: dict = {}
        self.lyric_embed: Optional[disnake.Embed] = None
        self.event_queue_task = self.bot.loop.create_task(self.hook_events())
        self.check_skins()
        self.setup_features()
        self.setup_hints()

        self.bot.dispatch("player_create", player=self)

    def __str__(self) -> str:
        return f"Servidor de música atual: {self.node.identifier} (v{self.node.version})"

    def __repr__(self):
        return (
            f"<volume={self.volume} "
            f"current_position={time_format(self.position) if self.position else 'Idling'} "
            f'queue={len(self.queue)} loop={self.loop} EQ="{self.eq}" guild="{self.guild.name}" '
            f'node="{self.node.identifier}" keep_connected="{self.keep_connected}">'
        )

    @property
    def has_thread(self):
        try:
            if isinstance(self.message.channel.parent, disnake.ForumChannel):
                return
        except AttributeError:
            pass

        return (
            self.message and self.message.thread
        )  # and not (self.message.thread.locked or self.message.thread.archived)

    @property
    def controller_link(self):
        if self.controller_mode:
            try:
                return f" {md_link('🎛️', self.message.jump_url)}"
            except AttributeError:
                try:
                    return f" {md_link('#️⃣', self.text_channel.jump_url)}"
                except AttributeError:
                    pass
        return ""

    @property
    def position(self):
        if not self.current:
            return 0

        if not self.is_playing:
            return 0

        if self.paused and not self.auto_pause:
            return min(self.last_position, self.current.duration)

        difference = (time() * 1000) - self.last_update
        position = self.last_position + difference

        if position > self.current.duration:
            return 0

        return min(position, self.current.duration)

    async def update_state(self, state: dict) -> None:
        state = state["state"]

        if not self.auto_pause:
            self.last_position = state.get("position") or 0

        self.last_update = time() * 1000
        self.position_timestamp = state.get("time", 0)
        self.ping = state.get("ping", None)

    async def report_error(
        self, embed: disnake.Embed, track: Union[LavalinkTrack, PartialTrack], error: Exception = None
    ):
        cog = self.bot.get_cog("Music")

        if cog and cog.error_report_queue:
            embed.description += (
                f"\n**Fonte:** {md_code(track.info['sourceName'])}"
                f"\n**Servidor:** {md_code(f'{self.guild.name} [{self.guild.id}]')}"
            )

            try:
                embed.description += f"\n**Canal:** {md_code(f'{self.guild.me.voice.channel.name} [{self.guild.me.voice.channel.id}]')}\n"
            except Exception:
                pass

            embed.description += f"**Data:** <t:{int(disnake.utils.utcnow().timestamp())}:F>"

            if self.guild.icon:
                embed.set_thumbnail(url=self.guild.icon.with_format("png").url)

            error_kwargs = {"embed": embed}

            if error:
                error_kwargs["file"] = string_to_file(
                    "".join(traceback.format_exception(type(error), error, error.__traceback__)),
                    "error_partialtrack.txt",
                )

            await cog.error_report_queue.put(error_kwargs)

    async def connect(self, channel_id: int, self_mute: bool = False, self_deaf: bool = False):
        await super().connect(channel_id, self_mute=self_mute, self_deaf=True)
        self._last_channel = self.bot.get_channel(channel_id)
        self._last_channel_id = channel_id

    @property
    def last_channel(self):
        try:
            return self.guild.me.voice.channel
        except AttributeError:
            return self._last_channel

    def setup_features(self):
        try:
            (
                self.bot.player_static_skins[self.skin_static] if self.static else self.bot.player_skins[self.skin]
            ).setup_features(self)
        except Exception:
            # linha temporária para resolver possíveis problemas com skins custom criadas por usuarios antes desse commit.
            self.auto_update = 0
            self.controller_mode = True

    def check_skins(self):
        if self.skin.startswith("> custom_skin: "):
            if self.skin[15:] not in self.custom_skin_data:
                self.skin = self.bot.pool.default_skin
        elif self.skin not in self.bot.pool.player_skins:
            self.skin = self.bot.pool.default_skin

        if self.skin_static.startswith("> custom_skin: "):
            if self.skin_static[15:] not in self.custom_skin_static_data:
                self.skin_static = self.bot.pool.default_static_skin
        elif self.skin_static not in self.bot.pool.player_static_skins:
            self.skin_static = self.bot.pool.default_static_skin

    def set_command_log(self, text="", emoji="", controller=False):
        if controller or not text:
            self.command_log = text
            self.command_log_emoji = emoji
        else:
            self.command_log_list.append(
                {"text": text, "emoji": emoji, "timestamp": disnake.utils.utcnow().timestamp()}
            )

    async def set_pause(self, pause: bool) -> None:
        await super().set_pause(pause)
        if usage := usage_of(self.bot):
            usage.playback_paused(self.bot, self.guild.id, pause)
        self.start_timestamp = (disnake.utils.utcnow() - datetime.timedelta(milliseconds=self.position)).timestamp()
        self.bot.dispatch("player_pause" if pause else "player_resume", player=self)

    def get_skin(self, disable=False):
        if not self.current:
            return

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

        data = fit_payload(data)

        if disable:
            data = self.get_skin()

            components = []

            for c in data.get("components", []):
                c.disabled = True
                components.append(c)

            data["components"] = components

        return data

    async def destroy(self, *, force: bool = False, inter: disnake.MessageInteraction = None):
        if usage := usage_of(self.bot):
            usage.playback_ended(
                self.bot, self.guild.id, "STOPPED"
            )  # sem TrackEnd ao destruir: fecha o evento de reprodução
        self.bot.loop.create_task(self.process_destroy(force=force, inter=inter))
