# -*- coding: utf-8 -*-
import logging
import asyncio
import datetime
import traceback
from typing import Union, Optional

import disnake
from disnake.ext import commands

from empiremuse.rules.request_panel import StrictAction, strict_action
from empiremuse.rules.servers import request_channel_strict
from empiremuse.rules.usage import KIND_REQUEST
from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    can_connect,
    check_player_perm,
    pick_request_bot,
)
from empiremuse.utils.music.converters import (
    time_format,
    fix_characters,
    URL_REG,
    YOUTUBE_VIDEO_REG,
    music_source_image,
)
from empiremuse.utils.music.errors import (
    GenericError,
    PoolException,
    parse_error,
    error_code_text,
    log_unexpected_error,
)
from empiremuse.utils.music.interactions import (
    SelectInteraction,
    ButtonInteraction,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    send_idle_embed,
    CustomContext,
    PlayerControls,
    string_to_file,
    song_request_buttons,
    update_inter,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    logger,
)
from empiremuse.music_cog.song_request_state import ParseRequestState, SongRequestState
from empiremuse.utils.usage import label_for, usage_of
from empiremuse.music_cog.song_request_steps import (
    _pr_checks,
    _pr_playlist,
    _pr_single,
    _sr_empty_content,
    _sr_prepare_query,
    _sr_validate_channel,
)


async def _request_channel_strict(self, message: disnake.Message) -> bool:
    """O servidor liga a Jukebox estrita? Sem servidor, documento antigo ou store indisponível, não (o canal fica como sempre foi)."""
    if message.guild is None:
        return False
    try:
        return request_channel_strict(await self.bot.pool.servers.settings(message.guild.id))
    except Exception as error:  # noqa: BLE001 - sem a configuração o canal segue o fluxo de antes (não estrito), mas fica no log
        logger.warning(
            "Jukebox estrita: configuração do servidor não lida, seguindo sem o modo estrito (%s)", type(error).__name__
        )
        return False


class SongRequestsMixin:
    @commands.Cog.listener("on_modal_submit")
    async def song_request_modal(self, inter: disnake.ModalInteraction):
        if inter.custom_id.startswith("modal_add_song"):
            try:
                query = inter.values["song_input"]
                position = inter.values["song_position"]

                try:
                    selected_fav = inter.values["fav_links"][0]
                except KeyError, IndexError:
                    selected_fav = None

                try:
                    selected_integration = inter.values["integration_links"][0]
                except KeyError, IndexError:
                    selected_integration = None

                multichoice_opts = []

                if query:
                    multichoice_opts.append(
                        disnake.SelectOption(
                            label="Nome/Link:",
                            emoji="🔍",
                            description=fix_characters(query, limit=45),
                            value="music_query",
                        )
                    )

                if selected_fav:
                    multichoice_opts.append(
                        disnake.SelectOption(
                            label="Favorito:",
                            emoji="⭐",
                            description=fix_characters(selected_fav[6:], 45),
                            value="music_fav",
                        )
                    )

                if selected_integration:
                    multichoice_opts.append(
                        disnake.SelectOption(
                            label="Integração:",
                            emoji="💠",
                            description=fix_characters(selected_integration[13:], 45),
                            value="music_integration",
                        )
                    )

                if not multichoice_opts:
                    raise GenericError("Você deve incluir pelo menos uma informação")

                if len(multichoice_opts) > 1:
                    view = SelectInteraction(user=inter.author, opts=multichoice_opts, timeout=30)

                    embed = disnake.Embed(
                        description="**Você usou dois itens na sua requisição...**\n"
                        f"Selecione uma opção para prosseguir (tempo limite: <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=30)).timestamp())}:R>).",
                        color=self.bot.get_color(inter.guild.me),
                    )

                    await inter.send(inter.author.mention, embed=embed, view=view, ephemeral=True)

                    await view.wait()

                    if not view.inter:
                        await inter.edit_original_message(
                            content=f"{inter.author.mention}, tempo esgotado!", embed=None, view=None
                        )
                        return

                    update_inter(inter, view.inter)

                    inter = view.inter

                    selected_opt = view.selected

                    await inter.response.defer(ephemeral=True)

                else:
                    selected_opt = multichoice_opts[0].value

                match selected_opt:
                    case "music_fav":
                        query = selected_fav
                    case "music_integration":
                        query = selected_integration

                kwargs = {
                    "query": query,
                    "position": int(position) if position else 0,
                    "options": False,
                    "manual_selection": True,
                    "server": None,
                    "force_play": "no",
                }

                # o modal é do painel; resolvido AGORA (S7b): trocou de call depois de abrir, vale a call atual
                cog = await self.request_executor(inter)
                await cog.process_player_interaction(
                    interaction=inter,
                    command=cog.bot.get_slash_command("play"),
                    kwargs=kwargs,
                )
            except Exception as e:
                self.bot.dispatch("interaction_player_error", inter, e)

    async def song_request_executor(self, req):
        """O cog ``Music`` do bot que toca o que foi digitado: o que já está na call de quem pediu, senão o 1º livre (S7)."""
        bot = pick_request_bot(self.bot.pool, req.message.guild.id, req.message.author)
        if bot is None:
            raise GenericError(
                "Todos os bots de música estão em uso no momento. Entre na call de um deles ou aguarde um ficar livre."
            )
        return self if bot is self.bot else bot.get_cog("Music")

    async def delete_message(self, message: disnake.Message, delay: int = None, ignore=False):
        if ignore:
            return

        try:
            is_forum = isinstance(message.channel.parent, disnake.ForumChannel)
        except AttributeError:
            is_forum = False

        if message.is_system() and is_forum:
            return

        if message.channel.permissions_for(message.guild.me).manage_messages or message.author.id == self.bot.user.id:
            try:
                await message.delete(delay=delay)
            except disnake.NotFound:
                pass  # já apagada (a Jukebox estrita apaga na hora; o fluxo do pedido tenta de novo no fim)
            except Exception:
                logger.exception("Falha em delete_message")

    @commands.Cog.listener("on_song_request")
    async def song_requests(self, ctx: Optional[CustomContext], message: disnake.Message):
        req = SongRequestState(ctx=ctx, message=message)
        if req.ctx.command:
            return

        if req.message.author.bot and not isinstance(req.message.channel, disnake.StageChannel):
            return

        try:
            req.data = await self.bot.get_data(req.message.guild.id, db_name=DBModel.guilds)
        except AttributeError:
            return

        req.player = self.bot.music.players.get(req.message.guild.id)

        if await _sr_validate_channel(self, req):
            return

        # só aqui, já sabendo que a mensagem é do canal de pedidos (este listener roda em toda mensagem de todo servidor)
        strict = await _request_channel_strict(self, req.message)
        if req.message.mentions and not strict:
            return  # sem o modo estrito, mensagem com menção segue ignorada como sempre foi
        action = strict_action(
            strict,
            author_is_bot=req.message.author.bot,
            is_command=False,
            has_mentions=bool(req.message.mentions),
            is_system=req.message.is_system(),
        )
        if action is StrictAction.DELETE:
            await self.delete_message(req.message)  # texto que não é pedido: some na hora, sem resposta
            return
        # o nome digitado vale como pedido, mas a mensagem já não fica no canal. Arquivo de áudio sem texto fica: apagar a mensagem
        # apaga o anexo e o Lavalink ainda vai baixá-lo (o fluxo a apaga no fim).
        if action is StrictAction.DELETE_AND_PROCESS and not (req.message.attachments and not req.message.content):
            await self.delete_message(req.message)

        if req.message.content.startswith("/") or req.message.is_system():
            await self.delete_message(req.message)
            return

        try:
            if isinstance(req.message.channel, disnake.Thread):
                if isinstance(req.message.channel.parent, disnake.ForumChannel):
                    if req.data["player_controller"]["channel"] != str(req.message.channel.id):
                        return
                    await self.delete_message(req.message)

        except AttributeError:
            pass

        req.msg = None
        error = None
        failure = None
        req.error_code = ""
        req.has_exception = None

        try:
            if req.message.author.bot:
                await self.delete_message(req.message)
                return

            if await _sr_empty_content(self, req):
                if action is StrictAction.DELETE_AND_PROCESS:
                    await self.delete_message(
                        req.message
                    )  # anexo que não é áudio, grande demais ou sem nada: no modo estrito também some
                return

            if await _sr_prepare_query(self, req):
                return

            executor = await self.song_request_executor(req)
            await executor.parse_song_request(
                req.message, req.text_channel, req.data, response=req.msg, source=req.source
            )

        except GenericError as e:
            failure = e
            error = f"{req.message.author.mention}. {e}"

        except Exception as e:
            if isinstance(e, PoolException):
                return

            failure = e

            try:
                error_msg = parse_error(req.ctx, e, no_log=True)[0]
            except Exception:
                error_msg = None

            if error_msg:
                error = f"{req.message.author.mention}. {error_msg}"
            else:
                req.has_exception = e
                req.error_code = log_unexpected_error(logger, e, "pedido de música (song-request)")
                error = (
                    f"{req.message.author.mention} **ocorreu um erro ao tentar obter resultados para sua busca.**"
                    f"{error_code_text(req.error_code)}"
                )

        if usage := usage_of(self.bot):
            usage.record_command(
                KIND_REQUEST,
                "song_request",
                req.message,
                bot=self.bot,
                ok=failure is None,
                error=None if failure is None else label_for(failure),
            )

        if error:
            await self.delete_message(req.message)

            try:
                if req.msg:
                    await req.msg.edit(content=error, embed=None, view=None)
                else:
                    await req.message.channel.send(error, delete_after=9)
            except Exception:
                logger.exception("Falha em song_requests")

        await self.song_request_concurrency.release(req.message)

        if req.has_exception and self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"]:
            await _sr_error_webhook(self, req)

    async def parse_song_request(
        self,
        message: disnake.Message,
        text_channel,
        data,
        *,
        response=None,
        attachment: disnake.Attachment = None,
        source=None,
    ):
        req = ParseRequestState(
            message=message,
            text_channel=text_channel,
            data=data,
            response=response,
            attachment=attachment,
            source=source,
        )
        await _pr_checks(self, req)

        if not isinstance(req.tracks, list):
            await _pr_playlist(self, req)

        else:
            await _pr_single(self, req)

        if not req.player.is_connected:
            guild = self.bot.get_guild(req.message.guild.id) or req.message.guild
            await self.do_connect(
                req.message,
                channel=guild.get_channel(req.message.author.voice.channel.id) or req.message.author.voice.channel,
                check_other_bots_in_vc=req.data["check_other_bots_in_vc"],
                bot=self.bot,
                me=guild.me,
            )

        if not req.player.current:
            await req.player.process_next()
        else:
            await req.player.update_message()

        await asyncio.sleep(1)

    async def interaction_message(
        self,
        inter: Union[disnake.Interaction, CustomContext],
        txt,
        emoji: str = "✅",
        rpc_update: bool = False,
        data: dict = None,
        store_embed: bool = False,
        force=False,
        defered=False,
        thumb=None,
        components=None,
    ):
        try:
            txt, txt_ephemeral = txt
        except Exception:
            txt_ephemeral = False

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        component_interaction = isinstance(inter, disnake.MessageInteraction)

        ephemeral = await self.is_request_channel(inter, data=data)

        if ephemeral:
            player.set_command_log(text=f"{inter.author.mention} {txt}", emoji=emoji)
            player.update = True

        await player.update_message(
            interaction=inter if (bot.user.id == self.bot.user.id and component_interaction) else False,
            rpc_update=rpc_update,
            force=force,
        )

        if isinstance(inter, CustomContext):
            embed = disnake.Embed(
                color=self.bot.get_color(guild.me), description=f"{txt_ephemeral or txt}{player.controller_link}"
            )

            if thumb:
                embed.set_thumbnail(url=thumb)

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(
                        text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                    )
            except AttributeError:
                pass

            if store_embed and not player.controller_mode and len(player.queue) > 0:
                player.temp_embed = embed

            else:
                kwargs = {"components": components} if components else {}
                try:
                    await inter.store_message.edit(embed=embed, view=None, content=None, **kwargs)
                except AttributeError:
                    await inter.send(embed=embed, **kwargs)

        elif not component_interaction:
            kwargs = {"components": components} if components else {}

            embed = disnake.Embed(
                color=self.bot.get_color(guild.me),
                description=(txt_ephemeral or f"{inter.author.mention} **{txt}**") + player.controller_link,
            )

            if thumb:
                embed.set_thumbnail(url=thumb)

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(
                        text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                    )
            except AttributeError:
                pass

            if not inter.response.is_done():
                await inter.send(embed=embed, ephemeral=ephemeral, **kwargs)

            elif defered:
                await inter.edit_original_response(embed=embed, **kwargs)

    @commands.Cog.listener("on_thread_create")
    async def thread_song_request(self, thread: disnake.Thread, reopen: bool = False, bot: BotCore = None):
        if not bot:
            bot = self.bot

        try:
            player: LavalinkPlayer = bot.music.players[thread.guild.id]
        except KeyError:
            return

        if player.static or player.message.id != thread.id:
            return

        if not thread.parent.permissions_for(thread.guild.me).send_messages_in_threads:
            await player.text_channel.send(
                embed=disnake.Embed(
                    color=self.bot.get_color(thread.guild.me),
                    description="**Não tenho permissão de enviar mensagens em conversas do canal atual para ativar "
                    "o sistema de song-request...**\n\n"
                    f"Mensagens enviadas na conversa {thread.mention} serão ignoradas.",
                ),
                delete_after=30,
            )
            return

        embed = disnake.Embed(color=bot.get_color(thread.guild.me))

        if not bot.intents.message_content:
            embed.description = (
                "**Aviso! Não estou com a intent de message_content ativada por meu desenvolvedor...\n"
                "A funcionalidade de pedir música aqui pode não ter um resultado esperado...**"
            )

        elif not player.controller_mode:
            embed.description = (
                "**A skin/aparência atual não é compatível com o sistem de song-request "
                "via thread/conversa\n\n"
                "Nota:** `Esse sistema requer uma skin que use botões.`"
            )

        else:
            if reopen:
                embed.description = "**A sessão para pedidos de música nessa conversa foi reaberta na conversa atual.**"
            else:
                embed.description = (
                    "**Essa conversa será usada temporariamente para pedido de músicas.**\n\n"
                    "**Peça sua música aqui enviando o nome dela ou o link de uma música/vídeo "
                    "que seja de uma das seguintes plataformas suportadas:**\n"
                    "[`Youtube`](<https://www.youtube.com/>), [`Soundcloud`](<https://soundcloud.com/>), "
                    "[`Spotify`](<https://open.spotify.com/>), [`Twitch`](<https://www.twitch.tv/>)"
                )

        await thread.send(embed=embed)


async def _sr_error_webhook(self, req):
    """Envia o relatório do erro inesperado para o webhook configurado."""
    ctx = req.ctx
    error_code = req.error_code
    has_exception = req.has_exception
    message = req.message

    cog = self.bot.get_cog("ErrorHandler")

    if not cog:
        return True

    max_concurrency = cog.webhook_max_concurrency

    await max_concurrency.acquire(message)

    try:
        full_error_msg = cog.mask_token(
            "".join(traceback.format_exception(type(has_exception), has_exception, has_exception.__traceback__))
        )

        embed = disnake.Embed(
            title="Ocorreu um erro em um servidor (song-request):",
            timestamp=disnake.utils.utcnow(),
            description=f"Erro inesperado no pedido de música.{error_code_text(error_code)}",
        )

        embed.set_footer(
            text=f"{message.author} [{message.author.id}]",
            icon_url=message.author.display_avatar.with_static_format("png").url,
        )

        embed.add_field(
            name="Servidor:",
            inline=False,
            value=f"```\n{disnake.utils.escape_markdown(ctx.guild.name)}\nID: {ctx.guild.id}```",
        )

        embed.add_field(name="Conteúdo do pedido de música:", inline=False, value=f"```\n{message.content}```")

        embed.add_field(
            name="Canal de texto:",
            inline=False,
            value=f"```\n{disnake.utils.escape_markdown(ctx.channel.name)}\nID: {ctx.channel.id}```",
        )

        if vc := ctx.author.voice:
            embed.add_field(
                name="Canal de voz (user):",
                inline=False,
                value=f"```\n{disnake.utils.escape_markdown(vc.channel.name)}"
                + (f" ({len(vc.channel.voice_states)}/{vc.channel.user_limit})" if vc.channel.user_limit else "")
                + f"\nID: {vc.channel.id}```",
            )

        if vcbot := ctx.guild.me.voice:
            if vcbot.channel != vc.channel:
                embed.add_field(
                    name="Canal de voz (bot):",
                    inline=False,
                    value=f"{vc.channel.name}"
                    + (f" ({len(vc.channel.voice_states)}/{vc.channel.user_limit})" if vc.channel.user_limit else "")
                    + f"\nID: {vc.channel.id}```",
                )

        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.with_static_format("png").url)

        await cog.send_webhook(
            embed=embed,
            file=string_to_file(
                full_error_msg,
                f"error_traceback_songrequest_{error_code}.txt" if error_code else "error_traceback_songrequest.txt",
            ),
        )

    except Exception:
        logger.exception("Falha em _sr_error_webhook")

    await asyncio.sleep(20)

    try:
        await max_concurrency.release(message)
    except Exception:
        pass
    return False
