# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional, Union

import disnake
from aiohttp import ClientSession
from disnake.ext import commands

from empiremuse.logs.errors import command_context
from empiremuse.logs.redact import MAX_TRACEBACK_CHARS, sanitize_value, scrub
from empiremuse.rules.error_code import safe_error_code
from empiremuse.utils.music.converters import URL_REG, fix_characters
from empiremuse.utils.music.errors import parse_error, PoolException, error_code_text
from empiremuse.utils.others import send_message, CustomContext, string_to_file, paginator, check_cmd

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

# Erro inesperado: o usuário nunca vê repr, traceback, caminho de arquivo nem configuração interna; o detalhe completo
# vai para o log do servidor e para o webhook de erro.
UNEXPECTED_ERROR_TEXT = "Ocorreu um erro inesperado ao executar o comando. Tente novamente em instantes."


# Limites do Discord: nome de servidor, canal e usuário até 100; descrição do embed até 4096 (sobra para as crases e o título).
NAME_LIMIT = 100
DESCRIPTION_TEXT_LIMIT = 4000


def _safe_text(text: object) -> str:
    """Nome de servidor, canal ou autor para o embed do relatório.

    O saneamento e o redator vêm antes de ``fix_characters`` (que troca ``_``, ``*`` e ``[`` e esconderia um segredo
    registrado do redator) e de novo depois dele, para conferir o nome na forma final; as quebras de linha viram ``⏎``
    para o nome não forjar uma linha ``ID:`` no bloco de código.
    """
    return scrub(fix_characters(scrub(sanitize_value(str(text)), NAME_LIMIT)), NAME_LIMIT)[:NAME_LIMIT]


class ErrorHandler(commands.Cog):
    def __init__(self, bot: BotCore):
        self.bot = bot
        self.components = []
        self.webhook_max_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=True)

        if not self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] and self.bot.config["ERROR_REPORT_WEBHOOK"]:
            self.components.append(disnake.ui.Button(label="Reporte esse erro", custom_id="report_error", emoji="⚠️"))

        if self.bot.config["SUPPORT_SERVER"]:
            self.components.append(
                disnake.ui.Button(label="Servidor de suporte", url=self.bot.config["SUPPORT_SERVER"], emoji="💻")
            )

    def report_embed_with_code(self, ctx, code: str) -> disnake.Embed:
        """Embed do relatório do webhook, com o código do erro quando houver."""
        embed = self.build_report_embed(ctx)
        if code:
            embed.add_field(name="Código do erro:", value=f"`{code}`", inline=False)
        return embed

    @staticmethod
    def traceback_filename(kind: str, code: str) -> str:
        """Nome do anexo do traceback; o código (só hexadecimal e hífen) entra no nome para achar o relatório."""
        return f"error_traceback_{kind}_{code}.txt" if code else f"error_traceback_{kind}.txt"

    def mask_token(self, text: str) -> str:
        token = self.bot.http.token
        return text.replace(token, "mytoken") if token else text

    def clean_for_webhook(self, text: str) -> str:
        """Texto do relatório do webhook: o token deste bot mascarado e, em seguida, o saneamento e o redator único do log.

        Assim o traceback mandado ao webhook não leva a senha de um nó, a URI do Mongo nem o token de outro bot, e o ESC é saneado.
        """
        return scrub(self.mask_token(text), MAX_TRACEBACK_CHARS)

    def log_unexpected_error(self, error: BaseException, origin: str, ctx: object = None) -> str:
        """Registra o erro inesperado com o traceback completo (só no log do servidor) e devolve o código dele.

        O código (``ERR-…``) vai na mensagem do log, para o usuário e para o webhook; ``""`` se não foi possível gerar.
        """
        code = safe_error_code()
        error = getattr(error, "original", error)
        # Só IDs e um resumo por tipo dos parâmetros (V-L4): nunca nome, conteúdo nem URL de anexo.
        context = f" {command_context(ctx, level=logging.ERROR)}" if ctx is not None else ""
        logger.error(
            "Erro inesperado no comando (%s)%s%s",
            origin,
            f" [{code}]" if code else "",
            context,
            exc_info=(type(error), error, error.__traceback__),
            # O agregador usa o código para apontar o traceback já impresso.
            extra={"error_code": code} if code else None,
        )
        return code

    @commands.Cog.listener("on_interaction_player_error")
    async def on_inter_player_error(self, inter: disnake.ApplicationCommandInteraction, error: Exception):
        if not isinstance(error, commands.MaxConcurrencyReached):
            try:
                await inter.application_command._max_concurrency.release(inter)
            except Exception:
                pass

        await self.process_interaction_error(inter=inter, error=error)

    """@commands.Cog.listener('on_user_command_completion')
    @commands.Cog.listener('on_message_command_completion')
    @commands.Cog.listener('on_slash_command_completion')
    async def interaction_command_completion(self, inter: disnake.ApplicationCommandInteraction):

        try:
            await inter.application_command._max_concurrency.release(inter)
        except Exception:
            pass


    @commands.Cog.listener("on_command_completion")
    async def legacy_command_completion(self, ctx: CustomContext):

        try:
            await ctx.command._max_concurrency.release(ctx.message)
        except Exception:
            pass"""

    @commands.Cog.listener("on_custom_error")
    async def custom_error_event(
        self, ctx: Union[disnake.ApplicationCommandInteraction, CustomContext], error: Exception
    ):
        if isinstance(ctx, (CustomContext, disnake.Message)):
            await self.on_legacy_command_error(ctx=ctx, error=error, resp_msg=False)
        else:
            await self.process_interaction_error(inter=ctx, error=error, resp_msg=False)

    @commands.Cog.listener("on_custom_slash_command_error")
    @commands.Cog.listener("on_user_command_error")
    @commands.Cog.listener("on_message_command_error")
    @commands.Cog.listener("on_slash_command_error")
    async def on_interaction_command_error(
        self, inter: disnake.ApplicationCommandInteraction, error: Exception, **kwargs
    ):
        await self.process_interaction_error(inter=inter, error=error, **kwargs)

    async def process_interaction_error(
        self, inter: disnake.ApplicationCommandInteraction, error: Exception, resp_msg=True, **kwargs
    ):
        if isinstance(error, PoolException):
            return

        no_log = kwargs.get("no_log", False)
        error_msg, full_error_msg, kill_process, components, mention_author = parse_error(
            inter, error, **{**kwargs, "no_log": True}
        )

        if isinstance(error, disnake.NotFound) and str(error).endswith("Unknown Interaction"):
            return

        kwargs = {"text": ""}
        color = disnake.Color.red()
        send_webhook = False
        code = ""

        try:
            if inter.message.author.bot or mention_author:
                kwargs["text"] = inter.author.mention
        except AttributeError:
            pass

        if not error_msg:
            components = self.components

            if not no_log:
                code = self.log_unexpected_error(error, f"interação /{getattr(inter.data, 'name', '?')}", inter)

            kwargs["embed"] = disnake.Embed(
                color=color, title="Ocorreu um erro no comando:", description=UNEXPECTED_ERROR_TEXT
            )

            if self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"]:
                send_webhook = True
                kwargs["embed"].description += " `Meu desenvolvedor será notificado sobre o problema.`"

            kwargs["embed"].description += error_code_text(code)

        else:
            logger.debug("Erro esperado no comando: %s", type(error).__name__)
            kwargs["embeds"] = []

            for p in paginator(error_msg):
                kwargs["embeds"].append(disnake.Embed(color=color, description=p))

        if resp_msg:
            try:
                await send_message(inter, components=components, **kwargs)
            except Exception:
                logger.exception("Falha ao responder o erro ao usuário")
        else:
            send_webhook = True

        if kill_process:
            await self.bot.pool.shutdown(1)
            return

        if not send_webhook or not full_error_msg:
            return

        acquired = False
        try:
            await self.webhook_max_concurrency.acquire(inter)
            acquired = True

            await self.send_webhook(
                embed=self.report_embed_with_code(inter, code),
                file=string_to_file(
                    self.clean_for_webhook(full_error_msg), self.traceback_filename("interaction", code)
                ),
            )

            await asyncio.sleep(20)

        except Exception:
            logger.exception("Falha ao enviar o relatório de erro ao webhook")
        finally:
            # Uma falha do webhook não pode deixar o servidor adquirido para sempre.
            if acquired:
                await self.webhook_max_concurrency.release(inter)

    async def do_playcmd(self, ctx: CustomContext):
        if not self.bot.pool.config["ENABLE_SONGREQUEST_MENTION"]:
            # em testes
            ctx.bot.dispatch("custom_message", ctx.message)
            return

        query = str(ctx.message.content)

        for m in ctx.message.mentions:
            query = query.replace(m.mention, "", 1)

        query = query.strip()

        if query:
            play_cmd = self.bot.get_slash_command("play")

            try:
                await check_cmd(play_cmd, ctx)
                await play_cmd.callback(
                    inter=ctx,
                    query=query,
                    self=play_cmd.cog,
                    position=0,
                    options=False,
                    force_play="no",
                    manual_selection=False,
                    server=None,
                )
            except commands.CommandNotFound:
                return
            except Exception as e:
                await self.on_legacy_command_error(ctx, e)

    @commands.Cog.listener("on_command_error")
    async def on_legacy_command_error(self, ctx: CustomContext, error: Exception, resp_msg=True):
        """if not isinstance(error, commands.MaxConcurrencyReached):
        try:
            await ctx.command._max_concurrency.release(ctx.message)
        except Exception:
            pass"""

        if isinstance(error, PoolException):
            return

        ctx.command_failed = True

        if isinstance(error, commands.CommandNotFound):
            if ctx.prefix.startswith(self.bot.user.mention):
                await self.do_playcmd(ctx)
            return

        if isinstance(error, commands.NotOwner):
            logger.debug("Usuário %s não é dono do bot para usar o comando: %s", ctx.author.id, ctx.command.name)
            return

        try:
            bot = ctx.bot
        except AttributeError:
            bot = self.bot

        if isinstance(error, commands.MissingPermissions) and (await bot.is_owner(ctx.author)):
            try:
                await ctx.reinvoke()
            except Exception as e:
                await self.on_legacy_command_error(ctx, e)
            return

        if isinstance(error, commands.TooManyArguments):
            if ctx.prefix.startswith(self.bot.user.mention):
                await self.do_playcmd(ctx)
            else:
                if ctx.cog:
                    try:
                        ctx.args.remove(ctx.cog)
                    except Exception:
                        pass
                try:
                    await ctx.command(*ctx.args, **ctx.kwargs)
                except Exception as e:
                    await self.on_legacy_command_error(ctx, e)
            return

        error_msg, full_error_msg, kill_process, components, mention_author = parse_error(ctx, error, no_log=True)
        kwargs = {"content": ""}
        send_webhook = False
        code = ""

        if ctx.author.bot or mention_author:
            kwargs["content"] = ctx.author.mention

        if not error_msg:
            components = self.components

            code = self.log_unexpected_error(error, f"prefixo {getattr(ctx.command, 'name', '?')}", ctx)

            if ctx.channel.permissions_for(ctx.guild.me).embed_links:
                kwargs["embed"] = disnake.Embed(
                    color=disnake.Colour.red(), title="Ocorreu um erro no comando:", description=UNEXPECTED_ERROR_TEXT
                )
                if self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"]:
                    send_webhook = True
                    kwargs["embed"].description += " `Meu desenvolvedor será notificado sobre o problema.`"

                kwargs["embed"].description += error_code_text(code)

            else:
                kwargs["content"] += (
                    f"\n**Ocorreu um erro no comando:**\n{UNEXPECTED_ERROR_TEXT}{error_code_text(code)}"
                )

        else:
            logger.debug("Erro esperado no comando: %s", type(error).__name__)
            if ctx.channel.permissions_for(ctx.guild.me).embed_links:
                kwargs["embed"] = disnake.Embed(color=disnake.Colour.red(), description=error_msg)
            else:
                kwargs["content"] += f"\n{error_msg}"

        try:
            kwargs["delete_after"] = error.delete_original
        except AttributeError:
            pass

        try:
            if error.self_delete and ctx.channel.permissions_for(ctx.guild.me).manage_messages:
                await ctx.message.delete()
        except Exception:
            pass

        if resp_msg:
            if hasattr(ctx, "inter"):
                if ctx.inter.response.is_done():
                    func = ctx.inter.edit_original_message
                else:
                    func = ctx.inter.response.edit_message
                kwargs.pop("delete_after", None)
            else:
                try:
                    func = ctx.store_message.edit
                except Exception:
                    func = ctx.send

            await func(components=components, **kwargs)

        else:
            send_webhook = True

        if kill_process:
            await self.bot.pool.shutdown(1)
            return

        if not send_webhook or not full_error_msg:
            return

        acquired = False
        try:
            await self.webhook_max_concurrency.acquire(ctx)
            acquired = True

            await self.send_webhook(
                embed=self.report_embed_with_code(ctx, code),
                file=string_to_file(self.clean_for_webhook(full_error_msg), self.traceback_filename("prefixed", code)),
            )

            await asyncio.sleep(20)

        except Exception:
            logger.exception("Falha ao enviar o relatório de erro ao webhook")
        finally:
            # Uma falha do webhook não pode deixar o servidor adquirido para sempre.
            if acquired:
                await self.webhook_max_concurrency.release(ctx)

    @commands.Cog.listener("on_button_click")
    async def on_error_report(self, inter: disnake.MessageInteraction):
        if inter.data.custom_id != "report_error":
            return

        if str(inter.author.id) not in inter.message.content:
            await inter.send(f"Apenas o usuário {inter.message.content} pode usar esse botão!", ephemeral=True)
            return

        await inter.response.send_modal(
            title="Reportar erro",
            custom_id=f"error_report_submit_{inter.message.id}",
            components=[
                disnake.ui.TextInput(
                    style=disnake.TextInputStyle.long,
                    label="Detalhes",
                    custom_id="error_details",
                    max_length=1900,
                    required=True,
                ),
                disnake.ui.TextInput(
                    style=disnake.TextInputStyle.short,
                    label="Link de imagem/print do erro (Opcional)",
                    custom_id="image_url",
                    max_length=300,
                    required=False,
                ),
            ],
        )

    @commands.Cog.listener("on_modal_submit")
    async def on_report_submit(self, inter: disnake.ModalInteraction):
        if not inter.custom_id.startswith("error_report_submit"):
            return

        if not inter.message.embeds:
            await inter.response.edit_message(
                embed=disnake.Embed(
                    title="A embed da mensagem foi removida!", description=inter.text_values["error_details"]
                ),
                view=None,
            )
            return

        image_url = inter.text_values["image_url"]

        if image_url and not URL_REG.match(image_url):
            await inter.send(
                embed=disnake.Embed(title="Link de imagem inválida!", description=inter.text_values["error_details"]),
                ephemeral=True,
            )
            return

        embed = disnake.Embed(
            color=self.bot.get_color(inter.guild.me),
            description=inter.text_values["error_details"],
            title="Report de erro",
        )

        embed.add_field(name="Log:", value=inter.message.embeds[0].description)

        await inter.response.edit_message(
            embed=disnake.Embed(
                description="**Erro reportado com sucesso!**", color=self.bot.get_color(inter.guild.me)
            ),
            view=None,
        )

        try:
            user_avatar = inter.author.avatar.with_static_format("png").url
        except AttributeError:
            user_avatar = inter.author.avatar.url

        embed.set_author(name=f"Erro reportado: {inter.author} - {inter.author.id}", icon_url=user_avatar)

        guild_txt = f"Servidor: {inter.guild.name} [{inter.guild.id}]"

        try:
            embed.set_footer(text=guild_txt, icon_url=inter.guild.icon.with_static_format("png").url)
        except AttributeError:
            embed.set_footer(text=guild_txt)

        if image_url:
            embed.set_image(url=image_url)

        await self.send_webhook(embed=embed)

    def build_report_embed(self, ctx):
        embed = disnake.Embed(title="Ocorreu um erro em um servidor:", timestamp=disnake.utils.utcnow())

        try:
            bot = ctx.bot
        except AttributeError:
            bot = self.bot

        if ctx.guild:
            embed.colour = bot.get_color(ctx.guild.me)
            embed.add_field(
                name="Servidor:",
                inline=False,
                value=f"```\n{_safe_text(ctx.guild.name)}\nID: {ctx.guild.id}```",
            )

            embed.add_field(
                name="Canal de texto:",
                inline=False,
                value=f"```\n{_safe_text(ctx.channel.name)}\nID: {ctx.channel.id}```",
            )

            if vc := ctx.author.voice:
                embed.add_field(
                    name="Canal de voz (user):",
                    inline=False,
                    value=f"```\n{_safe_text(vc.channel.name)}"
                    + (f" ({len(vc.channel.voice_states)}/{vc.channel.user_limit})" if vc.channel.user_limit else "")
                    + f"\nID: {vc.channel.id}```",
                )

            if vcbot := ctx.guild.me.voice:
                if vc and vcbot.channel != vc.channel:
                    embed.add_field(
                        name="Canal de voz (bot):",
                        inline=False,
                        value=f"```\n{_safe_text(vcbot.channel.name)}"
                        + (
                            f" ({len(vcbot.channel.voice_states)}/{vcbot.channel.user_limit})"
                            if vcbot.channel.user_limit
                            else ""
                        )
                        + f"\nID: {vcbot.channel.id}```",
                    )
            if ctx.guild.icon:
                embed.set_thumbnail(url=ctx.guild.icon.with_static_format("png").url)

        else:
            embed.colour = self.bot.get_color()
            try:
                guild_id = ctx.guild_id
            except AttributeError:
                guild_id = ctx.guild.id
            embed.add_field(name="Servidor [ID]:", inline=False, value=f"```\n{guild_id}```")

        embed.set_footer(
            text=f"{_safe_text(ctx.author)} [{ctx.author.id}]",
            icon_url=ctx.author.display_avatar.with_static_format("png").url,
        )

        try:
            embed.description = f"**Slash Command:**```\n{ctx.data.name}``` "

            if ctx.filled_options:
                # Resumo por tipo (V-L4): str(Attachment) é a URL assinada da CDN, str(Member) é o nome e o texto livre pode ter segredo.
                params = command_context(ctx, level=logging.ERROR)["params"]
                # As crases do texto não fecham o bloco de código.
                options = "\n".join(f"{k} -> {v}" for k, v in params.items())[:DESCRIPTION_TEXT_LIMIT].replace("`", "'")
                embed.description += "**Options**```\n" + options + "```"

        except AttributeError:
            if self.bot.intents.message_content and not ctx.author.bot:
                try:
                    message = ctx.message
                    type_ = "Comando"
                except AttributeError:
                    message = ctx
                    type_ = "Mensagem"

                # O texto do usuário passa pelo saneamento e pelo redator único; as crases não fecham o bloco de código.
                content = scrub(str(message.content), DESCRIPTION_TEXT_LIMIT).replace("`", "'")
                embed.description = f"**{type_}:**```\n{content}```"

        return embed

    async def send_webhook(
        self, content: str = None, embed: Optional[disnake.Embed] = None, file: Optional[disnake.File] = None
    ):
        if not self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"]:
            return

        kwargs = {
            "username": self.bot.user.name,
            "avatar_url": self.bot.user.display_avatar.replace(static_format="png").url,
            "allowed_mentions": disnake.AllowedMentions.none(),
        }

        if content:
            kwargs["content"] = content

        if embed:
            kwargs["embed"] = embed

        if file:
            kwargs["file"] = file

        async with ClientSession() as session:
            webhook = disnake.Webhook.from_url(self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"], session=session)
            await webhook.send(**kwargs)


def setup(bot: BotCore):
    bot.add_cog(ErrorHandler(bot))
