# -*- coding: utf-8 -*-

from typing import Union

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    is_dj,
    check_queue_loading,
)
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.saved_queue import store_saved_queue
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    CustomContext,
    pool_command,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    queue_manipulation_cd,
    remove_mc,
)
from empiremuse.utils.music.converters import md_link


class PlayerSessionMixin:
    jukebox_thread_cd = commands.CooldownMapping.from_cooldown(1, 120, commands.BucketType.guild)

    async def create_jukebox_thread(self, inter: Union[disnake.ApplicationCommandInteraction, CustomContext]):
        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        if not self.bot.intents.message_content:
            raise GenericError(
                "**Atualmente não tenho a intent de message-content para conferir o conteúdo de mensagens**"
            )

        player: LavalinkPlayer = bot.music.players[guild.id]

        if player.static:
            raise GenericError("**Você não pode usar esse comando com um canal da Jukebox configurado.**")

        if player.has_thread:
            raise GenericError("**Já há uma thread/conversa ativa no player.**")

        if not isinstance(player.text_channel, disnake.TextChannel):
            raise GenericError(
                "**O player-controller está ativo em um canal incompatível com criação de thread/conversa.**"
            )

        if not player.controller_mode:
            raise GenericError(
                "**A skin/aparência atual não é compatível com a conversa de pedidos "
                "via thread/conversa\n\n"
                "Nota:** `Esse sistema requer uma skin que use botões.`"
            )

        if not player.text_channel.permissions_for(guild.me).send_messages:
            raise GenericError(
                f"**{bot.user.mention} não possui permissão enviar mensagens no canal {player.text_channel.mention}.**"
            )

        if not player.text_channel.permissions_for(guild.me).create_public_threads:
            raise GenericError(f"**{bot.user.mention} não possui permissão de criar tópicos públicos.**")

        if not [
            m
            for m in player.guild.me.voice.channel.members
            if not m.bot and player.text_channel.permissions_for(m).send_messages_in_threads
        ]:
            raise GenericError(
                f"**Não há membros no canal <#{player.channel_id}> com permissão de enviar mensagens "
                f"em tópicos no canal {player.text_channel.mention}"
            )

        await inter.response.defer(ephemeral=True)

        thread = await player.message.create_thread(name=f"{bot.user.name} temp. jukebox", auto_archive_duration=10080)

        txt = [
            "Ativou o sistema de thread/conversa temporária para pedido de música.",
            f"💬 **⠂{inter.author.mention} criou uma {md_link('thread/conversa', thread.jump_url, code=False)} temporária para pedido de música.**",
        ]

        await self.interaction_message(inter, txt, emoji="💬", defered=True, force=True)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.user_command(
        name=disnake.Localized("Add DJ", data={disnake.Locale.pt_BR: "Adicionar DJ"}), extras={"only_voiced": True}
    )
    async def adddj_u(self, inter: disnake.UserCommandInteraction):
        await self.add_dj(interaction=inter, user=inter.target)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="adddj",
        aliases=["adj"],
        only_voiced=True,
        description="Adicionar um membro à lista de DJ's na sessão atual do player.",
        usage="{prefix}{cmd} [id|nome|@user]\nEx: {prefix}{cmd} @membro",
    )
    async def add_dj_legacy(self, ctx: CustomContext, user: disnake.Member):
        await self.add_dj.callback(self=self, inter=ctx, user=user)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.add_dj.description"),
        extras={"only_voiced": True},
    )
    @commands.contexts(guild=True)
    async def add_dj(
        self,
        inter: disnake.ApplicationCommandInteraction,
        *,
        user: disnake.User = commands.Param(
            name=localized("cmd.add_dj.opt.member.name"), description=localized("cmd.add_dj.opt.member.description")
        ),
    ):
        error_text = None

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        user = guild.get_member(user.id)

        if user.bot:
            error_text = "**Você não pode adicionar um bot na lista de DJ's.**"
        elif user == inter.author:
            error_text = "**Você não pode adicionar a si mesmo na lista de DJ's.**"
        elif user.guild_permissions.manage_channels:
            error_text = f"você não pode adicionar o membro {user.mention} na lista de DJ's (ele(a) possui permissão de **gerenciar canais**)."
        elif user.id == player.player_creator:
            error_text = f"**O membro {user.mention} é o criador do player...**"
        elif user.id in player.dj:
            error_text = f"**O membro {user.mention} já está na lista de DJ's**"

        if error_text:
            raise GenericError(error_text)

        player.dj.add(user.id)

        text = [
            f"adicionou {user.mention} à lista de DJ's.",
            f"🎧 **⠂{inter.author.mention} adicionou {user.mention} na lista de DJ's.**",
        ]

        if (player.static and channel == player.text_channel) or isinstance(
            inter.application_command, commands.InvokableApplicationCommand
        ):
            await inter.send(f"{user.mention} adicionado à lista de DJ's!{player.controller_link}")

        await self.interaction_message(inter, txt=text, emoji="🎧")

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.remove_dj.description"),
        extras={"only_voiced": True},
    )
    @commands.contexts(guild=True)
    async def remove_dj(
        self,
        inter: disnake.ApplicationCommandInteraction,
        *,
        user: disnake.User = commands.Param(
            name=localized("cmd.remove_dj.opt.member.name"),
            description=localized("cmd.remove_dj.opt.member.description"),
        ),
    ):
        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        user = guild.get_member(user.id)

        if user.id == player.player_creator:
            if inter.author.guild_permissions.manage_guild:
                player.player_creator = None
            else:
                raise GenericError(f"**O membro {user.mention} é o criador do player.**")

        elif user.id not in player.dj:
            GenericError(f"O membro {user.mention} não está na lista de DJ's")

        else:
            player.dj.remove(user.id)

        text = [
            f"removeu {user.mention} da lista de DJ's.",
            f"🎧 **⠂{inter.author.mention} removeu {user.mention} da lista de DJ's.**",
        ]

        if (player.static and channel == player.text_channel) or isinstance(
            inter.application_command, commands.InvokableApplicationCommand
        ):
            await inter.send(f"{user.mention} adicionado à lista de DJ's!{player.controller_link}")

        await self.interaction_message(inter, txt=text, emoji="🎧")

    @has_player()
    @check_voice()
    @pool_command(
        name="commandlog",
        aliases=["cmdlog", "cl"],
        only_voiced=True,
        description="Ver o log de uso dos comandos.",
    )
    async def command_log_legacy(self, ctx: CustomContext):
        await self.command_log.callback(self=self, inter=ctx)

    @has_player(check_node=False)
    @check_voice()
    @commands.slash_command(description=localized("cmd.command_log.description"), extras={"only_voiced": True})
    @commands.contexts(guild=True)
    async def command_log(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.command_log_list:
            raise GenericError("**O Log de comandos está vazio...**")

        embed = disnake.Embed(
            description="### Log de comandos:\n"
            + "\n\n".join(f"{i['emoji']} ⠂{i['text']}\n<t:{int(i['timestamp'])}:R>" for i in player.command_log_list),
            color=player.guild.me.color,
        )

        if isinstance(inter, CustomContext):
            await inter.reply(embed=embed)
        else:
            await inter.send(embed=embed, ephemeral=True)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="stop",
        aliases=["leave", "parar"],
        only_voiced=True,
        description="Parar o player e me desconectar do canal de voz.",
    )
    async def stop_legacy(self, ctx: CustomContext):
        await self.stop.callback(self=self, inter=ctx)

    @is_dj()
    @has_player(check_node=False)
    @check_voice()
    @commands.slash_command(description=localized("cmd.stop.description"), extras={"only_voiced": True})
    @commands.contexts(guild=True)
    async def stop(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
            guild = inter.music_guild
            inter_destroy = inter if bot.user.id == self.bot.user.id else None
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            inter_destroy = inter

        player: LavalinkPlayer = bot.music.players[inter.guild_id]
        player.set_command_log(text=f"{inter.author.mention} **parou o player!**", emoji="🛑", controller=True)

        self.bot.pool.song_select_cooldown.get_bucket(inter).update_rate_limit()

        if isinstance(inter, disnake.MessageInteraction):
            await player.destroy(inter=inter_destroy)
        else:
            embed = disnake.Embed(
                color=self.bot.get_color(guild.me), description=f"🛑 **⠂{inter.author.mention} parou o player.**"
            )

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(
                        text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                    )
            except AttributeError:
                pass

            try:
                ephemeral = player.text_channel.id == inter.channel_id and player.static
            except Exception:
                ephemeral = player.static

            await inter.send(embed=embed, ephemeral=ephemeral)
            await player.destroy()

    @check_queue_loading()
    @has_player()
    @check_voice()
    @pool_command(
        name="savequeue",
        aliases=["sq", "salvarfila"],
        only_voiced=True,
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
        description="Experimental: Salvar a música e fila atual pra reusá-los a qualquer momento.",
    )
    async def savequeue_legacy(self, ctx: CustomContext):
        await self.save_queue.callback(self=self, inter=ctx)

    @check_queue_loading()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.save_queue.description"),
        extras={"only_voiced": True},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    @commands.contexts(guild=True)
    async def save_queue(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = bot.get_guild(inter.guild_id)

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        tracks = []

        if player.current:
            player.current.info["id"] = player.current.id
            if player.current.playlist:
                player.current.info["playlist"] = {
                    "name": player.current.playlist_name,
                    "url": player.current.playlist_url,
                }
            tracks.append(player.current.info)

        for t in player.queue:
            t.info["id"] = t.id
            if t.playlist:
                t.info["playlist"] = {"name": t.playlist_name, "url": t.playlist_url}
            tracks.append(t.info)

        if len(tracks) < 3:
            raise GenericError(f"**É necessário ter no mínimo 3 músicas pra salvar (atual e/ou na fila)**")

        await store_saved_queue(inter.author.id, tracks, inter.guild_id, disnake.utils.utcnow())

        await inter.response.defer(ephemeral=True)

        global_data = await self.bot.get_global_data(guild.id, db_name=DBModel.guilds)

        try:
            slashcmd = (
                f"</play:"
                + str(self.bot.get_global_command_named("play", cmd_type=disnake.ApplicationCommandType.chat_input).id)
                + ">"
            )
        except AttributeError:
            slashcmd = "/play"

        embed = disnake.Embed(
            color=bot.get_color(guild.me),
            description=f"### {inter.author.mention}: A fila foi salva com sucesso!!\n"
            f"**Músicas salvas:** `{len(tracks)}`\n"
            "### Como usar?\n"
            f"* Usando o comando {slashcmd} (selecionando no preenchimento automático da busca)\n"
            "* Clicando no botão/select de tocar favorito/integração do player.\n"
            f"* Usando o comando {global_data['prefix'] or self.bot.default_prefix}{self.play_legacy.name} "
            "sem incluir um nome ou link de uma música/vídeo.",
        )

        embed.set_footer(
            text="Nota: Esse é um recurso muito experimental, a fila salva pode sofrer alterações ou ser "
            "removida em futuros updates"
        )

        if isinstance(inter, CustomContext):
            await inter.reply(embed=embed)
        else:
            await inter.edit_original_response(embed=embed)
