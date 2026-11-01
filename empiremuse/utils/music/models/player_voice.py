# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import datetime
import random
import time
from typing import TYPE_CHECKING, Any

import disnake

import empiremuse.wavelink as wavelink
from empiremuse.rules.voice_status import (
    VOICE_STATUS_BOT_RATE,
    VOICE_STATUS_MIN_INTERVAL,
    VOICE_STATUS_REAPPLY_INTERVAL,
    VOICE_STATUS_RESUME_JITTER,
    VoiceStatusConfig,
    fit_voice_status,
    render_voice_status,
    voice_status_value,
)
from empiremuse.utils.music.checks import VOICE_STATUS_PERMISSION, can_connect, voice_status_blocked_channel
from empiremuse.utils.music.converters import time_format
from empiremuse.utils.music.errors import (
    GenericError,
    PoolException,
    code_suffix,
    log_unexpected_error,
)
from empiremuse.utils.others import music_source_emoji

if TYPE_CHECKING:
    pass

from empiremuse.utils.music.models.helpers import (
    emoji_pattern,
    logger,
)


_UNSET: Any = object()  # "nenhum texto pendente" (``None`` é um texto: limpar o canal)


class VoiceStatusBucket:
    """Ficha de um bot: ``VOICE_STATUS_BOT_RATE`` PUT/s sem rajada (uma ficha a cada ``1/taxa`` s), dividida pelos players dele."""

    def __init__(self, rate: float = VOICE_STATUS_BOT_RATE) -> None:
        self._interval = 1 / rate
        self._last: float | None = None

    def try_acquire(self) -> bool:
        now = time.monotonic()
        if self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        return True

    async def acquire(self) -> None:
        while not self.try_acquire():
            assert self._last is not None
            await asyncio.sleep(max(self._last + self._interval - time.monotonic(), 0.001))


def _bot_bucket(bot: Any) -> VoiceStatusBucket:
    """O bucket do bot, criado sob demanda (os players do mesmo bot dividem a mesma ficha)."""
    bucket = getattr(bot, "voice_status_bucket", None)
    if not isinstance(bucket, VoiceStatusBucket):
        bucket = VoiceStatusBucket()
        bot.voice_status_bucket = bucket
    return bucket


def schedule_permanent_voice_status(player: Any, cfg: VoiceStatusConfig, *, jitter: bool = False) -> asyncio.Task:
    """Agenda ``apply_permanent_voice_status`` como a tarefa do player (a anterior é cancelada; o destroy cancela esta).

    Quem chama (retomada, modal, painel) nunca espera a aplicação em linha. A tarefa começa na hora, de forma síncrona, até a
    primeira espera (o jitter), então o modelo e o flag já valem quando esta função volta.
    """
    previous = getattr(player, "voice_status_apply", None)
    if previous is not None and not previous.done():
        previous.cancel()

    async def run() -> None:
        try:
            await player.apply_permanent_voice_status(cfg, jitter=jitter)
        except Exception:
            logger.exception("Falha ao aplicar o status permanente do canal")

    task = asyncio.Task(run(), eager_start=True)
    player.voice_status_apply = task
    return task


class PlayerVoiceMixin:
    async def reconnect_voice_channel(self):
        channel_check_retries = 0

        while True:
            try:
                self.bot.music.players[self.guild_id]
            except KeyError:
                return

            try:
                vc = self.bot.get_channel(self.last_channel.id)
            except AttributeError:
                vc = None

            if not vc:
                if channel_check_retries < 3:
                    channel_check_retries += 1
                    await asyncio.sleep(3 * channel_check_retries)
                    continue

                msg = self.tr("player.the_voice_channel_was_deleted")

                if self.static:
                    self.set_command_log(msg, controller=True)
                    await self.destroy()
                    return

                else:
                    if self.text_channel:
                        try:
                            self.bot.loop.create_task(
                                self.text_channel.send(
                                    embed=disnake.Embed(description=msg, color=self.bot.get_color(self.guild.me)),
                                    delete_after=7,
                                )
                            )
                        except Exception:
                            logger.exception("Falha em reconnect_voice_channel")
                    await self.destroy()
                    return

            if self.guild.me.voice:
                if (
                    isinstance(vc, disnake.StageChannel)
                    and self.guild.me not in vc.speakers
                    and vc.permissions_for(self.guild.me).mute_members
                ):
                    try:
                        await self.guild.me.edit(suppress=False)
                    except Exception:
                        logger.exception("Falha em reconnect_voice_channel")
                return

            if self.is_closing:
                return

            if not self._new_node_task:
                try:
                    can_connect(vc, self.guild, bot=self.bot)
                except (GenericError, PoolException) as e:
                    self.set_command_log(
                        self.tr("player.a_failure_occurred_while_reconnecting_2", e=str(e)), controller=True
                    )
                    self.update = True
                except Exception as e:
                    code = log_unexpected_error(logger, e, "reconexão do player no canal de voz")
                    self.set_command_log(
                        self.tr("player.a_failure_occurred_while_reconnecting", code_suffix=str(code_suffix(code))),
                        controller=True,
                    )
                    self.update = True
                else:
                    if (ping := round(self.bot.latency * 1000)) > 250:
                        voice_msg = self.tr("player.i_reconnected_to_the_voice", ping=str(ping))
                    elif self.keep_connected:
                        voice_msg = self.tr("player.i_noticed_an_attempt_to", id=str(vc.id))
                    else:
                        voice_msg = None

                    if not voice_msg:
                        self.set_command_log(
                            text=self.tr("player.the_player_was_ended_due", id=str(vc.id)),
                            emoji="⚠️",
                            controller=True,
                        )
                        await self.destroy()
                        return

                    try:
                        await self.connect(vc.id)
                        self.set_command_log(
                            text=self.tr("player.if_you_really_want_to", voice_msg=str(voice_msg)),
                            emoji="⚠️",
                            controller=True,
                        )
                        self.update = True
                        await asyncio.sleep(5)
                        continue
                    except Exception:
                        logger.exception("Falha em reconnect_voice_channel")

            await asyncio.sleep(30)

    async def channel_cleanup(self):
        if not self.text_channel:
            return

        try:
            parent = self.text_channel.parent
        except AttributeError:
            pass
        else:
            if (
                isinstance(parent, disnake.ForumChannel)
                and self.text_channel.owner_id == self.bot.user.id
                and self.text_channel.message_count > 1
            ):
                try:
                    await self.text_channel.purge(
                        check=lambda m: m.channel.id != m.id and (not m.pinned or not m.is_system())
                    )
                except Exception:
                    pass
                self.channel_purged = True
                return

        def check_current_message(m: disnake.Message):
            try:
                return m.id != self.message.id
            except AttributeError:
                return True

        if isinstance(self.text_channel, disnake.Thread):
            check = lambda m: (
                m.id != self.last_message_id
                and not not m.pinned
                and check_current_message(m)
                and (not m.is_system() or m.type != disnake.MessageType.channel_name_change)
            )
        else:
            check = lambda m: m.id != self.last_message_id and not m.pinned and check_current_message(m)

        try:
            await self.text_channel.purge(check=check)
        except Exception:
            logger.warning("Falha ao limpar mensagens do canal %s", self.text_channel.id, exc_info=True)

        self.channel_purged = True

    async def members_timeout(self, check: bool, force: bool = False, idle_timeout=None):
        if check:
            try:
                self.auto_skip_track_task.cancel()
            except Exception:
                pass

            self.auto_skip_track_task = None

            if self.current and self.auto_pause:
                self.auto_pause = False

                try:
                    self.set_command_log(
                        emoji="🔋", text=self.tr("player.the_resource_saving_mode_was"), controller=True
                    )
                    if self.current:
                        self.queue.insert(0, self.current)
                    await self.process_next(start_position=self.position)
                except Exception:
                    logger.exception("Falha em members_timeout")
            else:
                self.auto_pause = False
            return

        if not force:
            try:
                vc = self.guild.me.voice.channel
            except AttributeError:
                vc = self.last_channel

            if vc and [m for m in vc.members if not m.bot and not (m.voice.deaf or m.voice.self_deaf)]:
                try:
                    self.auto_skip_track_task.cancel()
                except Exception:
                    pass
                return

            if self.auto_pause:
                return

            if not idle_timeout:
                idle_timeout = self.bot.config["WAIT_FOR_MEMBERS_TIMEOUT"]

            await asyncio.sleep(idle_timeout)

            try:
                vc = self.guild.me.voice.channel
            except AttributeError:
                vc = self.last_channel

            if vc and [m for m in vc.members if not m.bot and not (m.voice.deaf or m.voice.self_deaf)]:
                try:
                    self.auto_skip_track_task.cancel()
                except Exception:
                    pass
                return

        if self.keep_connected:
            if self.paused or self.auto_pause:
                return

            self.auto_pause = True
            track = self.current
            if self.is_playing:
                await self.stop()
            self.current = track
            try:
                self.auto_skip_track_task.cancel()
            except Exception:
                pass
            self.set_command_log(
                emoji="🪫",
                text=self.tr("player.the_player_is_in_resource", channel_id=str(self.channel_id)),
                controller=True,
            )
            self.update = True
            self.start_auto_skip()
            await self.update_stage_topic()

        else:
            if self.is_closing:
                return

            try:
                vc = self.guild.me.voice.channel
            except AttributeError:
                vc = self.last_channel

            if vc and [m for m in vc.members if not m.bot]:
                return

            msg = self.tr("player.the_player_was_turned_off_2") + (f" <#{vc.id}>" if vc else "") + "..."

            self.command_log = msg

            if not self.controller_mode:
                embed = disnake.Embed(description=f"**{msg}**", color=self.bot.get_color(self.guild.me))
                try:
                    await self.text_channel.send(embed=embed, allowed_mentions=self.allowed_mentions)
                except Exception:
                    pass

            await self.destroy()

    def start_members_timeout(self, check: bool, force: bool = False, idle_timeout=None):
        try:
            self.members_timeout_task.cancel()
        except Exception:
            pass
        self.members_timeout_task = self.bot.loop.create_task(
            self.members_timeout(check=check, force=force, idle_timeout=idle_timeout)
        )

    voice_status_pending: asyncio.Task | None = None  # tarefa que espera a janela/ficha e envia o último texto guardado
    voice_status_reapply: asyncio.Task | None = None  # UMA por player: reenvia o status depois de edição manual
    _voice_status_pending_text: Any = _UNSET
    _voice_status_last_put: float | None = None
    voice_status_apply: asyncio.Task | None = None  # a aplicação agendada por quem chama (retomada, modal, painel)
    voice_status_saved_default = False  # o permanente salvo nunca foi escolhido (``None``): vale o padrão ligado
    _voice_status_default_noticed = False
    _voice_status_permission_noticed = False

    def _voice_status_channel(self):
        voice = getattr(self.guild.me, "voice", None)
        return voice.channel if voice else None

    async def _send_voice_status(self, request, channel_id: int | None = None) -> bool:
        """Espera o envio do status/tópico ao Discord; ``True`` se deu certo.

        Falta de permissão (403) desliga o status com um aviso no player; os demais ``HTTPException`` ficam em uma linha por
        (status, código) e o status continua ligado. O log leva só status, código e IDs (nunca título, nome nem corpo do erro).
        """
        if channel_id is None:
            channel_id = getattr(self._voice_status_channel(), "id", None)
        try:
            await request
        except disnake.Forbidden as e:
            if not self.stage_title_event:
                return False  # outro envio simultâneo já desligou e avisou
            self.stage_title_event = False
            self.set_command_log(
                emoji="❌",
                text=self.tr("player.the_automatic_status_was_turned"),
                controller=True,
            )
            self.update = True
            logger.warning(
                "Sem permissão para definir o status do canal (status=%s, código=%s, guild=%s, canal=%s)",
                e.status,
                e.code,
                self.guild.id,
                channel_id,
            )
        except disnake.HTTPException as e:
            logged = getattr(self, "_voice_status_http_errors", None)
            if logged is None:
                logged = self._voice_status_http_errors = set()
            if (e.status, e.code) not in logged:
                logged.add((e.status, e.code))
                logger.warning(
                    "Falha HTTP ao definir o status do canal (status=%s, código=%s, guild=%s, canal=%s)",
                    e.status,
                    e.code,
                    self.guild.id,
                    channel_id,
                )
        except Exception:
            logger.exception("Falha em update_stage_topic")
        else:
            return True
        return False

    def _cancel_voice_status_tasks(self) -> None:
        """Cancela o envio pendente e a reaplicação (desligar, mover e destruir o player)."""
        self._voice_status_pending_text = _UNSET
        current = asyncio.current_task()
        for name in ("voice_status_pending", "voice_status_reapply", "voice_status_apply"):
            task = getattr(self, name, None)
            setattr(self, name, None)
            if task is not None and task is not current and not task.done():
                task.cancel()

    async def disable_voice_status(self, *, channel: disnake.abc.GuildChannel | None = None) -> None:
        """Desliga o status da sessão e limpa o canal (``channel`` é o canal antigo, ao mover; senão o atual). Idempotente.

        Zera o flag ANTES de limpar: um ``track_start`` ou um envio pendente durante o clear não reescreve o status.
        """
        self.stage_title_event = False
        self._cancel_voice_status_tasks()
        if channel is None:
            channel = self._voice_status_channel()
        if not isinstance(channel, disnake.VoiceChannel) or not self.last_stage_title:
            return
        self.last_stage_title = ""
        await self._send_voice_status(
            self.bot.edit_voice_channel_status(status=None, channel_id=channel.id), channel_id=channel.id
        )

    def _notify_missing_voice_status_permission(self) -> None:
        if self._voice_status_permission_noticed:
            return
        self._voice_status_permission_noticed = True
        self.set_command_log(
            emoji="❌",
            text=self.tr("player.the_channel_s_automatic_status", voice_status_permission=str(VOICE_STATUS_PERMISSION)),
            controller=True,
        )
        self.update = True

    async def apply_permanent_voice_status(self, cfg: VoiceStatusConfig, *, jitter: bool = False) -> None:
        """O "voltar ao permanente": a sessão passa a seguir o modelo e o flag do servidor (liga e envia, ou desliga e limpa).

        O palco nunca recebe o permanente: no palco só grava ``voice_status_permanent`` (nem modelo, nem flag, nem tarefas, nem
        API), para não derrubar uma sessão de palco nem dar o modelo do servidor a um palco sem sessão. Sem canal, desliga a sessão
        (zera o flag e cancela as tarefas, sem API): um flag preso fora do canal faria o próximo entrar pular o permanente. Com
        ``jitter`` espera 0 a ``VOICE_STATUS_RESUME_JITTER`` s antes de ligar (retomada de muitas sessões; o canal só é conferido
        depois da espera, quando o bot já entrou). A permissão é revalidada a cada chamada, porque é por canal.
        """
        if self.is_closing:
            return  # o save do permanente durante o destroy não escreve depois da limpeza do cleanup
        self.voice_status_permanent = cfg.permanent
        if jitter and cfg.permanent:
            await asyncio.sleep(random.uniform(0, VOICE_STATUS_RESUME_JITTER))
            if self.is_closing or not self.voice_status_permanent:
                return
        channel = self._voice_status_channel()
        if not isinstance(channel, disnake.VoiceChannel):
            if channel is None:
                await self.disable_voice_status()
            return
        self.stage_title_template = cfg.template
        if not cfg.permanent:
            await self.disable_voice_status()
            return
        if voice_status_blocked_channel(self.guild):
            self.stage_title_event = False
            self._notify_missing_voice_status_permission()
            return
        self.stage_title_event = True
        self._notify_voice_status_default()
        await self.update_stage_topic(force=True)
        self._ensure_voice_status_reapply()

    def _notify_voice_status_default(self) -> None:
        """I-S3: com o permanente nunca escolhido, a 1ª aplicação da sessão avisa UMA vez que o status está ligado por padrão."""
        if not self.voice_status_saved_default or self._voice_status_default_noticed:
            return
        self._voice_status_default_noticed = True
        self.set_command_log(
            emoji="ℹ️",
            text=self.tr("player.channel_status_is_on_by"),
            controller=True,
        )
        self.update = True

    def _voice_status_text(self, *, stage: bool) -> str | None:
        """O texto do status da faixa atual (``None`` sem faixa): os valores externos entram numa passada só e já inertes."""
        track = self.current
        if not track:
            return None
        requester = self.guild.get_member(track.requester)
        requester_name = str(requester.display_name) if requester else "Membro desconhecido"
        if not track.is_stream and (not self.auto_pause or not self.paused):
            if stage:
                timestamp = str(track.duration)
            else:
                timestamp = f"<t:{int((disnake.utils.utcnow() + datetime.timedelta(milliseconds=track.duration - self.position)).timestamp())}:R>"
        else:
            timestamp = (
                ("pausado" if (self.paused) else "🔴") + (f" <t:{int(disnake.utils.utcnow().timestamp())}:R>")
                if not track.is_stream
                else ""
            )
        source = track.info.get("sourceName", "desconhecido")
        values = {
            "{track.title}": voice_status_value(track.single_title),
            "{track.author}": voice_status_value(track.authors_string),
            "{track.duration}": time_format(track.duration) if not track.is_stream else "Livestream",
            "{track.source}": voice_status_value(source),
            "{track.playlist}": voice_status_value(track.playlist_name or "Sem playlist"),
            "{requester.name}": voice_status_value(requester_name),
            "{requester.id}": str(track.requester),
            "{track.timestamp}": timestamp,
            "{track.emoji}": "♪" if stage else music_source_emoji(source, self.bot),
        }
        return render_voice_status(self.stage_title_template, values)

    async def _push_voice_status(self, text: str | None) -> None:
        """Envia já (sem esperar janela nem ficha) o status ao canal de voz atual e guarda o que foi ao ar."""
        channel = self._voice_status_channel()
        if not isinstance(channel, disnake.VoiceChannel):
            return
        self._voice_status_last_put = time.monotonic()
        if not await self._send_voice_status(
            self.bot.edit_voice_channel_status(status=text, channel_id=channel.id), channel_id=channel.id
        ):
            return
        current = self._voice_status_channel()
        if text is not None and (not self.stage_title_event or getattr(current, "id", None) != channel.id):
            # Desligou ou mudou de canal com este envio em voo: o ``disable_voice_status`` não viu texto para limpar (V-145-1).
            await self._send_voice_status(
                self.bot.edit_voice_channel_status(status=None, channel_id=channel.id), channel_id=channel.id
            )
            return
        self.last_stage_title = text or ""

    async def _queue_voice_status(self, text: str | None, *, force: bool) -> None:
        """Decide e volta: envia já se há janela do player e ficha do bot; senão guarda UM pendente (o último vence)."""
        if self.is_closing:
            return
        on_air = self.last_stage_title or None
        pending = self.voice_status_pending
        pending_active = pending is not None and not pending.done()
        if not force and text == on_air:
            if pending_active:
                self._voice_status_pending_text = _UNSET  # voltou ao que já está no ar: nada a enviar
            return
        if text is None and not on_air and not pending_active:
            return  # sem faixa e nada no canal para limpar
        if pending_active:
            self._voice_status_pending_text = text
            return
        last = self._voice_status_last_put
        window_open = last is None or time.monotonic() - last >= VOICE_STATUS_MIN_INTERVAL
        if window_open and _bot_bucket(self.bot).try_acquire():
            await self._push_voice_status(text)
            return
        self._voice_status_pending_text = text
        self.voice_status_pending = asyncio.get_running_loop().create_task(self._flush_voice_status())

    async def _flush_voice_status(self) -> None:
        """A espera do throttle e do bucket mora aqui, nunca no caminho de quem chamou ``update_stage_topic``."""
        bucket = _bot_bucket(self.bot)
        try:
            while True:
                last = self._voice_status_last_put
                if last is not None:
                    wait = last + VOICE_STATUS_MIN_INTERVAL - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                        continue
                await bucket.acquire()
                text = self._voice_status_pending_text
                self._voice_status_pending_text = _UNSET
                if text is _UNSET or not self.stage_title_event:
                    return
                await self._push_voice_status(text)
                if self._voice_status_pending_text is _UNSET:
                    return
        finally:
            if self.voice_status_pending is asyncio.current_task():
                self.voice_status_pending = None

    def _ensure_voice_status_reapply(self) -> None:
        if self.is_closing or not (self.stage_title_event and self.voice_status_permanent):
            return
        task = self.voice_status_reapply
        if task is None or task.done():
            self.voice_status_reapply = asyncio.get_running_loop().create_task(self._voice_status_reapply_loop())

    async def _voice_status_reapply_loop(self) -> None:
        """Reenvia o status a cada ``VOICE_STATUS_REAPPLY_INTERVAL`` s sem envio (edição manual do canal); uma tarefa por player."""
        try:
            due = time.monotonic() + VOICE_STATUS_REAPPLY_INTERVAL
            while True:
                last = self._voice_status_last_put
                if last is not None:
                    due = max(
                        due, last + VOICE_STATUS_REAPPLY_INTERVAL
                    )  # o envio de uma troca de faixa já reescreveu o canal
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue
                if not (self.stage_title_event and self.voice_status_permanent) or self.is_closing:
                    return
                if not isinstance(self._voice_status_channel(), disnake.VoiceChannel):
                    return
                pending = self.voice_status_pending
                if self.last_stage_title and (
                    pending is None or pending.done()
                ):  # com pendente, o texto novo já vai sair
                    await self._queue_voice_status(self.last_stage_title, force=True)
                due = time.monotonic() + VOICE_STATUS_REAPPLY_INTERVAL
        finally:
            if self.voice_status_reapply is asyncio.current_task():
                self.voice_status_reapply = None

    async def update_stage_topic(self, reconnect=True, clear=False, force=False):
        if not self.guild.me.voice:
            if reconnect:
                await self.connect(self.last_channel.id)
            return

        if not self.stage_title_event:
            return

        if clear:
            if isinstance(self.guild.me.voice.channel, disnake.VoiceChannel) and self.last_stage_title:
                await self._send_voice_status(
                    self.bot.edit_voice_channel_status(status=None, channel_id=self.guild.me.voice.channel.id)
                )
            return

        if not isinstance(self.guild.me.voice.channel, disnake.StageChannel):  # canal de voz: passa pelo throttle
            msg = self._voice_status_text(stage=False)
            await self._queue_voice_status(fit_voice_status(msg) if msg is not None else None, force=force)
            self._ensure_voice_status_reapply()
            return

        msg = self._voice_status_text(stage=True)

        if not self.guild.me.guild_permissions.manage_guild:
            return

        if msg is not None and len(msg) > 110:
            msg = msg[:107] + "..."

        if not msg:
            msg = self.tr("player.status_waiting_for_new_songs")
        else:
            emojis = emoji_pattern.findall(msg)
            for emoji in emojis:
                msg = msg.replace(emoji, "")

        if not self.guild.me.voice.channel.instance:
            func = self.guild.me.voice.channel.create_instance
        elif msg == self.last_stage_title:
            return
        else:
            func = self.guild.me.voice.channel.instance.edit

        if not await self._send_voice_status(func(topic=msg)):
            return

        self.last_stage_title = msg

    async def _wait_for_new_node(self, txt: str = None, ignore_node: wavelink.Node = None):
        if self.node.is_available:
            return

        self.locked = True

        try:
            self.auto_skip_track_task.cancel()
        except Exception:
            pass

        try:
            original_identifier = str(self.node.identifier)

            self.set_command_log(
                txt or self.tr("player.there_are_no_music_servers"),
                emoji="⏰",
                controller=True,
            )
            self.update = True

            while True:
                node = self.bot.music.get_best_node(ignore_node=ignore_node)

                if not node:
                    await asyncio.sleep(5)
                    continue

                self.native_yt = True

                try:
                    self.node.players[self.guild_id]
                except KeyError:
                    return

                try:
                    if node == self.node and self.guild.me.voice and self._voice_state:
                        await self._dispatch_voice_update()
                    else:
                        await self.change_node(node.identifier)
                except Exception:
                    await asyncio.sleep(5)
                    continue

                if not self.guild.me.voice:
                    try:
                        can_connect(self.last_channel, self.guild, bot=self.bot)
                    except (GenericError, PoolException) as e:
                        self.set_command_log(f"O player foi finalizado devido ao erro: {e}", controller=True)
                        await self.destroy()
                        return
                    except Exception as e:
                        error_code = log_unexpected_error(logger, e, "conexão do player ao novo servidor de música")
                        self.set_command_log(
                            f"O player foi finalizado devido a um erro inesperado{code_suffix(error_code)}.",
                            controller=True,
                        )
                        await self.destroy()
                        return
                    await self.connect(self.last_channel.id)

                self.locked = False

                if self.current:
                    self.queue.appendleft(self.current)
                    start_position = self.position
                    self.current = None
                    self.current_encoded = None
                else:
                    start_position = 0

                await self.process_next(start_position=start_position)

                try:
                    if not self.auto_pause:
                        if original_identifier != node.identifier:
                            txt = self.tr("player.the_player_was_moved_to", identifier=str(node.identifier))
                        else:
                            txt = self.tr("player.the_player_was_reconnected_to", identifier=str(self.node.identifier))
                        self.set_command_log(emoji="📶", text=txt, controller=True)
                        self.update = True
                except Exception:
                    logger.exception("Falha em _wait_for_new_node")
                self._new_node_task = None
                return
        except Exception:
            logger.exception("Falha em _wait_for_new_node")
