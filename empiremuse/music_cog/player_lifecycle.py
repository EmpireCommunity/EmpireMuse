# -*- coding: utf-8 -*-
import logging
import asyncio
from typing import Union

import aiohttp
import disnake
from disnake.ext import commands

import empiremuse.wavelink as wavelink
from empiremuse.logs.context import guild_extra
from empiremuse.rules.voice_status import resolve_voice_status
from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.request_panel import fetch_panel_message
from empiremuse.utils.music.skin_store import load_custom_skins
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    CustomContext,
)

logger = logging.getLogger("empiremuse.player")


class PlayerLifecycleMixin:
    async def process_music(
        self,
        inter: Union[
            disnake.Message,
            disnake.MessageInteraction,
            disnake.ApplicationCommandInteraction,
            CustomContext,
            disnake.ModalInteraction,
        ],
        player: LavalinkPlayer,
        force_play: str = "no",
        ephemeral=True,
        log_text="",
        emoji="",
        warn_message: str = "",
        user_data: dict = None,
        reg_query: dict = None,
    ):
        if not player.current:
            if warn_message:
                player.set_command_log(emoji="⚠️", text=warn_message)
            await player.process_next()
        elif force_play == "yes":
            player.set_command_log(
                emoji="▶️", text=f"{inter.author.mention} adicionou a música atual para tocar imediatamente."
            )
            await player.track_end()
            await player.process_next()
        # elif player.current.autoplay:
        #    player.set_command_log(text=log_text, emoji=emoji)
        #    await player.track_end()
        #    await player.process_next()
        else:
            if ephemeral:
                player.set_command_log(text=log_text, emoji=emoji)
            player.update = True

        if reg_query is not None:
            if not user_data:
                user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

            try:
                user_data["last_tracks"].remove(reg_query)
            except Exception:
                pass

            if len(user_data["last_tracks"]) > 6:
                user_data["last_tracks"].pop(0)

            user_data["last_tracks"].append(reg_query)

            await self.bot.update_global_data(inter.author.id, user_data, db_name=DBModel.users)

    async def create_player(
        self,
        inter: Union[
            disnake.Message,
            disnake.MessageInteraction,
            disnake.ApplicationCommandInteraction,
            CustomContext,
            disnake.ModalInteraction,
        ],
        bot: BotCore,
        guild: disnake.Guild,
        channel: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.Thread],
        guild_data: dict = None,
        message_inter=None,
        node: wavelink.Node = None,
        modal_message_id: int = None,
    ):
        try:
            return bot.music.players[guild.id]
        except KeyError:
            pass

        if not guild_data:
            guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        skin = guild_data["player_controller"]["skin"]
        static_skin = guild_data["player_controller"]["static_skin"]
        static_player = guild_data["player_controller"]

        if not node:
            node = await self.get_best_node(bot)

        global_data = await bot.get_global_data(guild.id, db_name=DBModel.guilds)
        voice_status = resolve_voice_status(global_data)

        try:
            vc = inter.author.voice.channel
        except AttributeError:
            vc = None

        if global_data["global_skin"]:
            skin = global_data["player_skin"] or skin
            static_skin = global_data["player_skin_static"] or guild_data["player_controller"]["static_skin"]

        try:
            invite = global_data["listen_along_invites"][str(vc.id)]
        except AttributeError, KeyError:
            invite = ""

        if invite:
            try:
                invite = (await bot.fetch_invite(invite)).url
            except disnake.NotFound:
                invite = None
            except Exception:
                logger.exception("Falha em create_player", extra=guild_extra(vc.guild.id if vc else None))
            else:
                try:
                    if invite.channel.id != vc.id:
                        invite = None
                except AttributeError:
                    pass

        if invite is None:
            try:
                del global_data["listen_along_invites"][str(vc.id)]
            except KeyError:
                pass
            else:
                logger.debug(
                    "Removendo invite: %s%s",
                    invite,
                    f" (servidor {vc.guild.id}, canal {vc.id})" if vc else "",
                    extra=guild_extra(vc.guild.id if vc else None),
                )
                await self.bot.update_global_data(inter.guild_id, global_data, db_name=DBModel.guilds)

        try:
            guild_id = inter.guild.id
        except AttributeError:
            guild_id = inter.guild_id

        skins = await load_custom_skins(bot, guild_id, global_data)

        static_channel = None

        if static_player["channel"]:
            try:
                static_channel = bot.get_channel(int(static_player["channel"])) or await bot.fetch_channel(
                    int(static_player["channel"])
                )
            except disnake.Forbidden:
                pass
            except disnake.NotFound:
                await self.reset_controller_db(inter.guild_id, guild_data, inter)

            allowed_channel = None

            for ch in (static_channel, channel):
                if not ch:
                    continue

                if isinstance(ch, disnake.Thread):
                    if not ch.parent:
                        await self.reset_controller_db(inter.guild_id, guild_data, inter)
                        continue

                    channel_check = ch.parent

                else:
                    channel_check = ch

                bot_perms = channel_check.permissions_for(guild.me)

                if bot_perms.read_message_history:
                    allowed_channel = ch
                    break

                elif bot_perms.manage_permissions:
                    overwrites = {
                        guild.me: disnake.PermissionOverwrite(
                            embed_links=True,
                            send_messages=True,
                            send_messages_in_threads=True,
                            read_messages=True,
                            create_public_threads=True,
                            read_message_history=True,
                            manage_messages=True,
                            manage_channels=True,
                            attach_files=True,
                        )
                    }

                    await channel_check.edit(overwrites=overwrites)
                    allowed_channel = ch
                    break

            channel = allowed_channel

        player: LavalinkPlayer = bot.music.get_player(
            guild_id=guild_id,
            cls=LavalinkPlayer,
            player_creator=inter.author.id,
            guild=guild,
            channel=channel,
            last_message_id=guild_data["player_controller"]["message_id"],
            node_id=node.identifier,
            static=bool(static_channel),
            skin=bot.pool.check_skin(skin),
            skin_static=bot.pool.check_static_skin(static_skin),
            custom_skin_data=skins.normal,
            custom_skin_static_data=skins.static,
            extra_hints=self.extra_hints,
            restrict_mode=guild_data["enable_restrict_mode"],
            listen_along_invite=invite,
            autoplay=guild_data["autoplay"],
            prefix=global_data["prefix"] or bot.default_prefix,
            stage_title_template=voice_status.template,
        )
        player.tr = await bot.pool.i18n.for_guild(guild)
        player.setup_hints()
        player.process_hint()

        # O status do canal vale por padrão em todo servidor (2.1.2): com o bot já no canal, aplica o permanente uma vez, antes
        # do 1º ``update_stage_topic`` do ``_he_track_start``; fora do canal quem aplica é o ``player_vc_disconnect`` ao entrar.
        player.voice_status_saved_default = global_data.get("voice_status_permanent") is None
        if guild.me and guild.me.voice:
            await player.apply_permanent_voice_status(voice_status)

        if (vol := int(guild_data["default_player_volume"])) != 100:
            await player.set_volume(vol)

        if not player.message and player.text_channel:
            try:
                if (
                    player.static
                ):  # a mensagem do painel é do bot do painel: o player a recebe ligada a ele (só ele a edita)
                    player.message = await fetch_panel_message(bot.pool, guild_id)
                else:
                    player.message = await player.text_channel.fetch_message(int(static_player["message_id"]))
            except TypeError:
                player.message = None
            except Exception:
                logger.exception("Falha em create_player", extra=guild_extra(getattr(player, "guild_id", None)))
                if (
                    hasattr(player.text_channel, "parent")
                    and isinstance(player.text_channel.parent, disnake.ForumChannel)
                    and str(player.text_channel.id) == static_player["message_id"]
                ):
                    pass
                elif player.static:
                    player.text_channel = None

        if not player.static and player.text_channel:
            if message_inter and inter.bot != bot:
                player.message = message_inter
            elif modal_message_id:
                try:
                    player.message = await player.text_channel.fetch_message(modal_message_id)
                except Exception:
                    pass

            if not player.has_thread:
                player.message = None
            else:
                await self.thread_song_request(message_inter.thread, reopen=True, bot=bot)

        return player

    async def _voice_status_follow_move(
        self, player: LavalinkPlayer, before: disnake.VoiceState, after: disnake.VoiceState
    ) -> None:
        """O próprio bot saiu, entrou ou mudou de canal (só com mudança de canal): limpa o canal antigo e a sessão volta ao permanente (2.1.2).

        Relê o documento global do servidor (ele pode ter mudado desde a criação, inclusive pelo painel ou por outro bot do pool).
        Entrar em canal com o status já ligado pela criação, ou com a aplicação da retomada agendada (ela espera o jitter), não reaplica.
        """
        if player.is_closing or before.channel == after.channel:
            return  # mute, deafen e suppress do próprio bot chegam como voice_state no MESMO canal: não mexem na sessão
        try:
            if before.channel:
                await player.disable_voice_status(channel=before.channel)
            entering = before.channel is None
            pending_apply = player.voice_status_apply is not None and not player.voice_status_apply.done()
            if after.channel and not (entering and (player.stage_title_event or pending_apply)):
                global_data = await player.bot.get_global_data(player.guild.id, db_name=DBModel.guilds)
                await player.apply_permanent_voice_status(resolve_voice_status(global_data))
        except Exception:
            logger.exception("Falha em player_vc_disconnect", extra=guild_extra(getattr(player, "guild_id", None)))

    @commands.Cog.listener("on_voice_state_update")
    async def player_vc_disconnect(self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState):
        try:
            player: LavalinkPlayer = self.bot.music.players[member.guild.id]
        except KeyError:
            return

        if before.channel and not after.channel:
            if player.last_channel != before.channel:
                return

        elif after.channel and not before.channel:
            if player.last_channel != after.channel:
                return

        if member.bot:
            # ignorar outros bots
            if player.bot.user.id == member.id:
                await self._voice_status_follow_move(player, before, after)

            if player.bot.user.id == member.id and not after.channel:
                await asyncio.sleep(3)

                if player.is_closing:
                    return

                try:
                    player.reconnect_voice_channel_task.cancel()
                except Exception:
                    pass
                player.reconnect_voice_channel_task = player.bot.loop.create_task(player.reconnect_voice_channel())

            return

        if before.channel == after.channel:
            try:
                vc = player.last_channel
                if vc != after.channel:
                    return
            except AttributeError:
                pass
            else:
                if after.channel == vc:
                    try:
                        player.members_timeout_task.cancel()
                    except Exception:
                        pass
                    try:
                        check = (m for m in vc.members if not m.bot and not (m.voice.deaf or m.voice.self_deaf))
                    except Exception:
                        check = None
                    player.start_members_timeout(check=bool(check))
            return

        try:
            player.members_timeout_task.cancel()
            player.members_timeout_task = None
        except AttributeError:
            pass

        if member.id == player.bot.user.id:
            """for b in self.bot.pool.get_guild_bots(member.guild.id):
                if b == player.bot:
                    if after.channel:
                        player._last_channel = after.channel
                    continue
                try:
                    try:
                        after.channel.voice_states[b.user.id]
                    except KeyError:
                        continue
                    if before.channel.permissions_for(member.guild.me).connect:
                        await asyncio.sleep(1)
                        await player.guild.voice_client.move_to(before.channel)
                    else:
                        player.set_command_log(text="O player foi finalizado porque me moveram ao canal "
                                                    f"{after.channel.mention} no qual o bot {b.user.mention} "
                                                    "também estava conectado gerando incompatibilidade com "
                                                    "meu sistema de multi-voice.", emoji="⚠️")
                        await player.destroy()
                    return
                except AttributeError:
                    pass
                except Exception:
                    traceback.print_exc()"""

            try:
                vc = member.guild.me.voice.channel
            except AttributeError:
                pass
            else:
                # tempfix para channel do voice_client não ser setado ao mover bot do canal.
                player.guild.voice_client.channel = vc
                player._last_channel = vc
                player.update = True

        try:
            check = [
                m
                for m in player.guild.me.voice.channel.members
                if not m.bot and not (m.voice.deaf or m.voice.self_deaf)
            ]
        except Exception:
            check = None

        if player.stage_title_event and member.bot and not player.is_closing:
            try:
                if isinstance(before.channel, disnake.StageChannel):
                    if before.channel.instance and member not in before.channel.members:
                        try:
                            await before.channel.instance.edit(topic="atualização automática desativada")
                        except Exception:
                            logger.exception("Falha em player_vc_disconnect")
                        player.stage_title_event = False

                else:
                    if isinstance(before.channel, disnake.VoiceChannel) and member not in before.channel.members:
                        await player.disable_voice_status(channel=before.channel)
            except Exception:
                logger.exception("Falha em player_vc_disconnect")

        if (
            member.bot
            and isinstance(after.channel, disnake.StageChannel)
            and after.channel.permissions_for(member).mute_members
        ):
            await asyncio.sleep(1.5)
            if member not in after.channel.speakers:
                try:
                    await member.guild.me.edit(suppress=False)
                except Exception:
                    logger.exception("Falha em player_vc_disconnect")

        if check:
            try:
                player.auto_skip_track_task.cancel()
            except AttributeError:
                pass
            player.auto_skip_track_task = None

        player.start_members_timeout(check=bool(check))

        if not member.guild.me.voice:
            await asyncio.sleep(1)
            if not player.is_closing and not player._new_node_task:
                try:
                    await player.destroy(force=True)
                except Exception:
                    logger.exception("Falha em player_vc_disconnect")

        # rich presence stuff

        if player.auto_pause:
            return

        if player.is_closing or (member.bot and not before.channel):
            return

        channels = set()

        try:
            channels.add(before.channel.id)
        except Exception:
            pass

        try:
            channels.add(after.channel.id)
        except Exception:
            pass

        try:
            try:
                vc = player.guild.me.voice.channel
            except AttributeError:
                vc = player.last_channel

            if vc.id not in channels:
                return
        except AttributeError:
            pass

        if not after or before.channel != after.channel:
            try:
                vc = player.guild.me.voice.channel
            except AttributeError:
                vc = before.channel

            if vc:
                try:
                    await player.process_rpc(
                        vc,
                        users=[member.id],
                        close=not player.guild.me.voice or after.channel != player.guild.me.voice.channel,
                        wait=True,
                    )
                except AttributeError:
                    logger.exception("Falha em player_vc_disconnect")
                    pass

                await player.process_rpc(vc, users=[m for m in vc.voice_states if (m != member.id)])

    async def check_available_bot(
        self, inter, guild: disnake.Guild, bot: BotCore = None, message: disnake.Message = None
    ):
        free_bots = []
        voice_channels = []
        bot_count = 0

        if bot:
            try:
                player = bot.music.players[guild.id]
            except KeyError:
                pass
            else:
                if player.guild.me.voice and inter.author.id in player.guild.me.voice.channel.voice_states:
                    return [bot]

        for b in self.bot.pool.get_guild_bots(guild.id):
            if not b.bot_ready:
                continue

            g = b.get_guild(guild.id)

            if not g:
                bot_count += 1
                continue

            author = g.get_member(inter.author.id)

            if not author:
                continue

            inter.author = author

            if b.user in inter.author.voice.channel.members:
                free_bots.append(b)
                break

            p: LavalinkPlayer = b.music.players.get(guild.id)

            if p:
                try:
                    vc = g.me.voice.channel
                except AttributeError:
                    vc = p.last_channel

                if not vc:
                    continue

                if inter.author.id in vc.members:
                    free_bots.append(b)
                    break
                else:
                    voice_channels.append(vc.mention)
                    continue

            free_bots.append(b)

        if not free_bots:
            if bot_count:
                txt = "**Todos os bots estão em uso no nomento...**"
                if voice_channels:
                    txt += "\n\n**Você pode conectar em um dos canais abaixo onde há sessões ativas:**\n" + ", ".join(
                        voice_channels
                    )
                    if inter.author.guild_permissions.manage_guild:
                        txt += "\n\n**Ou se preferir: Adicione mais bots de música no servidor atual clicando no botão abaixo:**"
                    else:
                        txt += (
                            "\n\n**Ou se preferir: Solicite a um administrador/manager do servidor para clicar no botão abaixo "
                            "para adicionar mais bots de música no servidor atual.**"
                        )
            else:
                txt = (
                    "**Não há bots de música compatíveis no servidor...**"
                    "\n\nSerá necessário adicionar pelo menos um bot compatível clicando no botão abaixo:"
                )

            kwargs = {}

            try:
                func = inter.edit_original_message
            except Exception:
                try:
                    func = inter.store_message.edit
                except Exception:
                    try:
                        func = message.edit
                    except Exception:
                        func = inter.send
                        kwargs["ephemeral"] = True

            await func(txt, components=[disnake.ui.Button(custom_id="bot_invite", label="Adicionar bots")], **kwargs)
            return []

        return free_bots

    async def reset_controller_db(self, guild_id: int, data: dict, inter: disnake.ApplicationCommandInteraction = None):
        data["player_controller"]["channel"] = None
        data["player_controller"]["message_id"] = None

        if inter:
            try:
                bot = inter.music_bot
            except AttributeError:
                bot = inter.bot
        else:
            bot = self.bot

        try:
            await bot.update_data(guild_id, data, db_name=DBModel.guilds)
        except Exception:
            logger.exception("Falha em reset_controller_db")

        try:
            player: LavalinkPlayer = bot.music.players[guild_id]
        except KeyError:
            return

        player.static = False

        if inter:
            try:
                if isinstance(inter.channel.parent, disnake.TextChannel):
                    player.text_channel = inter.channel.parent
                else:
                    player.text_channel = inter.channel
            except AttributeError:
                player.text_channel = inter.channel

    async def error_report_loop(self):
        while True:
            data = await self.error_report_queue.get()

            async with aiohttp.ClientSession() as session:
                webhook = disnake.Webhook.from_url(self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"], session=session)
                await webhook.send(
                    username=self.bot.user.display_name, avatar_url=self.bot.user.display_avatar.url, **data
                )

            await asyncio.sleep(15)
