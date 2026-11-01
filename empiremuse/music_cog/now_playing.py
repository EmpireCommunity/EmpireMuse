# -*- coding: utf-8 -*-
import itertools
from typing import Optional

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    check_voice,
    has_source,
    is_dj,
)
from empiremuse.utils.music.converters import time_format, music_source_image, md_link
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    check_cmd,
    CustomContext,
    PlayerControls,
    pool_command,
    ProgressBar,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class NowPlayingMixin:
    np_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.member)

    @commands.command(
        name="nowplaying",
        aliases=["np", "npl", "current", "tocando", "playing"],
        description="Exibir informações da música que você está ouvindo no momento.",
        cooldown=np_cd,
    )
    async def now_playing_legacy(self, ctx: CustomContext):
        await self.now_playing.callback(self=self, inter=ctx)

    @commands.slash_command(
        description=localized("cmd.now_playing.description"),
        cooldown=np_cd,
        extras={"allow_private": True},
    )
    @commands.contexts(guild=True)
    async def now_playing(self, inter: disnake.ApplicationCommandInteraction):
        player: Optional[LavalinkPlayer] = None

        for bot in self.bot.pool.get_guild_bots(inter.guild_id):
            try:
                p = bot.music.players[inter.guild_id]
            except KeyError:
                continue

            if not p.last_channel:
                continue

            if inter.author.id in p.last_channel.voice_states:
                player = p
                break

        if not player:
            if isinstance(inter, CustomContext) and not (await self.bot.is_owner(inter.author)):
                try:
                    slashcmd = (
                        f"</now_playing:"
                        + str(
                            self.bot.get_global_command_named(
                                "now_playing", cmd_type=disnake.ApplicationCommandType.chat_input
                            ).id
                        )
                        + ">"
                    )
                except AttributeError:
                    slashcmd = "/now_playing"

                raise GenericError(
                    "**Você deve estar conectado em um canal de voz do servidor atual onde há player ativo...**\n"
                    f"`Nota: Caso esteja ouvindo em outro servidor você pode usar o comando` {slashcmd}"
                )

            for bot in self.bot.pool.get_guild_bots(inter.guild_id):
                for player_id in bot.music.players:
                    if player_id == inter.guild_id:
                        continue

                    if inter.author.id in (p := bot.music.players[player_id]).last_channel.voice_states:
                        player = p
                        break

        if not player:
            raise GenericError("**Você deve estar conectado em um canal de voz com player ativo...**")

        if not player.current:
            raise GenericError(f"**No momento não estou tocando algo no canal {player.last_channel.mention}**")

        guild_data = await player.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        ephemeral = (
            player.guild.id != inter.guild_id and not await player.bot.is_owner(inter.author)
        ) or await self.is_request_channel(inter, data=guild_data)

        url = player.current.uri or player.current.search_uri

        if player.current.info["sourceName"] == "youtube":
            url += f"&t={int(player.position / 1000)}s"

        txt = f"### {md_link(player.current.title, url, code=False)}\n"

        footer_kw = {}

        if player.current.is_stream:
            txt += f"> {app_emoji(inter.bot, 'record')} **⠂Transmissão ao vivo**\n"
        else:
            progress = ProgressBar(player.position, player.current.duration, bar_count=8)

            txt += (
                f"```ansi\n[34;1m[{time_format(player.position)}] {('=' * progress.start)}[0m🔴️[36;1m{'-' * progress.end} "
                f"[{time_format(player.current.duration)}][0m```\n"
            )

        txt += f"> {app_emoji(inter.bot, 'microphone')} **⠂Uploader:** {player.current.authors_md}\n"

        if player.current.album_name:
            txt += f"> {app_emoji(inter.bot, 'disc')} **⠂Álbum:** {md_link(player.current.album_name, player.current.album_url, limit=20)}\n"

        if not player.current.autoplay:
            txt += f"> {app_emoji(inter.bot, 'user')} **⠂Solicitado por:** <@{player.current.requester}>\n"
        else:
            try:
                mode = f" {md_link('Recomendação', player.current.info['extra']['related']['uri'])}"
            except Exception:
                mode = "`Recomendação`"
            txt += f"> {app_emoji(inter.bot, 'like')} **⠂Adicionado via:** {mode}\n"

        if player.current.playlist_name:
            txt += f"> {app_emoji(inter.bot, 'playlists')} **⠂Playlist:** {md_link(player.current.playlist_name, player.current.playlist_url, limit=20)}\n"

        try:
            txt += f"> {app_emoji(inter.bot, 'speaker')} **⠂Canal de voz:** {player.guild.me.voice.channel.jump_url}\n"
        except AttributeError:
            pass

        txt += f"> {app_emoji(inter.bot, 'volume')} **⠂Volume:** `{player.volume}%`\n"

        components = [
            disnake.ui.Button(
                custom_id=f"np_{inter.author.id}", label="Atualizar", emoji=app_emoji(inter.bot, "refresh")
            )
        ]

        if player.guild_id != inter.guild_id:
            if (
                player.current
                and not player.paused
                and (
                    listeners := len(
                        [
                            m
                            for m in player.last_channel.members
                            if not m.bot and (not m.voice.self_deaf or not m.voice.deaf)
                        ]
                    )
                )
                > 1
            ):
                txt += f"> {app_emoji(inter.bot, 'headset')} **⠂Ouvintes atuais:** `{listeners}`\n"

            txt += f"> {app_emoji(inter.bot, 'history')} **⠂Player ativo:** <t:{player.uptime}:R>\n"

            try:
                footer_kw = {"icon_url": player.guild.icon.with_static_format("png").url}
            except AttributeError:
                pass

            footer_kw["text"] = f"No servidor: {player.guild.name} [ ID: {player.guild.id} ]"

        else:
            try:
                if player.bot.user.id != self.bot.user.id:
                    footer_kw["text"] = f"Bot selecionado: {player.bot.user.display_name}"
                    footer_kw["icon_url"] = player.bot.user.display_avatar.url
            except AttributeError:
                pass

        if player.keep_connected:
            txt += "> ♾️ **⠂Modo 24/7:** `Ativado`\n"

        if player.queue or player.queue_autoplay:
            if player.guild_id == inter.guild_id:
                txt += (
                    f"### {app_emoji(inter.bot, 'music_notes')} ⠂Próximas músicas ({(qsize := len(player.queue + player.queue_autoplay))}):\n"
                    + ("\n").join(
                        f"> `{n + 1})` {md_link(t.title, t.uri, limit=28)}\n"
                        f"> {app_emoji(inter.bot, 'history')} `{time_format(t.duration) if not t.is_stream else '🔴 Ao vivo'}`"
                        + (f" - `Repetições: {t.track_loops}`" if t.track_loops else "")
                        + f" **|** "
                        + (
                            f"{app_emoji(inter.bot, 'user')} <@{t.requester}>"
                            if not t.autoplay
                            else f"{app_emoji(inter.bot, 'like')} `Recomendada`"
                        )
                        for n, t in enumerate(itertools.islice(player.queue + player.queue_autoplay, 3))
                    )
                )

                if qsize > 3:
                    components.append(
                        disnake.ui.Button(
                            custom_id=PlayerControls.queue,
                            label="Ver lista completa",
                            emoji=app_emoji(inter.bot, "music_queue"),
                        )
                    )

            elif player.queue:
                txt += f"> {app_emoji(inter.bot, 'music_notes')} **⠂Músicas na fila:** `{len(player.queue)}`\n"

        if player.static and player.guild_id == inter.guild_id:
            if player.message:
                components.append(
                    disnake.ui.Button(url=player.message.jump_url, label="Ir p/ player-controller", emoji="🔳")
                )
            elif player.text_channel:
                txt += f"\n\n`Acesse o player-controller no canal:` {player.text_channel.mention}"

        embed = disnake.Embed(description=txt, color=self.bot.get_color(player.guild.me))

        embed.set_author(
            name=("⠂Tocando agora:" if inter.guild_id == player.guild_id else "Você está ouvindo agora:")
            if not player.paused
            else "⠂Música atual:",
            icon_url=music_source_image(player.current.info["sourceName"]),
        )

        embed.set_thumbnail(url=player.current.thumb)

        if footer_kw:
            embed.set_footer(**footer_kw)

        if isinstance(inter, disnake.MessageInteraction):
            await inter.response.edit_message(inter.author.mention, embed=embed, components=components)
        else:
            await inter.send(inter.author.mention, embed=embed, ephemeral=ephemeral, components=components)

    @commands.Cog.listener("on_button_click")
    async def reload_np(self, inter: disnake.MessageInteraction):
        if not inter.data.custom_id.startswith("np_"):
            return

        if inter.data.custom_id != f"np_{inter.author.id}":
            await inter.send("Você não pode clicar nesse botão...", ephemeral=True)
            return

        try:
            inter.application_command = self.now_playing_legacy
            await check_cmd(self.now_playing_legacy, inter)
            await self.now_playing_legacy(inter)
        except Exception as e:
            self.bot.dispatch("interaction_player_error", inter, e)

    controller_cd = commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.member)

    controller_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_source()
    @check_voice()
    @pool_command(
        name="controller",
        aliases=["ctl"],
        only_voiced=True,
        cooldown=controller_cd,
        max_concurrency=controller_mc,
        description="Enviar player controller para um canal específico/atual.",
    )
    async def controller_legacy(self, ctx: CustomContext):
        await self.controller.callback(self=self, inter=ctx)

    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.controller.description"),
        extras={"only_voiced": True},
        cooldown=controller_cd,
        max_concurrency=controller_mc,
    )
    @commands.contexts(guild=True)
    async def controller(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        if player.static:
            raise GenericError("Esse comando não pode ser usado no modo fixo do player.")

        if player.has_thread:
            raise GenericError(
                "**Esse comando não pode ser usado com uma conversa ativa na "
                f"{md_link('mensagem', player.message.jump_url, code=False)} do player.**"
            )

        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)

        if channel != player.text_channel:
            await is_dj().predicate(inter)

            try:
                player.set_command_log(
                    text=f"{inter.author.mention} moveu o player-controller para o canal {inter.channel.mention}.",
                    emoji="💠",
                )

                embed = disnake.Embed(
                    description=f"💠 **⠂{inter.author.mention} moveu o player-controller para o canal:** {channel.mention}",
                    color=self.bot.get_color(guild.me),
                )

                try:
                    if bot.user.id != self.bot.user.id:
                        embed.set_footer(
                            text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                        )
                except AttributeError:
                    pass

                await player.text_channel.send(embed=embed)

            except Exception:
                pass

        await player.destroy_message()

        player.text_channel = channel

        await player.invoke_np()

        if not isinstance(inter, CustomContext):
            await inter.edit_original_message("**Player reenviado com sucesso!**")
