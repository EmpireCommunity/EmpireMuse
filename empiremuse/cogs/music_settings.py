# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import datetime
import os
import random
import string
import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Union, Optional

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.rules.errors import ROLE_DUPLICATE, RuleError
from empiremuse.rules.limits import (
    CUSTOM_SKIN_PREFIX,
    FORUM_TITLE_MAX,
    FORUM_TITLE_MIN,
    MAX_DJ_ROLES,
    VOLUMES,
)
from empiremuse.rules.servers import SettingError
from empiremuse.rules.settings import add_dj_role as add_role, allowed_names
from empiremuse.utils import branding
from empiremuse.utils.branding import asset_url
from empiremuse.utils.db import DBModel
from empiremuse.rules.text import format_mib
from empiremuse.utils.music.converters import time_format, md_link, md_code
from empiremuse.utils.music.errors import GenericError, NoVoice
from empiremuse.utils.music.interactions import SkinEditorMenu, EmbedPaginatorInteraction, ButtonInteraction
from empiremuse.utils.music.skin_store import load_custom_skins
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import (
    send_idle_embed,
    CustomContext,
    select_bot_pool,
    pool_command,
    CommandArgparse,
    update_inter,
)
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    is_dj,
)
from empiremuse.utils.music.jukebox_channel import (
    apply_jukebox_channel,
    build_channel_kwargs,
    check_channel_perm,  # noqa: F401
    create_jukebox_channel,
    ensure_can_create_channel,
    ensure_can_reset,
    find_current_jukebox_message,
    find_jukebox_channel,
    jukebox_reset as run_jukebox_reset,
    reset_jukebox_channel,  # noqa: F401
    validate_jukebox_target,  # noqa: F401
)
from empiremuse.utils.music.app_emojis import emoji as app_emoji

logger = logging.getLogger(__name__)

NODE_REGION_LIMIT = 32  # a região vem do dono ou da lista externa de servidores
SKIN_LINKS_PER_ROW = 4  # o parágrafo de ~0,7 mil caracteres formou os links no desktop; o de ~1,4 mil não (2.1.1)


def skin_link(skins: Mapping[str, Any], name: str) -> str:
    """Nome da skin como link embutido (só o nome clica), com a URL da prévia; sem URL válida sai só o nome."""
    return md_link(name, skins[name].preview, code=False)


def skin_links(skins: Mapping[str, Any], names: Iterable[str]) -> str:
    """Os links das skins em linhas curtas de até ``SKIN_LINKS_PER_ROW``, separados por " • "."""
    links = [skin_link(skins, name) for name in names]
    rows = (links[i : i + SKIN_LINKS_PER_ROW] for i in range(0, len(links), SKIN_LINKS_PER_ROW))
    return "\n".join(" • ".join(row) for row in rows)


def skin_selector_description(
    skins: Mapping[str, Any], names: Iterable[str], static_skins: Mapping[str, Any], static_names: Iterable[str]
) -> str:
    """Descrição da embed do seletor: títulos em negrito (o ```ansi sumia no fundo escuro) e as duas listas de links."""
    return (
        f"**Modo Normal:**\n{skin_links(skins, names)}\n\n"
        f"**Modo Fixo (Jukebox):**\n{skin_links(static_skins, static_names)}\n\n"
        "`Nota: No modo global a skin será aplicada globalmente em todos os bots.`"
    )


if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore


class SkinSelector(disnake.ui.View):
    def __init__(
        self,
        ctx: Union[disnake.ApplicationCommandInteraction, CustomContext],
        embed: disnake.Embed,
        select_opts: list,
        static_select_opts: list,
        global_select_opts: list = None,
        global_static_select_opts: list = None,
        global_mode=False,
    ):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.interaction: Optional[disnake.MessageInteraction] = None
        self.global_mode = global_mode
        self.skin_selected = None
        self.static_skin_selected = None
        self.select_opts = select_opts
        self.static_select_opts = static_select_opts
        self.global_select_opts = global_select_opts
        self.global_static_select_opts = global_static_select_opts
        self.embed = embed

        if not global_mode:
            self.skin_selected = [s.value for s in select_opts if s.default][0]
            self.static_skin_selected = [s.value for s in static_select_opts if s.default][0]
        else:
            try:
                self.skin_selected = [s.value for s in global_select_opts if s.default][0]
            except IndexError:
                self.skin_selected = self.ctx.bot.pool.default_skin
            try:
                self.static_skin_selected = [s.value for s in global_static_select_opts if s.default][0]
            except IndexError:
                self.static_skin_selected = self.ctx.bot.pool.default_static_skin

        self.rebuild_selects()

    def rebuild_selects(self):
        self.clear_items()

        if not self.global_mode:
            self.embed.title = "Seletor de skin (Aplicar no bot selecionado)"

            for s in self.select_opts:
                s.default = self.skin_selected == s.value

            for s in self.static_select_opts:
                s.default = self.static_skin_selected == s.value

            select_opts = self.select_opts
            static_select_opts = self.static_select_opts

        else:
            self.embed.title = "Seletor de skin (Aplicar em todos os bots do servidor)"

            for s in self.global_select_opts:
                s.default = self.skin_selected == s.value

            for s in self.global_static_select_opts:
                s.default = self.static_skin_selected == s.value

            select_opts = self.global_select_opts
            static_select_opts = self.global_static_select_opts

        select_opts = disnake.ui.Select(options=select_opts, min_values=1, max_values=1)
        select_opts.callback = self.skin_callback
        self.add_item(select_opts)

        static_select_opts = disnake.ui.Select(options=static_select_opts, min_values=1, max_values=1)
        static_select_opts.callback = self.static_skin_callback
        self.add_item(static_select_opts)

        global_mode = disnake.ui.Button(
            label=("Desativar" if self.global_mode else "Ativar") + " modo Global ", emoji="🌐"
        )
        global_mode.callback = self.mode_callback
        self.add_item(global_mode)

        confirm_button = disnake.ui.Button(label="Salvar", emoji="💾")
        confirm_button.callback = self.confirm_callback
        self.add_item(confirm_button)

        cancel_button = disnake.ui.Button(label="Cancelar", emoji="❌")
        cancel_button.callback = self.stop_callback
        self.add_item(cancel_button)

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.ctx.author.id:
            return True

        await inter.send(f"Apenas {self.ctx.author.mention} pode interagir aqui!", ephemeral=True)
        return False

    async def skin_callback(self, inter: disnake.MessageInteraction):
        self.skin_selected = inter.data.values[0]
        self.rebuild_selects()
        await inter.response.edit_message(view=self)

    async def static_skin_callback(self, inter: disnake.MessageInteraction):
        self.static_skin_selected = inter.data.values[0]
        self.rebuild_selects()
        await inter.response.edit_message(view=self)

    async def mode_callback(self, inter: disnake.MessageInteraction):
        self.global_mode = not self.global_mode
        self.rebuild_selects()
        await inter.response.edit_message(view=self, embed=self.embed)

    async def confirm_callback(self, inter: disnake.MessageInteraction):
        self.interaction = inter
        self.stop()

    async def stop_callback(self, inter: disnake.MessageInteraction):
        self.interaction = inter
        self.skin_selected = None
        self.stop()


class PlayerSettings(disnake.ui.View):
    def __init__(self, ctx: Union[disnake.ApplicationCommandInteraction, CustomContext], bot: BotCore, data: dict):
        super().__init__()
        self.ctx = ctx
        self.bot = bot
        self.enable_autoplay = data["autoplay"]
        self.check_other_bots_in_vc = data["check_other_bots_in_vc"]
        self.enable_restrict_mode = data["enable_restrict_mode"]
        self.default_player_volume = data["default_player_volume"]
        self._initial = self._current()  # o que a tela abriu mostrando: só o que o usuário mudar é gravado
        self.message: Optional[disnake.Message] = None
        self.load_buttons()

    def _current(self) -> dict:
        return {
            "autoplay": self.enable_autoplay,
            "check_other_bots_in_vc": self.check_other_bots_in_vc,
            "enable_restrict_mode": self.enable_restrict_mode,
            "default_player_volume": int(self.default_player_volume),
        }

    def load_buttons(self):
        self.clear_items()

        player_volume_select = disnake.ui.Select(
            placeholder="Selecione um volume padrão.",
            options=[
                disnake.SelectOption(
                    label=f"Volume padrão: {i}",
                    default=i == self.default_player_volume,
                    description="Nota: Acima de 100% o audio pode ficar ruim." if i > 100 else None,
                    value=str(i),
                )
                for i in VOLUMES
            ],
        )

        player_volume_select.callback = self.volume_callback
        self.add_item(player_volume_select)

        check_other_bots_button = disnake.ui.Button(
            label="Não conectar com bots incompatíveis.", emoji="✅" if self.check_other_bots_in_vc else "🚫"
        )
        check_other_bots_button.callback = self.check_other_bots_callback
        self.add_item(check_other_bots_button)

        restrict_mode_button = disnake.ui.Button(
            label="Modo restrito", emoji="✅" if self.enable_restrict_mode else "🚫"
        )
        restrict_mode_button.callback = self.restrict_mode_callback
        self.add_item(restrict_mode_button)

        check_autoplay_button = disnake.ui.Button(label="Autoplay.", emoji="✅" if self.enable_autoplay else "🚫")
        check_autoplay_button.callback = self.autoplay_callback
        self.add_item(check_autoplay_button)

        close_button = disnake.ui.Button(label="Salvar/Fechar", emoji="💾")
        close_button.callback = self.close_callback
        self.add_item(close_button)

    async def check_other_bots_callback(self, interaction: disnake.MessageInteraction):
        self.check_other_bots_in_vc = not self.check_other_bots_in_vc
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def restrict_mode_callback(self, interaction: disnake.MessageInteraction):
        self.enable_restrict_mode = not self.enable_restrict_mode
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def volume_callback(self, interaction: disnake.MessageInteraction):
        self.default_player_volume = int(interaction.data.values[0])
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def autoplay_callback(self, interaction: disnake.MessageInteraction):
        self.enable_autoplay = not self.enable_autoplay
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def close_callback(self, interaction: disnake.MessageInteraction):
        try:
            await interaction.response.defer()  # a gravação pode demorar mais que o prazo de resposta da interação
        except disnake.HTTPException as exc:
            logger.warning(
                "Falha ao adiar a resposta do menu de configurações (%s); a gravação continua", type(exc).__name__
            )
        await self.save_data()  # grava antes de dizer "salvo": se a gravação falhar, o usuário não vê sucesso
        try:
            if isinstance(self.ctx, CustomContext):
                await self.message.edit(content="Alterações salvas com sucesso!", view=None, embed=None)
            else:
                await self.ctx.edit_original_message(content="Alterações salvas com sucesso!", view=None, embed=None)
        except Exception:
            logger.exception("Falha ao salvar as alterações do menu de configurações")
        self.stop()

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.ctx.author.id:
            return True

        await inter.send(f"Apenas {self.ctx.author.mention} pode interagir aqui!", ephemeral=True)
        return False

    async def save_data(self):
        now = self._current()
        changes = {name: value for name, value in now.items() if value != self._initial[name]}
        if changes:  # só os campos que o usuário mexeu: o resto pode ter sido alterado por outro admin ou pelo painel
            await self.bot.pool.servers.patch(self.ctx.guild_id, "settings", changes)
            self._initial = now

        try:
            player: LavalinkPlayer = self.bot.music.players[self.ctx.guild_id]
        except KeyError:
            pass
        else:
            await player.process_save_queue()

    async def on_timeout(self):
        if isinstance(self.ctx, CustomContext):
            await self.message.edit(
                embed=disnake.Embed(description="**Tempo esgotado...**", color=self.bot.get_color()), view=None
            )
        else:
            await self.ctx.edit_original_message(
                embed=disnake.Embed(description="**Tempo esgotado...**", color=self.bot.get_color()), view=None
            )

        await self.save_data()

        self.stop()


class MusicSettings(commands.Cog):
    emoji = "🔧"
    name = "Configurações"
    desc_prefix = f"[{emoji} {name}] | "

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.invite_cooldown = commands.CooldownMapping.from_cooldown(rate=1, per=45, type=commands.BucketType.guild)

    player_settings_cd = commands.CooldownMapping.from_cooldown(1, 5, commands.BucketType.guild)
    player_settings_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(
        name="playersettings",
        aliases=["ps", "settings"],
        description="Alterar algumas configurações padrões do player.",
        cooldown=player_settings_cd,
        max_concurrency=player_settings_mc,
    )
    async def player_settings_legacy(self, ctx: CustomContext):
        await self.player_settings.callback(self=self, interaction=ctx)

    @commands.slash_command(
        name=localized("cmd.player_settings.name"),
        description=localized("cmd.player_settings.description"),
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    @commands.contexts(guild=True)
    async def player_settings(self, interaction: disnake.ApplicationCommandInteraction):
        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        await inter.response.defer(ephemeral=True)

        guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        try:
            func = inter.store_message.edit
        except AttributeError:
            try:
                func = inter.edit_original_message
            except AttributeError:
                func = inter.send

        view = PlayerSettings(inter, bot, guild_data)

        view.message = await func(
            embed=disnake.Embed(
                description="**Ajustar configurações padrão do player:**", color=self.bot.get_color()
            ).set_author(name=str(bot.user), icon_url=bot.user.display_avatar.url),
            view=view,
        )

        await view.wait()

    setup_cd = commands.CooldownMapping.from_cooldown(1, 20, commands.BucketType.guild)
    setup_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)
    jukebox_thread_cd = commands.CooldownMapping.from_cooldown(1, 120, commands.BucketType.guild)

    @commands.group(
        name="jukebox",
        aliases=["jb"],
        description="Comandos da Jukebox do servidor.",
        invoke_without_command=True,
    )
    async def jukebox_group(self, ctx: CustomContext):
        tr = await self.bot.pool.i18n.for_guild(ctx.guild)
        embed = disnake.Embed(
            title="🎶 Jukebox",
            description=(
                f"`{ctx.prefix}jukebox setup` (`configurar`) — {tr('cmd.jukebox.setup.description')}\n"
                f"`{ctx.prefix}jukebox reset` (`redefinir`) — {tr('cmd.jukebox.reset.description')}\n"
                f"`{ctx.prefix}jukebox thread` (`conversa`) — {tr('cmd.jukebox.thread.description')}"
            ),
            color=self.bot.get_color(ctx.guild.me if ctx.guild else None),
        )
        await ctx.send(embed=embed)

    @commands.has_guild_permissions(manage_guild=True)
    @jukebox_group.command(
        name="setup",
        aliases=["configurar"],
        usage="{prefix}jukebox setup [id|#canal]\nEx: {prefix}jukebox setup #canal",
        description="Configurar o canal da Jukebox do servidor.",
        cooldown=setup_cd,
        max_concurrency=setup_mc,
    )
    async def jukebox_setup_legacy(
        self,
        ctx: CustomContext,
        channel: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.ForumChannel, None] = None,
    ):
        await self.jukebox_setup.callback(
            self=self, interaction=ctx, target=channel, auto_delete=None, text_requests=None
        )

    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_threads=True)
    @jukebox_group.command(
        name="reset",
        aliases=["redefinir"],
        description="Desativar a Jukebox do servidor, apagar o painel e voltar as opções ao padrão.",
        cooldown=setup_cd,
        max_concurrency=setup_mc,
    )
    async def jukebox_reset_legacy(self, ctx: CustomContext):
        await self.jukebox_reset.callback(self=self, interaction=ctx)

    @is_dj()
    @has_player()
    @check_voice()
    @jukebox_group.command(
        name="thread",
        aliases=["conversa"],
        description="Abrir uma conversa temporária na mensagem do player para pedir músicas por texto.",
        extras={"only_voiced": True},
        cooldown=jukebox_thread_cd,
    )
    async def jukebox_thread_legacy(self, ctx: CustomContext):
        await self.jukebox_thread.callback(self=self, inter=ctx)

    @commands.slash_command(
        name=localized("cmd.jukebox.name"),
        description=localized("cmd.jukebox.description"),
    )
    @commands.contexts(guild=True)
    async def jukebox(self, interaction: disnake.ApplicationCommandInteraction):
        pass

    @commands.has_guild_permissions(manage_guild=True)
    @jukebox.sub_command(
        name=localized("cmd.jukebox.setup.name"),
        description=localized("cmd.jukebox.setup.description"),
        cooldown=setup_cd,
        max_concurrency=setup_mc,
    )
    async def jukebox_setup(
        self,
        interaction: disnake.ApplicationCommandInteraction,
        target: Union[
            disnake.TextChannel, disnake.VoiceChannel, disnake.ForumChannel, disnake.StageChannel
        ] = commands.Param(
            name=localized("cmd.jukebox.setup.opt.channel.name"),
            default=None,
            description=localized("cmd.jukebox.setup.opt.channel.description"),
        ),
        auto_delete: Optional[bool] = commands.Param(
            name=localized("cmd.jukebox.setup.opt.auto_delete.name"),
            default=None,
            description=localized("cmd.jukebox.setup.opt.auto_delete.description"),
        ),
        text_requests: Optional[bool] = commands.Param(
            name=localized("cmd.jukebox.setup.opt.text_requests.name"),
            default=None,
            description=localized("cmd.jukebox.setup.opt.text_requests.description"),
        ),
    ):
        await self._run_jukebox_setup(interaction, target=target, auto_delete=auto_delete, text_requests=text_requests)

    async def _run_jukebox_setup(
        self,
        interaction: disnake.ApplicationCommandInteraction,
        *,
        target: Any = None,
        auto_delete: Optional[bool] = None,
        text_requests: Optional[bool] = None,
        purge_messages: str = "yes",
    ):
        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        # a Jukebox é única: quem cria o canal e escreve nele é o bot do painel (o principal, senão a 1ª reserva presente)
        bot = (await bot.pool.servers.jukebox.panel_bot(inter.guild_id)) or bot

        guild = bot.get_guild(inter.guild_id)

        channel = bot.get_channel(inter.channel.id)

        if target and bot != self.bot:
            target = bot.get_channel(target.id)

        channel_name = f"{bot.user.name} Jukebox"

        if isinstance(target, disnake.ForumChannel) and not isinstance(inter, CustomContext):
            await inter.response.send_modal(
                title="Escolha um nome para o post (em até 30seg.)",
                custom_id=str(inter.id),
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Nome",
                        custom_id="forum_title",
                        min_length=FORUM_TITLE_MIN,
                        max_length=FORUM_TITLE_MAX,
                        value=channel_name[:FORUM_TITLE_MAX],
                        required=True,
                    )
                ],
            )

            try:
                modal_inter: disnake.ModalInteraction = await inter.bot.wait_for(
                    "modal_submit", timeout=30, check=lambda i: i.data.custom_id == str(inter.id)
                )
            except asyncio.TimeoutError:
                if isinstance(inter, disnake.MessageInteraction):
                    try:
                        await inter.delete_original_message()
                    except Exception:
                        pass
                return

            if isinstance(inter, disnake.MessageInteraction):
                try:
                    await inter.delete_original_message()
                except Exception:
                    pass

            update_inter(interaction, modal_inter)
            inter = modal_inter
            channel_name = inter.text_values["forum_title"]

        channel_kwargs = build_channel_kwargs(guild)

        await inter.response.defer(ephemeral=True)

        config = await bot.pool.servers.jukebox.get(inter.guild_id)

        existing_channel = True

        player, original_message = await find_current_jukebox_message(bot, guild, config)

        if not target:
            try:
                id_ = inter.id
            except AttributeError:
                id_ = ""

            kwargs_msg = {}
            try:
                func = inter.edit_original_message
            except Exception:
                try:
                    func = inter.store_message.edit
                except Exception:
                    try:
                        func = inter.response.edit_message
                    except Exception:
                        func = inter.send
                        kwargs_msg = {"ephemeral": True}

            buttons = [
                disnake.ui.Button(
                    label="Criar canal de texto",
                    custom_id=f"text_channel_{id_}",
                    emoji="💬",
                    disabled=not guild.me.guild_permissions.manage_channels,
                ),
                disnake.ui.Button(
                    label="Criar canal de voz",
                    custom_id=f"voice_channel_{id_}",
                    emoji="🔊",
                    disabled=not guild.me.guild_permissions.manage_channels,
                ),
                disnake.ui.Button(label="Cancelar", custom_id=f"voice_channel_cancel_{id_}", emoji="❌"),
            ]

            if "COMMUNITY" in guild.features:
                buttons.insert(
                    2,
                    disnake.ui.Button(
                        label="Criar canal de palco",
                        custom_id=f"stage_channel_{id_}",
                        emoji=app_emoji(interaction.bot, "stagechannel"),
                        disabled=not guild.me.guild_permissions.manage_channels,
                    ),
                )

            color = self.bot.get_color(guild.me)

            embeds = [
                disnake.Embed(
                    description="**Selecione um canal "
                    + (
                        "ou clique em um dos botões abaixo para criar um novo canal para a Jukebox."
                        if guild.me.guild_permissions.manage_channels
                        else "abaixo:"
                    )
                    + "**",
                    color=color,
                ).set_footer(text="Você tem apenas 45 segundos para selecionar/clicar em uma opção.")
            ]

            if not guild.me.guild_permissions.manage_channels:
                embeds.append(
                    disnake.Embed(
                        description=f"Os botões de criar canal foram desativados devido o bot **{bot.user.mention}** "
                        "não possuir a permissão de **gerenciar canais** no servidor.",
                        color=color,
                    )
                )

            disnake.Embed(color=color).set_footer(
                text="Nota: Caso queira usar canal de forum você terá que selecionar um na lista de canais "
                "abaixo (Caso não tenha você terá que criar um canal de fórum manualmente e usar esse "
                "comando novamente."
            )

            msg_select = await func(
                embeds=embeds,
                components=[
                    disnake.ui.ChannelSelect(
                        custom_id=f"existing_channel_{id_}",
                        min_values=1,
                        max_values=1,
                        channel_types=[
                            disnake.ChannelType.text,
                            disnake.ChannelType.voice,
                            disnake.ChannelType.stage_voice,
                            disnake.ChannelType.forum,
                        ],
                    ),
                ]
                + buttons,
                **kwargs_msg,
            )

            if isinstance(inter, CustomContext):
                bot_inter = bot
                check = lambda i: i.message.id == msg_select.id and i.author.id == inter.author.id
            else:
                bot_inter = inter.bot
                check = lambda i: i.data.custom_id.endswith(f"_{id_}") and i.author.id == inter.author.id

            done, pending = await asyncio.wait(
                [
                    bot_inter.loop.create_task(bot_inter.wait_for("button_click", check=check)),
                    bot_inter.loop.create_task(bot_inter.wait_for("dropdown", check=check)),
                ],
                timeout=45,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for future in pending:
                future.cancel()

            if not done:
                try:
                    inter.application_command.reset_cooldown(inter)
                except AttributeError:
                    try:
                        inter.command.reset_cooldown(inter)
                    except Exception:
                        pass

                if msg_select:
                    func = msg_select.edit
                else:
                    try:
                        func = (await inter.original_message()).edit
                    except Exception:
                        func = inter.message.edit

                try:
                    await func(
                        embed=disnake.Embed(description="**Tempo esgotado!**", color=disnake.Color.red()),
                        components=None,
                    )
                except disnake.NotFound:
                    pass
                except Exception:
                    logger.exception("Falha inesperada no menu de configurações")

                return

            inter_message = done.pop().result()

            update_inter(interaction, inter_message)

            if inter_message.data.custom_id.startswith("voice_channel_cancel"):
                await inter_message.response.edit_message(
                    embed=disnake.Embed(
                        description="**Operação cancelada...**",
                        color=self.bot.get_color(guild.me),
                    ),
                    components=None,
                )
                return

            if channel.category and channel.category.permissions_for(guild.me).send_messages:
                target = channel.category
            else:
                target = guild

            if inter_message.data.custom_id.startswith("existing_channel_"):
                target = bot.get_channel(int(inter_message.data.values[0]))
                existing_channel = True

            else:
                ensure_can_create_channel(bot, guild)

                await inter_message.response.defer()
                if inter_message.data.custom_id.startswith("voice_channel_"):
                    kind = "voice"
                elif inter_message.data.custom_id.startswith("stage_channel_"):
                    kind = "stage"
                else:
                    kind = "text"
                target = await create_jukebox_channel(bot, target, kind, channel_kwargs)

                existing_channel = False

            inter = inter_message

        async def ask_forum_title(current_title):
            nonlocal inter

            try:
                id_ = f"modal_{inter.id}"
            except AttributeError:
                id_ = f"modal_{inter.message.id}"

            if not inter.response.is_done():
                await inter.response.send_modal(
                    title="Definir um nome para o post do fórum",
                    custom_id=id_,
                    components=[
                        disnake.ui.TextInput(
                            style=disnake.TextInputStyle.short,
                            label="Nome",
                            custom_id="forum_title",
                            min_length=FORUM_TITLE_MIN,
                            max_length=FORUM_TITLE_MAX,
                            value=channel_name[:FORUM_TITLE_MAX],
                            required=True,
                        )
                    ],
                )

                try:
                    modal_inter: disnake.ModalInteraction = await inter.bot.wait_for(
                        "modal_submit", timeout=30, check=lambda i: i.custom_id == id_
                    )
                except asyncio.TimeoutError:
                    try:
                        func = inter.edit_original_message
                    except AttributeError:
                        func = msg_select.edit
                    await func(
                        embed=disnake.Embed(description="### Tempo esgotado!", color=bot.get_color(guild.me)), view=None
                    )
                    return None

                try:
                    await msg_select.delete()
                except Exception:
                    pass

                update_inter(interaction, modal_inter)
                inter = modal_inter

                await inter.response.defer()

                return inter.text_values["forum_title"]

            return current_title

        result = await apply_jukebox_channel(
            bot,
            guild,
            inter.author,
            target,
            existing_channel=existing_channel,
            purge=purge_messages,
            channel_kwargs=channel_kwargs,
            channel_name=channel_name,
            original_message=original_message,
            player=player,
            config=config,
            ask_forum_title=ask_forum_title,
            send_idle_embed=send_idle_embed,
        )

        if not result:
            return

        option_changes = {}
        if auto_delete is not None:
            option_changes["jukebox_auto_delete"] = bool(auto_delete)
        if text_requests is not None:
            option_changes["jukebox_text_requests"] = bool(text_requests)
        if option_changes and hasattr(bot.pool.servers, "patch"):
            await bot.pool.servers.patch(inter.guild_id, "settings", option_changes)

        channel = result.channel

        msg = f"{inter.author.mention}, a Jukebox foi configurada no canal <#{channel.id}> através do bot: {bot.user.mention}"

        reset_txt = f"{inter.prefix}jukebox reset" if isinstance(inter, CustomContext) else "/jukebox reset"

        embed = disnake.Embed(
            description=f"**{msg}**\n\nObs: Caso queira reverter essa configuração, apenas use o comando {reset_txt} ou "
            f"delete o canal/post {channel.mention}",
            color=bot.get_color(guild.me),
        )

        if not inter.response.is_done():
            try:
                await inter.response.edit_message(embed=embed, components=None)
            except AttributeError:
                await inter.send(embed=embed)
        try:
            await inter.edit_original_message(embed=embed, components=None)
        except AttributeError, disnake.InteractionNotEditable:
            try:
                await inter.response.edit_message(embed=embed, components=None)
            except Exception:
                await inter.send(embed=embed, ephemeral=True)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_threads=True)
    @jukebox.sub_command(
        name=localized("cmd.jukebox.reset.name"),
        description=localized("cmd.jukebox.reset.description"),
        cooldown=setup_cd,
        max_concurrency=setup_mc,
    )
    async def jukebox_reset(
        self,
        interaction: disnake.ApplicationCommandInteraction,
    ):
        await self._run_jukebox_reset(interaction)

    async def _run_jukebox_reset(
        self,
        interaction: disnake.ApplicationCommandInteraction,
        delete_channel: Optional[str] = None,
    ):
        inter, bot = await select_bot_pool(interaction)

        if not bot:
            return

        bot = (
            await bot.pool.servers.jukebox.panel_bot(inter.guild_id)
        ) or bot  # o canal é único: quem reseta é o bot do painel

        await inter.response.defer(ephemeral=True)

        guild = bot.get_guild(inter.guild_id) or inter.guild

        ensure_can_reset(guild)

        channel_inter = bot.get_channel(inter.channel.id)

        config = await bot.pool.servers.jukebox.get(inter.guild_id)

        channel = await find_jukebox_channel(bot, inter.guild_id, config)

        async def notify(event: str):
            if event == "forum_deleted":
                if channel_inter != channel:
                    await inter.edit_original_message("O post foi deletado com sucesso!", embed=None, components=None)
                return

            try:
                func = inter.edit_original_message
            except AttributeError:
                try:
                    func = inter.response.edit_message
                except AttributeError:
                    try:
                        func = inter.store_message.edit
                    except AttributeError:
                        func = inter.send

            await func(
                embed=disnake.Embed(
                    color=self.bot.get_color(guild.me),
                    description="**A Jukebox foi redefinida com sucesso.**",
                ),
                components=[],
            )

        await run_jukebox_reset(
            bot,
            guild,
            inter.author,
            channel,
            delete_channel=delete_channel,
            channel_inter=channel_inter,
            config=config,
            notify=notify,
        )

    @is_dj()
    @has_player()
    @check_voice()
    @jukebox.sub_command(
        name=localized("cmd.jukebox.thread.name"),
        description=localized("cmd.jukebox.thread.description"),
        extras={"only_voiced": True},
        cooldown=jukebox_thread_cd,
    )
    async def jukebox_thread(self, inter: disnake.ApplicationCommandInteraction):
        music_cog = self.bot.get_cog("Music")
        if music_cog:
            await music_cog.create_jukebox_thread(inter)

    djrole_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.guild)
    djrole_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(
        name="adddjrole",
        description="Adicionar um cargo para a lista de DJ's do servidor.",
        usage="{prefix}{cmd} [id|nome|@cargo]\nEx: {prefix}{cmd} @cargo",
        cooldown=djrole_cd,
        max_concurrency=djrole_mc,
    )
    async def add_dj_role_legacy(self, ctx: CustomContext, *, role: disnake.Role):
        await self.add_dj_role.callback(self=self, interaction=ctx, role=role)

    @commands.slash_command(
        name=localized("cmd.add_dj_role.name"),
        description=localized("cmd.add_dj_role.description"),
        default_member_permissions=disnake.Permissions(manage_guild=True),
        cooldown=djrole_cd,
        max_concurrency=djrole_mc,
    )
    @commands.contexts(guild=True)
    async def add_dj_role(
        self,
        interaction: disnake.ApplicationCommandInteraction,
        role: disnake.Role = commands.Param(
            name=localized("cmd.add_dj_role.opt.role.name"),
            description=localized("cmd.add_dj_role.opt.role.description"),
        ),
    ):
        inter, bot = await select_bot_pool(interaction)
        guild = bot.get_guild(inter.guild_id) or inter.guild
        role = guild.get_role(role.id)

        if role == guild.default_role:
            await inter.send("Você não pode adicionar esse cargo.", ephemeral=True)
            return

        guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        roles = add_role(guild_data["djroles"], str(role.id))

        if isinstance(roles, RuleError):
            if roles.code == ROLE_DUPLICATE:
                await inter.send(f"O cargo {role.mention} já está na lista de DJ's", ephemeral=True)
            else:
                await inter.send(
                    f"A lista de DJ's já tem o máximo de {MAX_DJ_ROLES} cargos; remova algum para adicionar outro.",
                    ephemeral=True,
                )
            return

        try:
            await bot.pool.servers.add_djrole(guild.id, role.id)  # a lista é alterada dentro da trava do servidor
        except SettingError:  # entre a checagem e a gravação a lista encheu ou o cargo foi apagado
            await inter.send(
                f"Não foi possível adicionar o cargo: a lista de DJ's pode estar com o máximo de {MAX_DJ_ROLES} cargos "
                "ou o cargo não existe mais.",
                ephemeral=True,
            )
            return

        await inter.send(f"O cargo {role.mention} foi adicionado à lista de DJ's.", ephemeral=True)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(
        name="removedjrole",
        description="Remover um cargo da lista de DJ's do servidor.",
        usage="{prefix}{cmd} [id|nome|@cargo]\nEx: {prefix}{cmd} @cargo",
        cooldown=djrole_cd,
        max_concurrency=djrole_mc,
    )
    async def remove_dj_role_legacy(self, ctx: CustomContext, *, role: disnake.Role):
        await self.remove_dj_role.callback(self=self, interaction=ctx, role=role)

    @commands.slash_command(
        name=localized("cmd.remove_dj_role.name"),
        description=localized("cmd.remove_dj_role.description"),
        default_member_permissions=disnake.Permissions(manage_guild=True),
        cooldown=djrole_cd,
        max_concurrency=djrole_mc,
    )
    @commands.contexts(guild=True)
    async def remove_dj_role(
        self,
        interaction: disnake.ApplicationCommandInteraction,
        role: disnake.Role = commands.Param(
            name=localized("cmd.remove_dj_role.opt.role.name"),
            description=localized("cmd.remove_dj_role.opt.role.description"),
        ),
    ):
        inter, bot = await select_bot_pool(interaction)

        if not bot:
            return

        guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        if not guild_data["djroles"]:
            await inter.send("Não há cargos na lista de DJ's.", ephemeral=True)
            return

        guild = bot.get_guild(inter.guild_id) or inter.guild
        role = guild.get_role(role.id)

        if str(role.id) not in guild_data["djroles"]:
            await inter.send(
                f"O cargo {role.mention} não está na lista de DJ's\n\n"
                + "Cargos:\n"
                + " ".join(f"<@&{r}>" for r in guild_data["djroles"]),
                ephemeral=True,
            )
            return

        await bot.pool.servers.remove_djrole(guild.id, role.id)

        await inter.send(f"O cargo {role.mention} foi removido da lista de DJ's.", ephemeral=True)

    skin_cd = commands.CooldownMapping.from_cooldown(1, 20, commands.BucketType.guild)
    skin_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(
        description="Alterar aparência/skin do player.",
        name="changeskin",
        aliases=["skin", "skins"],
        cooldown=skin_cd,
        max_concurrency=skin_mc,
    )
    async def change_skin_legacy(self, ctx: CustomContext):
        await self.change_skin.callback(self=self, interaction=ctx)

    @commands.slash_command(
        name=localized("cmd.change_skin.name"),
        description=localized("cmd.change_skin.description"),
        cooldown=skin_cd,
        max_concurrency=skin_mc,
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    @commands.contexts(guild=True)
    async def change_skin(self, interaction: disnake.ApplicationCommandInteraction):
        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        skin_list = list(allowed_names(bot.player_skins, bot.config["IGNORE_SKINS"]))
        static_skin_list = list(allowed_names(bot.player_static_skins, bot.config["IGNORE_STATIC_SKINS"]))

        await inter.response.defer(ephemeral=True)

        guild = bot.get_guild(inter.guild_id) or inter.guild

        add_skin_prefix = lambda d: [f"{CUSTOM_SKIN_PREFIX}{i}" for i in d.keys()]

        guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        global_data = await bot.get_global_data(guild.id, db_name=DBModel.guilds)

        global_mode = global_data["global_skin"]

        selected = guild_data["player_controller"]["skin"] or bot.pool.default_skin
        static_selected = guild_data["player_controller"]["static_skin"] or bot.pool.default_static_skin

        global_selected = global_data["player_skin"] or bot.pool.default_skin
        global_static_selected = global_data["player_skin_static"] or bot.pool.default_static_skin

        skins_opts = [
            disnake.SelectOption(
                emoji="💠" if s.startswith(CUSTOM_SKIN_PREFIX) else "🎨",
                label=f"Modo normal: {s.replace(CUSTOM_SKIN_PREFIX, '')}",
                value=s,
                **{"default": True, "description": "skin atual"} if selected == s else {},
            )
            for s in skin_list + add_skin_prefix(global_data["custom_skins"])
        ]
        static_skins_opts = [
            disnake.SelectOption(
                emoji="💠" if s.startswith(CUSTOM_SKIN_PREFIX) else "🎨",
                label=f"Jukebox: {s.replace(CUSTOM_SKIN_PREFIX, '')}",
                value=s,
                **{"default": True, "description": "skin atual"} if static_selected == s else {},
            )
            for s in static_skin_list + add_skin_prefix(global_data["custom_skins_static"])
        ]

        global_skins_opts = [
            disnake.SelectOption(
                emoji="💠" if s.startswith(CUSTOM_SKIN_PREFIX) else "🎨",
                label=f"Modo Normal: {s.replace(CUSTOM_SKIN_PREFIX, '')}",
                value=s,
                **{"default": True, "description": "skin atual"} if global_selected == s else {},
            )
            for s in skin_list + add_skin_prefix(global_data["custom_skins"])
        ]
        global_static_skins_opts = [
            disnake.SelectOption(
                emoji="💠" if s.startswith(CUSTOM_SKIN_PREFIX) else "🎨",
                label=f"Jukebox: {s.replace(CUSTOM_SKIN_PREFIX, '')}",
                value=s,
                **{"default": True, "description": "skin atual"} if global_static_selected == s else {},
            )
            for s in static_skin_list + add_skin_prefix(global_data["custom_skins_static"])
        ]

        embed = disnake.Embed(
            description=skin_selector_description(
                bot.player_skins, skin_list, bot.player_static_skins, static_skin_list
            ),
            colour=bot.get_color(guild.me),
        ).set_image(asset_url("ui_rainbow_bar_2.gif"))

        try:
            if bot.user.id != self.bot.user.id:
                embed.set_footer(text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
        except AttributeError:
            pass

        select_view = SkinSelector(
            inter, embed, skins_opts, static_skins_opts, global_skins_opts, global_static_skins_opts, global_mode
        )

        try:
            func = inter.store_message.edit
        except Exception:
            try:
                func = inter.edit_original_message
            except AttributeError:
                func = inter.send

        msg = await func(embed=embed, view=select_view)

        await select_view.wait()

        if select_view.skin_selected is None:
            await select_view.interaction.response.edit_message(
                view=None, embed=disnake.Embed(description="**Solicitação cancelada.**", colour=bot.get_color(guild.me))
            )
            return

        if not select_view.interaction:
            try:
                msg = await inter.original_message()
            except AttributeError:
                pass
            for c in select_view.children:
                c.disabled = True
            await msg.edit(view=select_view)
            return

        update_inter(interaction, select_view.interaction)

        inter = select_view.interaction

        await inter.response.defer()

        try:
            global_data.update({"global_skin": select_view.global_mode})
        except Exception:
            pass

        changed_skins_txt = ""

        if select_view.global_mode:
            try:
                global_data.update(
                    {"player_skin": select_view.skin_selected, "player_skin_static": select_view.static_skin_selected}
                )
            except Exception:
                pass
            else:
                await bot.update_global_data(inter.guild_id, global_data, db_name=DBModel.guilds)

            if global_selected != select_view.skin_selected:
                try:
                    changed_skins_txt += (
                        f"Global - Modo Normal: {skin_link(self.bot.player_skins, select_view.skin_selected)}\n"
                    )
                except Exception:
                    changed_skins_txt += f"Global - Modo Normal: `{select_view.skin_selected.replace(CUSTOM_SKIN_PREFIX, '[custom skin]: ')}`\n"

            if global_static_selected != select_view.static_skin_selected:
                try:
                    changed_skins_txt += f"Global - Jukebox: {skin_link(self.bot.player_static_skins, select_view.static_skin_selected)}\n"
                except Exception:
                    changed_skins_txt += f"Global - Jukebox: `{select_view.static_skin_selected.replace(CUSTOM_SKIN_PREFIX, '[custom skin]: ')}`\n"

        else:
            # só o que o usuário trocou: o outro campo pode ter sido alterado por outro admin enquanto a tela estava aberta
            skin_changes = {}
            if selected != select_view.skin_selected:
                skin_changes["skin"] = select_view.skin_selected
            if static_selected != select_view.static_skin_selected:
                skin_changes["static_skin"] = select_view.static_skin_selected
            if skin_changes:
                await bot.pool.servers.patch(inter.guild_id, "settings", skin_changes)

            if selected != select_view.skin_selected:
                try:
                    changed_skins_txt += f"Modo Normal: {skin_link(self.bot.player_skins, select_view.skin_selected)}\n"
                except Exception:
                    changed_skins_txt += (
                        f"Modo Normal: `{select_view.skin_selected.replace(CUSTOM_SKIN_PREFIX, '[custom skin]: ')}`\n"
                    )

            if static_selected != select_view.static_skin_selected:
                try:
                    changed_skins_txt += (
                        f"Jukebox: {skin_link(self.bot.player_static_skins, select_view.static_skin_selected)}\n"
                    )
                except Exception:
                    changed_skins_txt += f"Jukebox: `{select_view.static_skin_selected.replace(CUSTOM_SKIN_PREFIX, '[custom skin]: ')}`\n"

        if global_mode != select_view.global_mode:
            changed_skins_txt += "Skin Global: `" + ("Ativado" if select_view.global_mode else "Desativado") + "`\n"

        if not changed_skins_txt:
            txt = "**Não houve alterações nas configurações de skin...**"
        else:
            txt = f"**A skin do player do servidor foi alterada com sucesso.**\n{changed_skins_txt}"

        skins = await load_custom_skins(bot, guild.id, global_data)

        if skins.rejected:
            txt += (
                f"\n\n⚠️ {skins.rejected} skin(s) personalizada(s) do servidor estão em um formato "
                "incompatível e foram ignoradas (o player usa a skin padrão no lugar)."
            )

        kwargs = {
            "embed": disnake.Embed(description=txt, color=bot.get_color(guild.me)).set_footer(
                text=f"{bot.user} - [{bot.user.id}]", icon_url=bot.user.display_avatar.with_format("png").url
            )
        }

        if isinstance(inter, CustomContext):
            await msg.edit(view=None, **kwargs)
        elif inter.response.is_done():
            await inter.edit_original_message(view=None, **kwargs)
        else:
            await inter.send(ephemeral=True, **kwargs)

        for b in self.bot.pool.get_guild_bots(interaction.guild_id):
            try:
                player: LavalinkPlayer = b.music.players[inter.guild_id]
            except KeyError:
                continue

            last_skin = str(player.skin)
            last_static_skin = str(player.skin_static)

            if player.static:
                if select_view.static_skin_selected == last_static_skin:
                    continue

            elif select_view.skin_selected == last_skin:
                continue

            try:
                await player.destroy_message()
            except Exception:
                logger.exception("Falha ao apagar a mensagem do player")

            player.skin = select_view.skin_selected
            player.skin_static = select_view.static_skin_selected

            player.custom_skin_data = skins.normal
            player.custom_skin_static_data = skins.static
            player.setup_features()

            player.setup_hints()
            player.process_hint()
            player.set_command_log(text=f"{inter.author.mention} alterou a skin do player.", emoji="🎨")

            try:
                if player.controller_mode and not [m for m in player.guild.me.voice.channel.members if not m.bot]:
                    player.start_auto_skip()
            except Exception:
                logger.exception("Falha ao iniciar o auto-skip")

            await player.invoke_np(force=True)
            await asyncio.sleep(1.5)

    @commands.cooldown(2, 10, commands.BucketType.member)
    @commands.has_guild_permissions(manage_channels=True)
    @pool_command(
        aliases=["la"], description="Ativar o envio de invite para ouvir junto via RPC (Sistema ainda em testes)"
    )
    async def listenalong(self, ctx: CustomContext):
        try:
            _bot = ctx.music_bot
        except AttributeError:
            _bot = ctx.bot

        # if not guild.me.guild_permissions.create_instant_invite:
        #    raise GenericError(f"**{bot.user.mention} não possui permissão de criar convites instantâneos...**")

        if not ctx.author.voice:
            raise NoVoice()

        await ctx.reply(
            embed=disnake.Embed(
                description=f"**Crie um convite no canal {ctx.author.voice.channel.mention} marcando a opção "
                f'"Inscrição como convidado" e em seguida clique no botão abaixo para enviar o link do '
                f"convite.**\n\n"
                f"Cuidado! Caso não tenha essa opção significa que o recurso não está disponível no seu "
                f"servidor e não recomendo prosseguir pra evitar dar acesso permanente ao membro que usar "
                f"o botão ou evitar problemas de permissões etc."
            )
            .set_image(url=asset_url("tutorial_guest_invite_guide.png"))
            .set_footer(
                text="Nota: crie um convite sem limitações como: datas para expirar, quantidade de usos ou "
                "apenas para um usuário usar."
            ),
            components=[disnake.ui.Button(label="Enviar convite", custom_id=f"listen_along_{ctx.author.id}")],
            fail_if_not_exists=False,
        )

    @commands.Cog.listener("on_button_click")
    async def send_listen_along_invite(self, inter: disnake.MessageInteraction):
        if not inter.data.custom_id.startswith("listen_along_"):
            return

        if not inter.data.custom_id.endswith(str(inter.author.id)):
            return await inter.send("**Você não pode usar este botão.**", ephemeral=True)

        if not inter.author.voice.channel:
            return await inter.send("**Você precisa estar em um canal de voz para enviar o convite.**", ephemeral=True)

        await inter.response.send_modal(
            title="Invite para ouvir junto",
            custom_id="listen_along_modal",
            components=[
                disnake.ui.TextInput(
                    style=disnake.TextInputStyle.short,
                    label="Cole o invite no campo abaixo:",
                    custom_id="invite_url",
                    min_length=25,
                    max_length=36,
                    required=True,
                ),
            ],
        )

    @commands.Cog.listener("on_modal_submit")
    async def listen_along_modal(self, inter: disnake.ModalInteraction):
        if inter.data.custom_id != "listen_along_modal":
            return

        if not inter.author.voice.channel:
            return await inter.send("**Você precisa estar em um canal de voz para enviar o convite.**", ephemeral=True)

        bucket = self.invite_cooldown.get_bucket(inter)
        retry_after = bucket.update_rate_limit()

        if retry_after:
            return await inter.send(
                "**Você deve aguardar {} para enviar o convite**".format(
                    time_format(int(retry_after) * 1000, use_names=True)
                ),
                ephemeral=True,
            )

        await inter.response.defer(ephemeral=True)

        try:
            invite = await self.bot.fetch_invite(inter.text_values["invite_url"].strip(), with_expiration=True)
        except disnake.NotFound:
            return await inter.edit_original_message("Link inválido ou o convite não existe/expirou")

        if invite.max_uses:
            return await inter.edit_original_message("O convite pode ter quantidade máxima de usos")

        if invite.target_user:
            return await inter.edit_original_message("O convite não pode ser configurado para apenas 1 usuário usar.")

        channel = None

        for bot in self.bot.pool.get_guild_bots(inter.guild_id):
            channel = bot.get_channel(invite.channel.id)

            if not channel:
                continue

            if not isinstance(channel, disnake.VoiceChannel):
                return await inter.edit_original_message("**Esse recurso funciona apenas em canais de voz.**")

            break

        if not channel:
            return await inter.edit_original_message(
                "**Não há bots compatíveis adicionado no servidor do invite informado.**"
            )

        global_data = await self.bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)

        if len(global_data["listen_along_invites"]) > 4:
            return await inter.edit_original_message(
                embed=disnake.Embed(
                    description="**Limite de convites excedido no servidor atual, delete pelo menos um dos convites "
                    "abaixo do servidor:** ```ansi\n"
                    + ", ".join(f"[31;1m{c}[0m" for c in global_data["listen_along_invites"])
                    + "```",
                    color=self.bot.get_color(),
                )
            )

        global_data["listen_along_invites"][str(channel.id)] = invite.url

        await self.bot.update_global_data(inter.guild_id, global_data, db_name=DBModel.guilds)

        await inter.edit_original_message(
            f"**O link {invite} foi ativado/atualizado com sucesso para ser enviado via RPC quando houver "
            f"player ativo no canal {inter.author.voice.channel.mention}.**\n"
            f"`Nota: Caso queira exibir no seu status e não tenha o app de RPC, use o comando /rich_presence para "
            f"obter mais informações.`"
        )

        for bot in self.bot.pool.get_guild_bots(inter.guild_id):
            try:
                p = bot.music.players[inter.guild_id]
            except KeyError:
                continue

            if p.guild.me.voice.channel == inter.author.voice.channel:
                p.listen_along_invite = invite.url
                await p.process_rpc()
                await p.process_save_queue()

    @commands.Cog.listener("on_modal_submit")
    async def rpc_create_modal(self, inter: disnake.ModalInteraction):
        if inter.data.custom_id != "rpc_token_create":
            return

        await inter.response.defer(ephemeral=True)

        data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        if inter.text_values["token_input"] == data["token"]:
            await inter.send("Seu token é igual ao token atual!", ephemeral=True)
            return

        await self.bot.get_cog("RPCCog").close_presence(inter)

        data["token"] = inter.text_values["token_input"]

        await self.bot.update_global_data(id_=inter.author.id, data=data, db_name=DBModel.users)

        await inter.edit_original_message(
            f"O seu token foi importado/editado com sucesso!\nNota: Adicione/Atualize o token no app de RPC."
        )

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(
        name="nodeinfo", aliases=["llservers", "ll"], description="Ver informações dos servidores de música."
    )
    async def nodeinfo_legacy(self, ctx: CustomContext):
        await self.nodeinfo.callback(self=self, interaction=ctx)

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.slash_command(
        name=localized("cmd.nodeinfo.name"),
        description=localized("cmd.nodeinfo.description"),
    )
    @commands.contexts(guild=True)
    async def nodeinfo(self, interaction: disnake.ApplicationCommandInteraction):
        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        guild = bot.get_guild(inter.guild_id)

        color = bot.get_color(guild.me if guild else None)

        embeds = []

        if not bot.music.nodes:
            raise GenericError("**Não há servidores de música.**")

        failed_nodes = set()

        available_nodes = []

        for identifier, node in bot.music.nodes.items():
            if not node.stats or not node.is_available:
                failed_nodes.add(node.identifier)
            else:
                available_nodes.append([identifier, node])

        for page in disnake.utils.as_chunks(available_nodes, 6):
            em = disnake.Embed(color=color, title="Servidores de música:")

            for identifier, node in page:
                try:
                    current_player = node.players[inter.guild_id]
                except KeyError:
                    current_player = None

                if identifier == "LOCAL":
                    txt = "Região: `Local`\n"
                elif getattr(node, "region_label", None):
                    txt = f"Região: {md_code(node.region_label, limit=NODE_REGION_LIMIT)}\n"
                else:
                    txt = ""

                used = format_mib(node.stats.memory_used)
                total = format_mib(node.stats.memory_allocated)
                free = format_mib(node.stats.memory_free)
                reservable = format_mib(node.stats.memory_reservable)
                cpu_cores = node.stats.cpu_cores
                cpu_usage = f"{node.stats.lavalink_load * 100:.2f}"
                started = node.stats.players

                txt += (
                    f"Heap Java (usado/livre): `{used} / {free}`\n"
                    f"Heap Java alocado: `{total}`\n"
                    f"Heap Java máximo: `{reservable}`\n"
                    f"CPU Cores: {md_code(cpu_cores)}\n"
                    f"Uso de CPU: `{cpu_usage}%`\n"
                    f"Versão do Lavalink: {md_code(f'v{node.version}')}\n"
                    f"Uptime: <t:{int((disnake.utils.utcnow() - datetime.timedelta(milliseconds=node.stats.uptime)).timestamp())}:R>\n"
                )

                if started:
                    txt += "Players: "
                    players = node.stats.playing_players
                    idle = started - players
                    if players:
                        txt += f"`[▶️{players}]`" + (" " if idle else "")
                    if idle:
                        txt += f"`[💤{idle}]`"

                    txt += "\n"

                if node.website:
                    txt += f"{md_link('Website do server', node.website)}\n"

                status = "🌟" if current_player else "✅"

                em.add_field(name=f"**{identifier}** `{status}`", value=txt)

            em.set_footer(text=f"{bot.user} - [{bot.user.id}]", icon_url=bot.user.display_avatar.with_format("png").url)

            if failed_nodes:
                em.add_field(
                    name="**Servidores que falharam** `❌`",
                    value=f"```ansi\n[31;1m" + "\n".join(failed_nodes) + "[0m\n```",
                    inline=False,
                )

            embeds.append(em)

        kwargs = {}

        msg = None

        if len(embeds) > 1:
            kwargs["view"] = EmbedPaginatorInteraction(inter.author, embeds)

        if isinstance(inter, CustomContext):
            msg = await inter.send(embed=embeds[0], **kwargs)
        elif isinstance(inter, disnake.MessageInteraction):
            await inter.response.edit_message(embed=embeds[0], **kwargs)
        else:
            msg = await inter.send(embed=embeds[0], **kwargs)

        if kwargs.get("view"):
            await kwargs["view"].wait()
            for c in kwargs["view"].children:
                c.disabled = True
            if kwargs["view"].inter:
                await kwargs["view"].inter.response.edit_message(view=kwargs["view"])
            elif msg or isinstance(inter, CustomContext):
                await msg.edit(view=kwargs["view"])
            else:
                await inter.edit_original_message(view=kwargs["view"])

    customskin_cd = commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.guild)
    customskin__mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @commands.has_guild_permissions(administrator=True)
    @commands.command(
        name="customskin",
        aliases=["cskin", "cs", "setskin"],
        description="Criar suas próprias skins/templates para usar no player de música.",
        cooldown=customskin_cd,
        max_concurrency=customskin__mc,
    )
    async def customskin_legacy(self, ctx: CustomContext):
        await self.custom_skin.callback(self=self, inter=ctx)

    @commands.slash_command(
        name=localized("cmd.custom_skin.name"),
        cooldown=customskin_cd,
        max_concurrency=customskin__mc,
        description=localized("cmd.custom_skin.description"),
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    @commands.contexts(guild=True)
    async def custom_skin(self, inter: disnake.ApplicationCommandInteraction):
        inter, bot = await select_bot_pool(inter, return_new=True)

        if not bot:
            return

        await inter.response.defer()

        global_data = await bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)

        view = SkinEditorMenu(inter, bot, guild=bot.get_guild(inter.guild_id), global_data=global_data)

        if isinstance(inter, disnake.MessageInteraction):
            func = inter.edit_original_message
        else:
            func = inter.send
        view.message = await func(view=view, **view.build_embeds())
        await view.wait()

    @commands.Cog.listener("on_button_click")
    async def editor_placeholders(self, inter: disnake.MessageInteraction):
        if inter.data.custom_id != "skin_editor_placeholders" or not inter.guild:
            return

        await inter.send(
            ephemeral=True,
            embed=disnake.Embed(
                color=self.bot.get_color(inter.guild.me),
                description="### Placeholders para custom skins:\n```ansi\n"
                "[34;1m{track.title}[0m -> Nome da música\n"
                "[34;1m{track.title_25}[0m -> Nome da música (até 25 caracteres)\n"
                "[34;1m{track.title_42}[0m -> Nome da música (até 42 caracteres)\n"
                "[34;1m{track.title_58}[0m -> Nome da música (até 58 caracteres)\n"
                "[34;1m{track.url}[0m -> Link da música\n"
                "[34;1m{track.author}[0m -> Nome do Uploader/Artista da música\n"
                "[34;1m{track.duration}[0m -> Tempo/Duração da música\n"
                "[34;1m{track.thumb}[0m -> Link da miniatura/artowkr da música\n"
                "[34;1m{playlist.name}[0m -> Nome da playlist de origem da música\n"
                "[34;1m{playlist.url}[0m -> Link/Url da playlist de origem da música\n"
                "[34;1m{player.loop.mode}[0m -> Modo de repetição do player\n"
                "[34;1m{player.queue.size}[0m -> Quantidade de músicas na fila\n"
                "[34;1m{player.volume}[0m -> Volume do player\n"
                "[34;1m{player.autoplay}[0m -> Reprodução automática (Ativado/Desativado)\n"
                "[34;1m{player.nightcore}[0m -> Efeito nightcore (Ativado/Desativado)\n"
                "[34;1m{player.hint}[0m -> Dicas de uso do player\n"
                "[34;1m{player.log.text}[0m -> Log do player\n"
                "[34;1m{player.log.emoji}[0m -> Emoji do log do player\n"
                "[34;1m{requester.global_name}[0m -> Nome global do membro que pediu a música.\n"
                "[34;1m{requester.display_name}[0m -> Nome de exibição do membro que pediu a música.\n"
                "[34;1m{requester.mention}[0m -> Menção do membro que pediu a música\n"
                "[34;1m{requester.avatar}[0m -> Link do avatar do membro que pediu a música\n"
                "[34;1m{guild.color}[0m -> Cor do maior cargo do bot no servidor\n"
                "[34;1m{guild.icon}[0m -> Link do icone do servidor\n"
                "[34;1m{guild.name}[0m -> Nome do servidor\n"
                "[34;1m{guild.id}[0m -> ID do servidor\n"
                "[34;1m{queue_format}[0m -> Músicas da fila pré-formatada (use o botão de configurar "
                "placeholder caso queira alterar o estilo)\n"
                "[34;1m{track.number}[0m -> Número da posição da música na fila (funcional junto com "
                "o placeholder: [31;1m{queue_format}[0m)```",
            ),
        )


class RPCCog(commands.Cog):
    emoji = "🔧"
    name = "Configurações"
    desc_prefix = f"[{emoji} {name}] | "

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.voice_regions = {
            "brazil": "🇧🇷",
            "hongkong": "🇭🇰",
            "india": "🇮🇳",
            "japan": "🇯🇵",
            "rotterdam": "🇳🇱",
            "russia": "🇷🇺",
            "singapore": "🇸🇬",
            "southafrica": "🇿🇦",
            "sydney": "🇦🇺",
            "us-central": "🇺🇸",
            "us-east": "🇺🇸",
            "us-west": "🇺🇸",
            "us-south": "🇺🇸",
        }

    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.cooldown(1, 120, commands.BucketType.guild)
    @commands.has_permissions(manage_channels=True)
    @commands.command(aliases=["regiaopalco"], description="Alterar a região de um canal de palco")
    async def stageregion(self, ctx: CustomContext):
        if not isinstance(ctx.author.voice.channel, disnake.StageChannel):
            ctx.command.reset_cooldown(ctx)
            raise GenericError("**Você deve estar conectado em um canal de palco para usar esse comando.**")

        bot: Optional[BotCore] = None

        for b in self.bot.pool.get_guild_bots(ctx.guild_id):
            if not (bot_member := ctx.guild.get_member(b.user.id)):
                continue
            if ctx.author.voice.channel.permissions_for(bot_member).manage_channels:
                bot = b
                break

        if not bot:
            ctx.command.reset_cooldown(ctx)
            raise GenericError("**Não há bots com permissão de gerenciar canais no servidor**")

        vc = ctx.author.voice.channel

        view = ButtonInteraction(
            user=ctx.author,
            buttons=[
                disnake.ui.Button(label=k, custom_id=k, emoji=v)
                for k, v in self.voice_regions.items()
                if k != vc.rtc_region
            ],
        )

        color = self.bot.get_color(ctx.guild.me)

        msg = await ctx.send(
            embed=disnake.Embed(description="### Selecione uma região abaixo:", color=color),
            view=view,
        )

        await view.wait()

        if view.inter:
            ctx.inter = view.inter

        try:
            func = view.inter.edit_original_response
            await view.inter.response.defer()
        except AttributeError:
            func = msg.edit

        if not view.selected:
            await func(embed=disnake.Embed(color=color, description="### Operação cancelada!"), view=None)
            return

        await vc.edit(rtc_region=view.selected, reason=f"Região alterada por: {ctx.author.name} [{ctx.author.id}]")

        await func(
            embed=disnake.Embed(
                color=color,
                description=f"**A região do canal {vc.mention} foi alterado com sucesso para:\n{view.selected}**",
            ),
            view=None,
        )

    rpc_cd = commands.CooldownMapping.from_cooldown(1, 30, commands.BucketType.user)

    @commands.command(
        description="Ativar/Desativar o sistema de rich-presence no seu status.",
        name="richpresence",
        aliases=["rpc"],
        cooldown=rpc_cd,
    )
    async def rich_presence_legacy(self, ctx: CustomContext):
        await self.rich_presence.callback(self=self, inter=ctx)

    @commands.slash_command(
        name=localized("cmd.rich_presence.name"),
        description=localized("cmd.rich_presence.description"),
        cooldown=rpc_cd,
    )
    @commands.contexts(guild=True)
    async def rich_presence(self, inter: disnake.ApplicationCommandInteraction):
        if not self.bot.config["ENABLE_RPC_COMMAND"] and not any(
            [await b.is_owner(inter.author) for b in self.bot.pool.get_guild_bots(inter.guild_id)]
        ):
            raise GenericError(
                "**Este comando está desativado nas minhas configurações...**\n"
                "Apenas o meu desenvolvedor pode ativar este comando publicamente."
            )

        if not self.bot.config["RPC_PUBLIC_URL"] and not self.bot.config["RPC_SERVER"]:
            raise GenericError("**O RPC_SERVER não foi configurado na ENV/ENVIRONMENTS (ou arquivo .env)**")

        components = []

        embed = disnake.Embed(
            color=self.bot.get_color(),
            description="**Mini-guia para usar o app para exibir a música que você está ouvindo via RPC:\n\n"
            "Faça o download do app (musicbot_rpc.zip) "
            f"{md_link('aqui', branding.RPC_APP_RELEASES_URL, code=False)}.\n\n"
            "Extraia o musicbot_rpc.zip e na pasta abra o musicbot_rpc."
            "Adicione o link do websocket abaixo no app (aba: Socket Settings):** ```ansi\n"
            f"{(self.bot.config['RPC_PUBLIC_URL'] or self.bot.config['RPC_SERVER']).replace('$PORT', os.environ.get('PORT', '80'))}```",
        )

        embed.set_footer(
            text="Nota: No momento funciona apenas no windows com discord desktop, não funciona no mobile "
            "ou discord web."
        )

        if self.bot.config["ENABLE_RPC_AUTH"]:
            embed.description += (
                "\n**Será necessário criar/gerar/importar um token para liberar o acesso do RPC "
                "(Verifique os botões abaixo), copie o token e no app (Aba: Socket Settings) "
                'clique no botão "Colar Token"**'
            )

            components.extend(
                [
                    disnake.ui.Button(
                        label="Criar/Resetar token", custom_id=f"rpc_gen.{inter.author.id}", emoji="🔑", row=0
                    ),
                    disnake.ui.Button(
                        label="Importar/Editar/Ver token", custom_id=f"rpc_create.{inter.author.id}", emoji="✍️", row=0
                    ),
                    disnake.ui.Button(
                        label="Remover token (Desativar)", custom_id=f"rpc_remove.{inter.author.id}", emoji="♻️", row=1
                    ),
                ]
            )

        embed.description += (
            '\n\n**Agora basta apenas clicar no botão "Iniciar Presence" e escutar música através de '
            "algum bot compatível.**"
        )

        embed.set_author(
            name=f"{inter.author.display_name}#{inter.author.discriminator} - [ {inter.author.id} ]",
            icon_url=inter.author.display_avatar.with_static_format("png").url,
        )

        if isinstance(inter, CustomContext):
            components.append(
                disnake.ui.Button(label="Fechar", custom_id=f"rpc_close.{inter.author.id}", emoji="❌", row=1),
            )

        await inter.send(embed=embed, components=components, ephemeral=True)

    @commands.Cog.listener("on_button_click")
    async def rpc_button_event(self, inter: disnake.MessageInteraction):
        if not inter.data.custom_id.startswith("rpc_"):
            return

        button_id, user_id = inter.data.custom_id.split(".")

        if user_id != str(inter.author.id):
            await inter.send(f"Apenas <@{user_id}> pode usar os botões da mensagem!", ephemeral=True)
            return

        if button_id == "rpc_gen":
            await inter.response.defer()

            data = await self.bot.get_global_data(id_=user_id, db_name=DBModel.users)

            if data["token"]:
                await self.close_presence(inter)

            data["token"] = "".join(random.choice(string.ascii_letters + string.digits) for i in range(50))
            await self.bot.update_global_data(id_=user_id, data=data, db_name=DBModel.users)
            msg = (
                f"O token para usar no app de RPC (Rich Presence) foi gerado com sucesso!\n\n"
                f"`Token gerado:` ||{data['token']}||"
            )

        elif button_id == "rpc_create":
            kwargs = {}

            try:
                data = await self.bot.get_global_data(id_=user_id, db_name=DBModel.users)

                if len(data["token"]) == 50:
                    kwargs["value"] = data["token"]
            except Exception:
                pass

            await inter.response.send_modal(
                title="Importar token",
                custom_id="rpc_token_create",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Cole o token no campo abaixo:",
                        placeholder="Nota: Por medida de segurança, jamais inclua uma senha pessoal aqui!",
                        custom_id="token_input",
                        min_length=50,
                        max_length=50,
                        required=True,
                        **kwargs,
                    ),
                ],
            )

            if not inter.message.flags.ephemeral:
                await inter.message.delete()

            return

        elif button_id == "rpc_remove":
            await inter.response.defer()

            await self.close_presence(inter)

            data = await self.bot.get_global_data(id_=user_id, db_name=DBModel.users)

            data["token"] = ""
            await self.bot.update_global_data(id_=user_id, data=data, db_name=DBModel.users)
            msg = "O token foi removido com sucesso!\nAgora o sistema de rpc estará desativado no seu usuário."

        else:  # button_id == "rpc_close"
            await inter.message.delete()
            return

        if inter.message.flags.ephemeral:
            await inter.edit_original_message(content=msg, embeds=[], components=[])
        else:
            await inter.send(f"{inter.author.mention}: {msg}", embeds=[], components=[], ephemeral=True)
            await inter.message.delete()

    async def close_presence(self, inter: Union[disnake.MessageInteraction, disnake.ModalInteraction]):
        for b in self.bot.pool.get_guild_bots(inter.guild_id):
            try:
                player: LavalinkPlayer = b.music.players[inter.guild_id]
            except KeyError:
                continue

            try:
                if inter.author.id not in player.guild.me.voice.channel.voice_states:
                    continue
            except AttributeError:
                continue

            stats = {
                "op": "close",
                "bot_id": self.bot.user.id,
                "bot_name": str(self.bot.user),
                "thumb": self.bot.user.display_avatar.replace(size=512, static_format="png").url,
            }

            await player._send_rpc_data([inter.author.id], stats)


def setup(bot: BotCore):
    bot.add_cog(MusicSettings(bot))
    bot.add_cog(RPCCog(bot))
