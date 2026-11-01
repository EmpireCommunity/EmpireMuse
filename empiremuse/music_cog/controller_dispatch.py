# -*- coding: utf-8 -*-
import logging
import asyncio
from typing import Union, Optional

import disnake
from disnake.ext import commands

from empiremuse.utils.client import BotCore
from empiremuse.rules.usage import KIND_BUTTON
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import pick_request_bot, update_attr, voice_status_lock_remaining
from empiremuse.utils.music.converters import (
    time_format,
)
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    check_cmd,
    send_idle_embed,
    CustomContext,
)
from empiremuse.utils.usage import component_name, usage_of

from empiremuse.logs.context import guild_extra

logger = logging.getLogger(__name__)


class ControllerDispatchMixin:
    @commands.Cog.listener("on_message_delete")
    async def player_message_delete(self, message: disnake.Message):
        if not message.guild:
            return

        try:
            player: LavalinkPlayer = self.bot.music.players[message.guild.id]

            if message.id != player.message.id:
                return

        except AttributeError, KeyError:
            return

        thread = self.bot.get_channel(message.id)

        if not thread:
            return

        player.message = None
        await thread.edit(archived=True, locked=True, name=f"arquivado: {thread.name}")

    @commands.Cog.listener("on_ready")
    async def resume_players_ready(self):
        if not self.bot.bot_ready:
            return

        for guild_id in list(self.bot.music.players):
            try:
                player: LavalinkPlayer = self.bot.music.players[guild_id]

                try:
                    channel_id = player.guild.me.voice.channel.id
                except AttributeError:
                    channel_id = player.channel_id

                vc = self.bot.get_channel(channel_id) or player.last_channel

                try:
                    player.guild.voice_client.cleanup()
                except Exception:
                    pass

                if not vc:
                    logger.debug(
                        "Player finalizado por falta de canal de voz",
                        extra=guild_extra(getattr(player.guild, "id", None)),
                    )
                    try:
                        await player.destroy()
                    except Exception:
                        logger.exception("Falha em resume_players_ready")
                    continue

                await player.connect(vc.id)

                if not player.is_paused and not player.is_playing:
                    await player.process_next()
                logger.debug("Player reconectado no canal de voz", extra=guild_extra(getattr(player.guild, "id", None)))
            except Exception:
                logger.exception("Falha em resume_players_ready")

    async def request_executor(self, inter):
        """O cog ``Music`` do bot que atende um pedido feito no canal de pedidos ÚNICO (o do painel recebe, quem toca pode ser outro).

        Define ``inter.music_bot``/``music_guild`` para o resto do fluxo agir no bot certo: o que já está na call de quem pediu, senão o
        1º livre. Fora do canal único devolve este mesmo cog, sem mexer na interação.
        """
        pool = self.bot.pool
        try:
            config = await pool.servers.request.get(inter.guild_id)
        except GenericError:
            return self
        channel = getattr(inter, "channel", None)
        if not config.channel or config.channel not in (inter.channel_id, getattr(channel, "parent_id", None)):
            return self
        bot = pick_request_bot(pool, inter.guild_id, inter.author)
        if bot is None:
            raise GenericError(
                "**Todos os bots de música estão em uso no momento.** Entre na call de um deles ou aguarde um ficar livre."
            )
        update_attr(inter, bot, bot.get_guild(inter.guild_id))
        return self if bot is self.bot else bot.get_cog("Music")

    async def is_request_channel(
        self,
        ctx: Union[disnake.ApplicationCommandInteraction, disnake.MessageInteraction, CustomContext],
        *,
        data: dict = None,
        ignore_thread=False,
    ) -> bool:
        if isinstance(ctx, (CustomContext, disnake.MessageInteraction)):
            return True

        try:
            bot = ctx.music_bot
            channel_ctx = bot.get_channel(ctx.channel_id)
        except AttributeError:
            bot = ctx.bot
            channel_ctx = ctx.channel

        if not self.bot.check_bot_forum_post(channel_ctx):
            return True

        try:
            player: LavalinkPlayer = bot.music.players[ctx.guild_id]

            if not player.static:
                return False

            if isinstance(channel_ctx, disnake.Thread) and player.text_channel == channel_ctx.parent:
                return not ignore_thread

            return player.text_channel == channel_ctx

        except KeyError:
            if not data:
                data = await bot.get_data(ctx.guild_id, db_name=DBModel.guilds)

            try:
                channel = bot.get_channel(int(data["player_controller"]["channel"]))
            except Exception:
                channel = None

            if not channel:
                return False

            if isinstance(channel_ctx, disnake.Thread) and channel == channel_ctx.parent:
                return not ignore_thread

            return channel.id == channel_ctx.id

    async def check_channel(
        self,
        guild_data: dict,
        inter: Union[disnake.ApplicationCommandInteraction, CustomContext],
        channel: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.Thread],
        guild: disnake.Guild,
        bot: BotCore,
    ):
        static_player = guild_data["player_controller"]

        warn_message = None
        message: Optional[disnake.Message] = None

        try:
            channel_db = bot.get_channel(int(static_player["channel"])) or await bot.fetch_channel(
                int(static_player["channel"])
            )
        except TypeError, disnake.NotFound:
            channel_db = None
        except disnake.Forbidden:
            channel_db = bot.get_channel(inter.channel_id)
            warn_message = f"Não tenho permissão de acessar o canal <#{static_player['channel']}>, o player será usado no modo tradicional."
            static_player["channel"] = None

        if (
            channel_db
            and channel_db.guild.id == inter.guild_id
            and not await bot.pool.servers.request.is_panel_bot(inter.guild_id, bot)
        ):
            # canal único: quem toca (e não é o bot do painel) só o usa; criar post ou zerar a configuração é do bot do painel
            return channel_db, warn_message, None

        if not channel_db or channel_db.guild.id != inter.guild_id:
            await self.reset_controller_db(inter.guild_id, guild_data, inter)

        else:
            if channel_db.id != channel.id:
                try:
                    if isinstance(channel_db, disnake.Thread):
                        if not channel_db.parent:
                            await self.reset_controller_db(inter.guild_id, guild_data, inter)
                            channel_db = None

                        else:
                            if channel_db.owner_id != bot.user.id:
                                if (
                                    not isinstance(channel_db.parent, disnake.ForumChannel)
                                    or not channel_db.parent.permissions_for(channel_db.guild.me).create_forum_threads
                                ):
                                    await self.reset_controller_db(inter.guild_id, guild_data, inter)
                                    channel_db = None
                                else:
                                    thread = None

                                    for t in channel_db.parent.threads:
                                        if t.owner_id == bot.user.id:
                                            try:
                                                message = await t.fetch_message(t.id)
                                            except disnake.NotFound:
                                                continue
                                            if not message or message.author.id != bot.user.id:
                                                continue
                                            thread = t
                                            break

                                    if not thread and guild.me.guild_permissions.read_message_history:
                                        async for t in channel_db.parent.archived_threads(limit=100):
                                            if t.owner_id == bot.user.id:
                                                try:
                                                    message = await t.fetch_message(t.id)
                                                except disnake.NotFound:
                                                    continue
                                                if not message or message.author.id != bot.user.id:
                                                    continue
                                                thread = t
                                                break

                                    if not thread:
                                        thread_wmessage = await channel_db.parent.create_thread(
                                            name=f"{bot.user} song-request",
                                            content="Post para pedido de músicas.",
                                            auto_archive_duration=10080,
                                            slowmode_delay=5,
                                        )
                                        channel_db = thread_wmessage.thread
                                        message = thread_wmessage.message
                                    else:
                                        channel_db = thread

                            thread_kw = {}

                            if channel_db.locked and channel_db.permissions_for(guild.me).manage_threads:
                                thread_kw.update({"locked": False, "archived": False})

                            elif channel_db.archived and channel_db.owner_id == bot.user.id:
                                thread_kw["archived"] = False

                            if thread_kw:
                                await channel_db.edit(**thread_kw)

                            elif isinstance(channel.parent, disnake.ForumChannel):
                                warn_message = (
                                    f"**{bot.user.mention} não possui permissão de gerenciar tópicos "
                                    f"para desarquivar/destrancar o tópico: {channel_db.mention}**"
                                )

                except AttributeError:
                    pass

                if channel_db:
                    channel_db_perms = channel_db.permissions_for(guild.me)

                    channel = bot.get_channel(inter.channel.id)

                    if isinstance(channel, disnake.Thread):
                        send_message_perm = (
                            getattr(channel_db, "parent", channel_db)
                            .permissions_for(channel.guild.me)
                            .send_messages_in_threads
                        )
                    else:
                        send_message_perm = channel_db.permissions_for(channel.guild.me).send_messages

                    if not send_message_perm:
                        raise GenericError(
                            f"**{bot.user.mention} não possui permissão para enviar mensagens no canal <#{static_player['channel']}>**\n"
                            "Caso queira resetar a configuração do canal de pedir música, use o comando /reset ou /setup "
                            "novamente..."
                        )

                    if not channel_db_perms.embed_links:
                        raise GenericError(
                            f"**{bot.user.mention} não possui permissão para anexar links/embeds no canal <#{static_player['channel']}>**\n"
                            "Caso queira resetar a configuração do canal de pedir música, use o comando /reset ou /setup "
                            "novamente..."
                        )

        return channel_db, warn_message, message

    async def process_player_interaction(
        self,
        interaction: Union[disnake.MessageInteraction, disnake.ModalInteraction],
        command: Optional[disnake.ApplicationCommandInteraction],
        kwargs: dict,
    ):
        if not command:
            raise GenericError(
                (await self.bot.pool.i18n.for_guild(interaction.guild))("player.command_not_found_implemented")
            )

        try:
            interaction.application_command = command
            await command._max_concurrency.acquire(interaction)
        except AttributeError:
            pass

        await check_cmd(command, interaction)

        await command(interaction, **kwargs)

        if usage := usage_of(self.bot):
            usage.record_command(KIND_BUTTON, component_name(interaction), interaction, bot=self.bot, ok=True)

        try:
            await command._max_concurrency.release(interaction)
        except Exception:
            pass

        try:
            player: LavalinkPlayer = self.bot.music.players[interaction.guild_id]
            player.interaction_cooldown = True
            await asyncio.sleep(1)
            player.interaction_cooldown = False
        except KeyError, AttributeError:
            pass

    @commands.Cog.listener("on_dropdown")
    async def guild_pin(self, interaction: disnake.MessageInteraction):
        if not self.bot.bot_ready:
            await interaction.send("Ainda estou inicializando...\nPor favor aguarde mais um pouco...", ephemeral=True)
            return

        if interaction.data.custom_id != "player_guild_pin":
            return

        if not interaction.data.values:
            await interaction.response.defer()
            return

        if not interaction.user.voice:
            await interaction.send("Você deve entrar em um canal de voz para usar isto.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_data = await self.bot.get_data(interaction.guild_id, db_name=DBModel.guilds)

        try:
            query = interaction.data.values[0]
        except KeyError:
            await interaction.send("**O item selecionado não foi encontrado na base de dados...**", ephemeral=True)
            await send_idle_embed(interaction.message, bot=self.bot, guild_data=guild_data, force=True)
            return

        kwargs = {
            "query": f"> pin: {query}",
            "position": 0,
            "options": False,
            "manual_selection": True,
            "server": None,
            "force_play": "no",
        }

        try:
            cog = await self.request_executor(interaction)
            await cog.play.callback(self=cog, inter=interaction, **kwargs)
        except Exception as e:
            self.bot.dispatch("interaction_player_error", interaction, e)

    @commands.Cog.listener("on_dropdown")
    async def player_dropdown_event(self, interaction: disnake.MessageInteraction):
        if interaction.data.custom_id == "musicplayer_queue_dropdown":
            await self.process_player_interaction(
                interaction=interaction,
                command=self.bot.get_slash_command("skipto"),
                kwargs={"query": interaction.values[0][3:], "case_sensitive": True},
            )
            return

        if not interaction.data.custom_id.startswith("musicplayer_dropdown_"):
            return

        if not interaction.values:
            await interaction.response.defer()
            return

        await self.player_controller(interaction, interaction.values[0])

    @commands.Cog.listener("on_button_click")
    async def player_button_event(self, interaction: disnake.MessageInteraction):
        if not interaction.data.custom_id.startswith("musicplayer_"):
            return

        await self.player_controller(interaction, interaction.data.custom_id)

    async def check_stage_title(self, inter, bot: BotCore, player: LavalinkPlayer):
        if (remaining := voice_status_lock_remaining(player)) > 0 and not (await bot.is_owner(inter.author)):
            raise GenericError(
                player.tr(
                    "player.you_will_have_to_wait", time_format=str(time_format(remaining * 1000, use_names=True))
                )
            )
