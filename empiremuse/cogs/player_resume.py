# -*- coding: utf-8 -*-

# nota: este sistema é totalmente experimental.
import asyncio
import logging
from contextlib import suppress
from typing import Union

import disnake
from disnake.ext import commands

from empiremuse.rules.voice_status import resolve_voice_status
from empiremuse.serialization.schema import SchemaError
from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.request_panel import fetch_panel_message
from empiremuse.utils.music.checks import can_connect, can_send_message
from empiremuse.utils.music.errors import PoolException
from empiremuse.utils.music.filters import AudioFilter
from empiremuse.utils.music import session_store
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.music.models.player_voice import schedule_permanent_voice_status
from empiremuse.utils.others import send_idle_embed, CustomContext

from empiremuse.logs.context import guild_extra

logger = logging.getLogger("empiremuse.player")


class PlayerSession(commands.Cog):
    def __init__(self, bot: BotCore):
        self.bot = bot

        if not hasattr(bot, "player_resumed"):
            bot.player_resumed = False

        if not hasattr(bot, "player_resuming"):
            bot.player_resuming = False

        if not hasattr(bot, "players_resumed"):
            bot.players_resumed = {}

        self.resume_task = bot.loop.create_task(self.resume_players())

    @commands.Cog.listener()
    async def on_player_destroy(self, player: LavalinkPlayer):
        try:
            player.queue_updater_task.cancel()
        except Exception:
            pass

        await self.delete_data(player)

    @commands.Cog.listener("on_wavelink_track_end")
    async def track_end(self, player: LavalinkPlayer, *args, **kwargs):
        if len(player.queue) > 0:
            return

        await self.save_info(player)

    @commands.is_owner()
    @commands.command(
        hidden=True, description="Salvar informações dos players na database instantaneamente.", aliases=["svplayers"]
    )
    async def saveplayers(self, ctx: CustomContext):
        await ctx.defer()

        player_count = 0

        for bot in self.bot.pool.get_all_bots():
            for player in bot.music.players.values():
                try:
                    await player.process_save_queue()
                    player_count += 1
                except Exception:
                    continue

        txt = (
            f"As informações dos players atuais foram salvos com sucesso ({player_count})!"
            if player_count
            else "Não há player ativo..."
        )
        await ctx.send(txt)

    async def queue_updater_task(self, player: LavalinkPlayer):
        while True:
            if self.bot.config["PLAYER_SESSIONS_MONGODB"] and self.bot.config["MONGO"]:
                await asyncio.sleep(self.bot.config["PLAYER_INFO_BACKUP_INTERVAL_MONGO"])
            else:
                await asyncio.sleep(self.bot.config["PLAYER_INFO_BACKUP_INTERVAL"])

            try:
                await self.save_info(player)
            except Exception:
                logger.exception("Falha em queue_updater_task", extra=guild_extra(getattr(player, "guild_id", None)))

    async def save_info(self, player: LavalinkPlayer):
        if getattr(player, "radio_id", None) is not None:
            return  # rádio (item 8): quem a retoma é o gerenciador de rádios; a sessão a traria de volta como um player comum, controlável pela Jukebox
        if not player.guild.me.voice or player.is_closing:
            return

        try:
            message_id = player.message.id
        except Exception:
            message_id = None

        try:
            text_channel_id = player.text_channel.id
        except Exception:
            text_channel_id = None
            message_id = None

        tracks = []
        played = []
        autoqueue = []
        failed_tracks = []

        if player.current:
            player.current.info["id"] = player.current.id
            if player.current.playlist_name:
                player.current.info["playlist"] = {
                    "name": player.current.playlist_name,
                    "url": player.current.playlist_url,
                }
                try:
                    player.current.info["playlist"]["thumb"] = player.current.playlist.thumb
                except Exception:
                    pass
            tracks.append(player.current.info)

        for t in player.queue:
            t.info["id"] = t.id
            if t.playlist:
                t.info["playlist"] = {"name": t.playlist_name, "url": t.playlist_url}
            tracks.append(t.info)

        for t in player.played:
            t.info["id"] = t.id
            if t.playlist:
                t.info["playlist"] = {"name": t.playlist_name, "url": t.playlist_url}
            played.append(t.info)

        for t in player.queue_autoplay:
            t.info["id"] = t.id
            autoqueue.append(t.info)

        for t in player.failed_tracks:
            t.info["id"] = t.id
            if t.playlist:
                t.info["playlist"] = {"name": t.playlist_name, "url": t.playlist_url}
            failed_tracks.append(t.info)

        try:
            vc_id = player.guild.me.voice.channel.id
        except AttributeError:
            vc_id = player.last_channel.id

        data = {
            "_id": player.guild.id,
            "version": getattr(player, "version", 1),
            "volume": player.volume,
            "nightcore": player.nightcore,
            "position": player.position,
            "voice_channel": vc_id,
            "dj": player.dj,
            "player_creator": player.player_creator,
            "static": player.static,
            "paused": player.paused and not player.auto_pause,
            "text_channel_id": text_channel_id,
            "message_id": message_id,
            "keep_connected": player.keep_connected,
            "loop": player.loop,
            "autoplay": player.autoplay,
            "stage_title_event": player.stage_title_event,
            "stage_title_template": player.stage_title_template,
            "skin": player.skin,
            "skin_static": player.skin_static,
            "custom_skin_data": {},
            "custom_skin_static_data": {},
            "uptime": player.uptime,
            "restrict_mode": player.restrict_mode,
            "mini_queue_enabled": player.mini_queue_enabled,
            "listen_along_invite": player.listen_along_invite,
            "queue": tracks,
            "played": played,
            "queue_autoplay": autoqueue,
            "failed_tracks": failed_tracks,
            "prefix_info": player.prefix_info,
            "time": disnake.utils.utcnow(),
            "lastfm_artists": player.lastfm_artists,
            "start_timestamp": player.start_timestamp,
        }

        try:
            data["last_voice_channel_id"] = player._last_channel_id
        except AttributeError:
            player._last_channel_id = vc_id
            data["last_voice_channel_id"] = vc_id

        with suppress(AttributeError):
            data["live_lyrics_status"] = player.live_lyrics_enabled
        with suppress(AttributeError):
            data["current_encoded"] = player.current_encoded
        with suppress(AttributeError):
            data["command_log_list"] = player.command_log_list

        if player.static:
            if player.skin_static.startswith("> custom_skin: "):
                custom_skin = player.skin_static[15:]
                data["custom_skin_static_data"] = {custom_skin: player.custom_skin_static_data[custom_skin]}

        elif player.skin.startswith("> custom_skin: "):
            custom_skin = player.skin[15:]
            data["custom_skin_data"] = {custom_skin: player.custom_skin_data[custom_skin]}

        try:
            await self.save_session(player, data=data)
        except Exception:
            logger.exception("Falha em save_info", extra=guild_extra(getattr(player, "guild_id", None)))

    async def resume_players(self):
        try:
            if self.bot.player_resuming:
                return

            self.bot.player_resuming = True

            await self.bot.wait_until_ready()

            while not self.bot.bot_ready:
                await asyncio.sleep(3)

        except Exception:
            logger.exception("Falha em resume_players")
            self.bot.player_resuming = False
            return

        try:
            mongo_sessions = await self.get_player_sessions_mongo()
            local_sessions = await self.get_player_sessions_local()

            data_list = {}

            if self.bot.config["PLAYER_SESSIONS_MONGODB"] and self.bot.config["MONGO"]:
                for d in local_sessions:
                    data_list[d["_id"]] = d
                    logger.debug(
                        "Migrando dados de sessões do server: %s | DB Local -> Mongo",
                        d["_id"],
                        extra=guild_extra(d["_id"]),
                    )
                    await self.save_session_mongo(d["_id"], d)
                    self.delete_data_local(d["_id"])
                for d in mongo_sessions:
                    data_list[d["_id"]] = d

            else:
                for d in mongo_sessions:
                    data_list[d["_id"]] = d
                    logger.debug(
                        "Migrando dados de sessões do server: %s | Mongo -> DB Local",
                        d["_id"],
                        extra=guild_extra(d["_id"]),
                    )
                    await self.save_session_local(d["_id"], d)
                    if self.bot.config["MONGO"]:
                        await self.delete_data_mongo(d["_id"])
                for d in local_sessions:
                    data_list[d["_id"]] = d

            mongo_sessions.clear()
            local_sessions.clear()

            hints = self.bot.config["EXTRA_HINTS"].split("||")

            for data in data_list.values():
                try:
                    self.bot.players_resumed[data["_id"]]
                except KeyError:
                    self.bot.players_resumed[data["_id"]] = self.bot.loop.create_task(
                        self.resume_player(data, hints=hints)
                    )
                    await asyncio.sleep(1)

        except Exception:
            logger.exception("Falha ao retomar players")

        self.bot.player_resumed = True

    async def update_player(
        self,
        player: LavalinkPlayer,
        voice_channel: Union[disnake.VoiceChannel, disnake.StageChannel],
        pause: bool,
        position: int,
        has_members=None,
    ):
        if not player.current:
            try:
                player.current = player.queue.popleft()
            except IndexError:
                pass

            if not player.current and player.autoplay:
                try:
                    player.current = await player.get_autoqueue_tracks()
                    position = 0
                except Exception:
                    logger.exception("Falha em update_player", extra=guild_extra(getattr(player, "guild_id", None)))

        data = {
            "volume": player.volume,
            "filters": player.filters,
        }

        await player.connect(voice_channel.id)
        await self.voice_check(voice_channel, position)

        try:
            track_id = player.current_encoded or player.current.id
        except Exception:
            track_id = None

        if track_id and has_members:
            data.update(
                {
                    "encodedTrack": track_id,
                    "position": position,
                    "paused": pause,
                }
            )
            await player.node.update_player(player.guild.id, data=data)
        else:
            await player.node.update_player(player.guild.id, data=data)
            if player.current:
                player.queue.appendleft(player.current)
            await player.process_next(start_position=position)

    async def voice_check(self, voice_channel: Union[disnake.VoiceChannel, disnake.StageChannel], position: int = 0):
        wait_counter = 30

        guild = voice_channel.guild

        while wait_counter > 1:
            if not guild.me.voice:
                wait_counter -= 1
                await asyncio.sleep(1)
                continue
            try:
                player = self.bot.music.players[voice_channel.guild.id]
                player._last_channel = voice_channel
                if player.node.version > 3:
                    player.last_position = position
            except KeyError:
                pass
            break

        if not wait_counter:
            logger.warning("Player ignorado devido a demora para conectar no canal de voz.", extra={"guild": guild.id})
            return

        if isinstance(voice_channel, disnake.StageChannel) and voice_channel.permissions_for(guild.me).mute_members:
            while not guild.me.voice:
                await asyncio.sleep(1)

            await asyncio.sleep(3)

            try:
                await guild.me.edit(suppress=False)
            except Exception as e:
                logger.warning("Falha ao falar no palco do servidor: %s", type(e).__name__, extra={"guild": guild.id})

    async def _guild_channel(self, guild: disnake.Guild, channel_id, *, fetch: bool = False):
        """O canal ``channel_id`` SE for do servidor ``guild``; canal de outro servidor (ou sem servidor) vira ``None``."""
        try:
            channel_id = int(channel_id)
            channel = self.bot.get_channel(channel_id)

            if channel is None and fetch:
                channel = await self.bot.fetch_channel(channel_id)
        except disnake.HTTPException, TypeError, ValueError:
            return None

        if channel is not None and getattr(getattr(channel, "guild", None), "id", None) != guild.id:
            logger.warning("Canal da sessão ignorado por não ser do servidor da sessão: servidor %s", guild.id)
            return None

        return channel

    @staticmethod
    def _to_id(value):
        try:
            return int(value)
        except TypeError, ValueError:
            return None

    async def resume_player(self, data: dict, hints: list = None):
        if hints is None:
            hints = []

        voice_channel = None

        try:
            guild = self.bot.get_guild(data["_id"])

            db_date = data.get("time")

            if not db_date or (not guild and (disnake.utils.utcnow() - db_date).total_seconds() > 172800):
                logger.debug("Limpando informações do player: %s", data["_id"], extra=guild_extra(data["_id"]))
                await self.delete_data(data["_id"])
                return

            if not guild:
                logger.debug(
                    "Player ignorado: %s | Servidor inexistente...", data["_id"], extra=guild_extra(data["_id"])
                )
                return

            try:
                player = self.bot.music.players[int(data["_id"])]
                started = True
            except KeyError:
                message = None
                started = False

                guild_data = await self.bot.get_data(guild.id, db_name=DBModel.guilds)

                voice_channel = await self._guild_channel(guild, data["voice_channel"])

                text_channel = None

                controller = guild_data["player_controller"]

                if controller["channel"]:
                    text_channel = await self._guild_channel(guild, controller["channel"], fetch=True)

                    if text_channel:
                        data["message_id"] = self._to_id(controller["message_id"])

                if not text_channel and data["text_channel_id"]:
                    text_channel = await self._guild_channel(guild, data["text_channel_id"], fetch=True)

                    if not text_channel:
                        data["message_id"] = None

                if not text_channel:
                    data["static"] = False
                    text_channel = voice_channel
                    data["message_id"] = None

                if text_channel:
                    if isinstance(text_channel, disnake.Thread):
                        if (
                            not text_channel.parent.permissions_for(guild.me).send_messages_in_threads
                            or not text_channel.permissions_for(guild.me).read_messages
                        ):
                            text_channel = None

                        elif text_channel.locked:
                            if not text_channel.parent.permissions_for(guild.me).manage_threads:
                                text_channel = None
                                message = None
                            else:
                                await text_channel.edit(archived=False, locked=False)

                        elif text_channel.archived:
                            if text_channel.owner_id == self.bot.user.id:
                                await text_channel.edit(archived=False)
                            else:
                                await text_channel.send("Desarquivando o tópico.", delete_after=2)

                    if text_channel:
                        try:
                            can_send_message(text_channel, self.bot.user)
                        except Exception:
                            logger.debug(
                                "Controller ignorado (falta de permissão) [canal: %s]",
                                text_channel.id,
                                extra={"guild": guild.id},
                            )
                            text_channel = None
                        else:
                            if data["message_id"]:
                                try:
                                    message = await text_channel.fetch_message(data["message_id"])
                                except disnake.NotFound, disnake.Forbidden:
                                    pass

                if data.get("static") and text_channel and controller["channel"]:
                    # sessão estática do canal único: a mensagem é a do painel, vista pelo bot do painel (o autor); só ele a edita
                    try:
                        panel_message = await fetch_panel_message(self.bot.pool, guild.id)
                    except Exception:
                        panel_message = None
                    if panel_message is not None:
                        message = panel_message

                message_without_thread = None

                if text_channel and not message and text_channel.permissions_for(guild.me).read_message_history:
                    try:
                        async for msg in text_channel.history(limit=100):
                            if msg.author.id != self.bot.user.id:
                                continue

                            if msg.reference:
                                continue

                            if msg.thread:
                                message = msg
                                break

                            if message_without_thread:
                                continue

                            message_without_thread = msg

                    except Exception as e:
                        logger.warning(
                            "Falha ao obter a mensagem do controller na retomada: servidor %s, motivo %s",
                            guild.id,
                            type(e).__name__,
                        )

                if not voice_channel or not voice_channel.permissions_for(guild.me).connect:
                    if data["voice_channel"] != (vc_id := data.get("last_voice_channel_id", data["voice_channel"])):
                        voice_channel = await self._guild_channel(guild, vc_id)

                if not voice_channel:
                    logger.debug("Player ignorado: o canal de voz não existe...", extra={"guild": guild.id})
                    try:
                        msg = "Player finalizado pois o canal de voz não existe ou foi deletado."
                        if not data["skin_static"]:
                            await text_channel.send(
                                embed=disnake.Embed(description=msg, color=self.bot.get_color(guild.me))
                            )
                        else:
                            if isinstance(text_channel, disnake.ForumChannel):
                                cog = self.bot.get_cog("Music")
                                if cog:
                                    await cog.reset_controller_db(guild.id, guild_data)
                                logger.debug(
                                    "Controller resetado por config de canal inválido (canal %s, %s)",
                                    text_channel.id,
                                    type(text_channel).__name__,
                                    extra={"guild": guild.id},
                                )
                                return
                            await send_idle_embed(text_channel, bot=self.bot, text=msg)
                    except Exception:
                        logger.exception("Falha em resume_player", extra={"guild": guild.id})
                    if (disnake.utils.utcnow() - data.get("time", disnake.utils.utcnow())).total_seconds() > 172800:
                        await self.delete_data(guild.id)
                    return

                try:
                    can_connect(voice_channel, guild=guild, bot=self.bot)
                except Exception as e:
                    logger.warning("Player ignorado: %s", type(e).__name__, extra={"guild": guild.id})
                    try:
                        msg = f"O player foi finalizado devido a falta da permissão de conectar no canal {voice_channel.mention}."
                        if not data["skin_static"]:
                            await text_channel.send(
                                embed=disnake.Embed(description=msg, color=self.bot.get_color(guild.me))
                            )
                        else:
                            await send_idle_embed(text_channel, bot=self.bot, text=msg)
                    except Exception:
                        logger.exception("Falha em resume_player", extra={"guild": guild.id})
                    if isinstance(e, PoolException):
                        await self.delete_data(guild.id)
                    return

                while True:
                    node = self.bot.music.get_best_node()

                    if not node:
                        try:
                            node = await self.bot.wait_for("wavelink_node_ready", timeout=5)
                        except asyncio.TimeoutError:
                            continue

                    break

                try:
                    player: LavalinkPlayer = self.bot.music.get_player(
                        node_id=node.identifier,
                        guild_id=guild.id,
                        cls=LavalinkPlayer,
                        guild=guild,
                        channel=text_channel,
                        message=message or message_without_thread,
                        last_message_id=data["message_id"],
                        skin=data["skin"],
                        skin_static=data["skin_static"],
                        player_creator=data["player_creator"],
                        keep_connected=data.get("keep_connected"),
                        autoplay=data.get("autoplay", False),
                        static=data["static"],
                        custom_skin_data=data.get("custom_skin_data", {}),
                        custom_skin_static_data=data.get("custom_skin_static_data", {}),
                        extra_hints=hints,
                        uptime=data.get("uptime"),
                        stage_title_template=data.get("stage_title_template"),
                        restrict_mode=data["restrict_mode"],
                        prefix=data["prefix_info"],
                        session_resuming=True,
                    )
                    player.tr = await self.bot.pool.i18n.for_guild(guild)
                    player.setup_hints()
                    player.process_hint()
                except Exception:
                    logger.exception("Falha ao criar player", extra={"guild": guild.id})
                    if (
                        not data.get("autoplay")
                        and (disnake.utils.utcnow() - data.get("time", disnake.utils.utcnow())).total_seconds() > 172800
                    ):
                        await self.delete_data(guild.id)
                    return

                player.extra_info = {}
                player._voice_state = None
                player.current_encoded = data.get("current_encoded")
                player.live_lyrics_enabled = data.get("live_lyrics_status", False)
                player.mini_queue_enabled = data.get("mini_queue_enabled")
                player.lastfm_artists = data.get("lastfm_artists")
                if isinstance(voice_channel, disnake.StageChannel) and voice_channel.id == data["voice_channel"]:
                    # Palco gravado e retomado no mesmo palco: o permanente nunca vai ao palco, então o flag salvo só está ligado por
                    # sessão de palco (2.1.1). Restaura-o com o modelo da sessão, sem ler o global nem agendar a aplicação.
                    player.stage_title_event = data.get("stage_title_event", False)
                else:
                    # O reinício é uma saída do canal: a sessão volta ao permanente do servidor (o flag e o modelo salvos não valem).
                    # Agendado como tarefa do player, com jitter, para a retomada de muitas sessões não estourar o limite do bot.
                    # Falha de leitura fecha: o padrão ligado (D1) vale para quem nunca salvou, não para quem não deu para ler; um erro
                    # de banco passageiro não pode sobrepor o desligado do dono. Fica sem status até o próximo entrar/mover.
                    # O modal/painel pode salvar e agendar a aplicação deste player enquanto a leitura espera: se o agendamento
                    # mudou, o valor lido é velho e a retomada não sobrepõe o do modal.
                    apply_before_read = getattr(player, "voice_status_apply", None)
                    try:
                        global_data = await self.bot.get_global_data(guild.id, db_name=DBModel.guilds)
                    except Exception:
                        logger.exception("Falha ao ler o status permanente do canal", extra=guild_extra(guild.id))
                    else:
                        if getattr(player, "voice_status_apply", None) is apply_before_read:
                            player.voice_status_saved_default = global_data.get("voice_status_permanent") is None
                            schedule_permanent_voice_status(player, resolve_voice_status(global_data), jitter=True)
                player.listen_along_invite = data.pop("listen_along_invite", "")
                player.command_log_list.extend(data.pop("command_log_list", []))

                if start_timestamp := data.get("start_timestamp"):
                    player.start_timestamp = start_timestamp

                player.dj = set(data["dj"])
                player.loop = data["loop"]

                player.volume = int(data["volume"])

                if player.volume != 100 and player.node.version == 3:
                    player.filters["volume"] = max(min(player.volume, 1000), 0)

                player.nightcore = data.get("nightcore")

                if player.nightcore:
                    player.filters.update(AudioFilter.timescale(pitch=1.2, speed=1.1).filter)

                if node.version == 3:
                    if player.filters:
                        await player.update_filters()

                    await player.connect(voice_channel.id)

                    await self.voice_check(voice_channel)

            tracks, playlists = self.bot.pool.process_track_cls(data["queue"])

            player.queue.extend(tracks)

            played_tracks, playlists = self.bot.pool.process_track_cls(data["played"], playlists)

            player.played.extend(played_tracks)

            queue_autoplay_tracks, playlists = self.bot.pool.process_track_cls(data.get("queue_autoplay", []))

            player.queue_autoplay.extend(queue_autoplay_tracks)

            failed_tracks, playlists = self.bot.pool.process_track_cls(data.get("failed_tracks", []), playlists)

            player.queue.extend(failed_tracks)

            if player.controller_mode is False:
                try:
                    await player.message.delete()
                    player.message = None
                except Exception:
                    pass

            if started:
                player.set_command_log(text="Os dados do player foram restaurados com sucessos!", emoji="🔰")
                player.update = True

            else:
                if player.keep_connected and not player.queue and not player.queue_autoplay:
                    if player.failed_tracks:
                        player.queue.extend(reversed(player.failed_tracks))
                        player.failed_tracks.clear()
                    if not player.queue:
                        player.queue.extend(player.played)
                        player.played.clear()

                player.set_command_log(text="O player foi restaurado com sucesso!", emoji="🔰")

                player._last_channel = voice_channel

                try:
                    check = any(
                        m for m in voice_channel.members if not m.bot or not (m.voice.deaf or m.voice.self_deaf)
                    )
                except Exception:
                    check = None

                if not check:
                    player.auto_pause = True

                try:
                    pause = data.get("paused") and check

                    start_position = int(float(data.get("position", 0))) if player.queue else 0

                    if player.node.version > 3:
                        await self.update_player(
                            player=player,
                            voice_channel=voice_channel,
                            pause=pause,
                            position=start_position,
                            has_members=check,
                        )
                        await player.invoke_np()
                    else:
                        await player.process_next(start_position=start_position)
                        if pause:
                            await player.set_pause(True)
                            if not check:
                                player.start_members_timeout(check=False)

                    player._session_resuming = False
                except Exception:
                    logger.exception("Falha na reprodução da música ao retomar o player", extra={"guild": guild.id})
                    return

            logger.debug("Player retomado - servidor de música: %s", player.node.identifier, extra={"guild": guild.id})

        except Exception:
            logger.exception("Falha crítica ao retomar players")

    def _uses_mongo(self) -> bool:
        return bool(self.bot.config["PLAYER_SESSIONS_MONGODB"] and self.bot.config["MONGO"])

    async def get_player_sessions_mongo(self):
        if not self.bot.config["MONGO"] or self.bot.pool.mongo_database is None:
            return []

        return await session_store.read_mongo_sessions(self.bot.pool.mongo_database, str(self.bot.user.id))

    async def get_player_sessions_local(self):
        return await asyncio.get_running_loop().run_in_executor(
            None, session_store.read_local_sessions, self.bot.user.id
        )

    async def _in_thread(self, id_: Union[int, str], data: dict, function, *leading):
        """Roda ``function`` (validação e codificação pesadas) numa thread com a cópia dos dados tirada aqui, no laço.

        Devolve o resultado, ou ``None`` (com aviso só com o servidor e o motivo) se os dados não valem.
        """
        try:
            copy_of_data = session_store.snapshot(data)
            return await asyncio.get_running_loop().run_in_executor(None, function, *leading, copy_of_data)
        except SchemaError as error:
            logger.warning(
                "Sessão não salva (dados incompatíveis): servidor %s, motivo %s",
                id_,
                error.code,
                extra=guild_extra(id_),
            )
            return None

    async def save_session_mongo(self, id_: Union[int, str], data: dict):
        document = await self._in_thread(id_, data, session_store.encode_for_mongo)
        if document is None:
            return

        await self.bot.pool.mongo_database.update_data(
            id_=str(id_),
            data=document,
            collection=session_store.NEW_COLLECTION,
            db_name=str(self.bot.user.id),
        )

    async def save_session_local(self, id_: Union[int, str], data: dict):
        try:
            await self._in_thread(id_, data, session_store.encode_for_disk, self.bot.user.id, id_)
        except OSError as error:
            logger.warning(
                "Sessão não salva (falha ao gravar): servidor %s, motivo %s",
                id_,
                type(error).__name__,
                extra=guild_extra(id_),
            )

    async def save_session(self, player: LavalinkPlayer, data: dict):
        try:
            player = player.bot.music.players[player.guild.id]
        except Exception:
            try:
                player.queue_updater_task.cancel()
            except Exception:
                pass
            return

        try:
            if self.bot.config["PLAYER_SESSIONS_MONGODB"] and self.bot.config["MONGO"]:
                await self.save_session_mongo(player.guild.id, data)
            else:
                await self.save_session_local(player.guild.id, data)

        except asyncio.CancelledError as e:
            logger.debug("Salvamento cancelado: %s", type(e).__name__)

    async def delete_data_mongo(self, id_: Union[LavalinkPlayer, int]):
        await session_store.delete_mongo_session(self.bot.pool.mongo_database, str(self.bot.user.id), id_)

    def delete_data_local(self, id_: Union[LavalinkPlayer, int]):
        session_store.delete_local_session(self.bot.user.id, id_)

    async def delete_data(self, player: Union[LavalinkPlayer, int]):
        try:
            guild_id = player.guild.id
        except AttributeError:
            guild_id = int(player)

        if self._uses_mongo():
            await self.delete_data_mongo(guild_id)
        else:
            self.delete_data_local(guild_id)

    def cog_unload(self):
        try:
            self.resume_task.cancel()
        except Exception:
            pass

        for guild_id in list(self.bot.players_resumed):
            try:
                self.bot.players_resumed[guild_id].cancel()
            except Exception:
                pass
            try:
                del self.bot.players_resumed[guild_id]
            except KeyError:
                continue


def setup(bot: BotCore):
    bot.add_cog(PlayerSession(bot))
