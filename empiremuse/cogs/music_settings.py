# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import datetime
import os
import logging
from typing import TYPE_CHECKING, Any, Union, Optional

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.confirm_view import ConfirmView
from empiremuse.utils.ephemeral import CONFIRMATION_TTL_S, cleanup_later
from empiremuse.rules.errors import ROLE_DUPLICATE, RuleError
from empiremuse.rules.limits import (
    FORUM_TITLE_MAX,
    FORUM_TITLE_MIN,
    MAX_DJ_ROLES,
    VOLUMES,
)
from empiremuse.rules.rpc import TOKEN_LENGTH, generate_rpc_token
from empiremuse.rules.servers import SettingError
from empiremuse.rules.skins import jukebox_skin, saved_player_skins
from empiremuse.rules.settings import add_dj_role as add_role
from empiremuse.utils import branding
from empiremuse.utils.branding import asset_url
from empiremuse.utils import playlists_commands
from empiremuse.rules.playlists import SCOPE_JUKEBOX
from empiremuse.utils.db import DBModel
from empiremuse.rules.text import format_mib
from empiremuse.utils.music.jukebox_confirm import confirm_purge
from empiremuse.utils.music.converters import time_format, md_link, md_code, perms_translations
from empiremuse.utils.music.errors import GenericError, NoVoice
from empiremuse.utils.music.guild_text import guild_text
from empiremuse.utils.music.interactions import SkinEditorMenu, EmbedPaginatorInteraction, ButtonInteraction
from empiremuse.utils.music.interactions.i18n_views import guild_translator
from empiremuse.utils.music.interactions.skins_draft import plan_is_valid, plan_save
from empiremuse.utils.music.interactions.skins_view import (
    SkinsView,
    can_configure_radios,
    can_manage_skins,
    initial_draft,
    load_choices,
    redraw_radio_panels,
    save_skins,
    summary_lines,
)
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
from empiremuse.utils.user_docs import change_user_doc

logger = logging.getLogger(__name__)

NODE_REGION_LIMIT = 32  # a região vem do dono ou da lista externa de servidores


if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore


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
        await self._run_jukebox_reset(ctx)

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

    @jukebox.sub_command_group(
        name=localized("cmd.jukebox.playlists.name"), description=localized("cmd.jukebox.playlists.description")
    )
    async def jukebox_playlists(self, inter: disnake.ApplicationCommandInteraction) -> None:
        return

    @jukebox_playlists.sub_command(
        name=localized("cmd.jukebox.playlists.list.name"),
        description=localized("cmd.jukebox.playlists.list.description"),
    )
    async def jukebox_playlists_list(self, inter: disnake.ApplicationCommandInteraction) -> None:
        await playlists_commands.list_(self.bot, inter, SCOPE_JUKEBOX)

    @jukebox_playlists.sub_command(
        name=localized("cmd.jukebox.playlists.create.name"),
        description=localized("cmd.jukebox.playlists.create.description"),
    )
    async def jukebox_playlists_create(self, inter: disnake.ApplicationCommandInteraction) -> None:
        await playlists_commands.create(self.bot, inter, SCOPE_JUKEBOX)

    @jukebox_playlists.sub_command(
        name=localized("cmd.jukebox.playlists.edit.name"),
        description=localized("cmd.jukebox.playlists.edit.description"),
    )
    async def jukebox_playlists_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            name=localized("cmd.jukebox.playlists.edit.opt.name.name"),
            description=localized("cmd.jukebox.playlists.edit.opt.name.description"),
            autocomplete=playlists_commands.jukebox_names,
        ),
    ) -> None:
        await playlists_commands.edit(self.bot, inter, SCOPE_JUKEBOX, name)

    @jukebox_playlists.sub_command(
        name=localized("cmd.jukebox.playlists.remove.name"),
        description=localized("cmd.jukebox.playlists.remove.description"),
    )
    async def jukebox_playlists_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            name=localized("cmd.jukebox.playlists.remove.opt.name.name"),
            description=localized("cmd.jukebox.playlists.remove.opt.name.description"),
            autocomplete=playlists_commands.jukebox_names,
        ),
    ) -> None:
        await playlists_commands.remove(self.bot, inter, SCOPE_JUKEBOX, name)

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

        if (
            existing_channel
            and purge_messages == "yes"
            and isinstance(target, (disnake.TextChannel, disnake.VoiceChannel, disnake.StageChannel))
            and not await confirm_purge(inter, target)
        ):
            return

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

        description = (
            f"**{msg}**\n\nObs: Caso queira reverter essa configuração, apenas use o comando {reset_txt} ou "
            f"delete o canal/post {channel.mention}"
        )
        if result.not_applied:
            tr = await bot.pool.i18n.for_guild(guild)
            names = ", ".join(f"**{perms_translations.get(name, name)}**" for name in result.not_applied)
            description += "\n\n" + tr("jukebox.setup.warn_permissions", channel=channel.mention, permissions=names)
        embed = disnake.Embed(description=description, color=bot.get_color(guild.me))

        warning_message = None
        if isinstance(inter, CustomContext):
            warning_message = await inter.send(embed=embed)
        else:
            if not inter.response.is_done():
                try:
                    await inter.response.edit_message(embed=embed, components=None)
                except AttributeError:
                    await inter.send(embed=embed)
            try:
                warning_message = await inter.edit_original_message(embed=embed, components=None)
            except AttributeError, disnake.InteractionNotEditable:
                try:
                    await inter.response.edit_message(embed=embed, components=None)
                    warning_message = await inter.original_message()
                except Exception:
                    warning_message = await inter.followup.send(embed=embed, ephemeral=True, wait=True)

        if result.not_applied:
            from empiremuse.music_cog.jukebox_messages import retain_jukebox_permissions_warning

            await retain_jukebox_permissions_warning(interaction, warning_message)

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
        """Sem opção nenhuma (R3): redefine a Jukebox DO SERVIDOR, depois de uma confirmação só de quem pediu."""
        tr = await self.bot.pool.i18n.for_guild(interaction.guild)

        async def run(click: disnake.MessageInteraction) -> None:
            await click.response.defer()
            await self._run_jukebox_reset(click)

        view = ConfirmView(
            tr,
            interaction.author.id,
            run,
            confirm_key="jukebox.reset.confirm",
            cancel_key="jukebox.reset.cancel",
            cancelled_key="jukebox.reset.cancelled",
            not_yours_key="jukebox.reset.not_yours",
        )
        await interaction.response.send_message(tr("jukebox.reset.ask"), view=view, ephemeral=True)
        view.message_interaction = interaction

    async def _run_jukebox_reset(
        self,
        interaction: disnake.ApplicationCommandInteraction,
        delete_channel: Optional[str] = None,
    ):
        if isinstance(interaction, CustomContext):
            inter, bot = await select_bot_pool(interaction)
            if not bot:
                return
        else:  # slash: nenhum seletor de bot, a Jukebox é uma por servidor e quem a reseta é o bot do painel
            inter, bot = interaction, interaction.bot

        bot = (
            await bot.pool.servers.jukebox.panel_bot(inter.guild_id)
        ) or bot  # o canal é único: quem reseta é o bot do painel

        if not inter.response.is_done():
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
                **({} if isinstance(inter, CustomContext) else {"content": None}),
            )
            if not isinstance(inter, CustomContext):
                cleanup_later(inter, CONFIRMATION_TTL_S)

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
    RADIO_ONLY_FIELDS = frozenset({"radio_skin"})  # campos do documento global do /skins que não mudam o player
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
        name=localized("cmd.skins.name"),
        description=localized("cmd.skins.description"),
        cooldown=skin_cd,
        max_concurrency=skin_mc,
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    @commands.contexts(guild=True)
    async def change_skin(self, interaction: disnake.ApplicationCommandInteraction):
        """O ``/skins`` (2.3): rascunho na view até o Salvar; o par das rádios só com a permissão de quem configura rádio."""
        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        await inter.response.defer(ephemeral=True)

        guild = bot.get_guild(inter.guild_id) or inter.guild
        tr = await guild_translator(bot, guild)
        colour = bot.get_color(guild.me)
        if not can_manage_skins(inter.author):  # a permissão do comando pode ser trocada pelo servidor nas integrações
            await inter.edit_original_message(embed=disnake.Embed(description=tr("skin.no_permission"), colour=colour))
            return
        # Pelo prefixo a resposta é pública (o ``CustomContext`` descarta ``ephemeral``): sem o par das rádios, que lista o nome
        # dos canais delas, inclusive de call privada. As rádios ficam no ``/skins`` de barra.
        via_prefix = isinstance(interaction, CustomContext)
        radio_allowed = not via_prefix and can_configure_radios(inter.author)
        settings = await bot.pool.servers.settings(guild.id)
        global_data = await bot.get_global_data(guild.id, db_name=DBModel.guilds)
        choices = await load_choices(bot, guild, global_data, radio_allowed=radio_allowed)
        footer = tr("skin.ui.bot", bot=bot.user.display_name) if bot.user.id != self.bot.user.id else None

        view = SkinsView(
            bot=bot,
            author_id=inter.author.id,
            tr=tr,
            choices=choices,
            initial=initial_draft(bot.pool, settings, global_data, choices),
            colour=colour,
            footer=footer,
            note=tr("skin.ui.radios_by_slash") if via_prefix else None,
        )

        try:
            func = inter.store_message.edit
        except Exception:
            try:
                func = inter.edit_original_message
            except AttributeError:
                func = inter.send

        view.message = await func(**view.payload())

        await view.wait()

        if view.interaction is None:
            return  # o tempo acabou: ``on_timeout`` já desligou os componentes

        update_inter(interaction, view.interaction)
        inter = view.interaction

        if view.result != "save":
            await inter.response.edit_message(
                embeds=[disnake.Embed(description=tr("skin.cancelled"), colour=colour)], view=None, attachments=[]
            )
            return

        await inter.response.defer()
        await self._save_skins(bot, guild, inter, tr, view, radio_allowed)

    async def _save_skins(self, bot, guild, inter, tr, view: SkinsView, radio_allowed: bool) -> None:
        """O Salvar do ``/skins``: a permissão de novo, tudo revalidado contra as listas relidas e só o que mudou é gravado."""
        colour = bot.get_color(guild.me)

        async def reply(text: str) -> None:
            embed = disnake.Embed(description=text, colour=colour).set_footer(
                text=f"{bot.user} - [{bot.user.id}]", icon_url=bot.user.display_avatar.with_format("png").url
            )
            try:
                await inter.edit_original_message(embeds=[embed], view=None, attachments=[])
            except disnake.HTTPException:  # o que já foi gravado ainda precisa chegar aos players e painéis
                logger.warning("Falha ao responder o /skins", exc_info=True)

        if not can_manage_skins(inter.author):
            await reply(tr("skin.no_permission"))
            return

        radio_allowed = radio_allowed and can_configure_radios(inter.author)
        plan = plan_save(view.initial, view.draft, view.choices.radios, radio_allowed=radio_allowed)
        fresh = await bot.get_global_data(guild.id, db_name=DBModel.guilds)
        current = await load_choices(bot, guild, fresh, radio_allowed=radio_allowed)
        if not plan_is_valid(plan, current):
            await reply(tr("skin.invalid"))
            return

        try:
            failed = await save_skins(bot, guild.id, plan, updated_by=str(inter.author.id))
        except Exception as error:  # noqa: BLE001 - banco fora (ou bug) no meio do Salvar: o membro recebe a resposta
            logger.warning("Falha ao gravar o /skins (%s)", type(error).__name__, exc_info=True)
            await reply(tr("skin.save_failed"))
            await self._reconcile_players(bot, guild.id, plan, tr, inter.author.mention)  # um escopo pode ter gravado
            return
        saved_plan = plan._replace(radio_looks=tuple(i for i in plan.radio_looks if i.bot_id not in failed))
        lines = summary_lines(tr, saved_plan, view.choices.radios)
        text = "\n".join([f"**{tr('skin.saved')}**", *lines]) if lines else f"**{tr('skin.unchanged')}**"
        labels = {row.bot_id: row.text for row in view.choices.radios}
        for bot_id in failed:
            text += "\n⚠️ " + tr("skin.radio.changed", radio=labels.get(bot_id, str(bot_id)))
        if not radio_allowed and plan_save(view.initial, view.draft, view.choices.radios, radio_allowed=True) != plan:
            text += "\n⚠️ " + tr("skin.no_permission")  # a edição das rádios foi descartada, a escrita fica bloqueada

        global_data = await bot.get_global_data(guild.id, db_name=DBModel.guilds)
        skins = await load_custom_skins(bot, guild.id, global_data)
        if skins.rejected:
            text += "\n\n⚠️ " + tr("skin.custom_rejected", count=skins.rejected)

        await reply(text)

        if "radio_skin" in plan.global_fields:
            redraw_radio_panels(bot.pool, guild.id, [row.bot_id for row in current.radios])  # a lista de agora
        else:
            redraw_radio_panels(bot.pool, guild.id, [item.bot_id for item in saved_plan.radio_looks])

        await self._reconcile_players(bot, guild.id, plan, tr, inter.author.mention, global_data, skins)

    async def _reconcile_players(
        self, bot, guild_id: int, plan, tr, mention: str, global_data=None, skins=None
    ) -> None:
        """Aplica aos players o que ficou gravado. Só a skin das rádios mudou: os players ficam como estão."""
        if not (set(plan.global_fields) - self.RADIO_ONLY_FIELDS or plan.server_fields):
            return
        try:
            if global_data is None:
                global_data = await bot.get_global_data(guild_id, db_name=DBModel.guilds)
                skins = await load_custom_skins(bot, guild_id, global_data)
            await self._reload_player_skins(bot, guild_id, global_data, skins, mention, tr)
        except Exception:
            logger.exception("Falha ao aplicar o /skins aos players")

    async def _reload_player_skins(self, bot, guild_id: int, global_data, skins, mention: str, tr) -> None:
        """Recarrega os players ativos com a skin efetiva (mesma precedência da criação do player). A animação do disco mudou:
        o cartão se redesenha no próximo ciclo, mesmo sem troca de skin."""
        settings = await bot.pool.servers.settings(guild_id)
        skin, static = saved_player_skins(settings, global_data)
        skin = jukebox_skin(skin, bot.pool.default_skin, True)  # o Q1 já foi aplicado em cada nível
        static = jukebox_skin(static, bot.pool.default_static_skin, True)

        for b in self.bot.pool.get_all_bots():
            try:
                player: LavalinkPlayer = b.music.players[guild_id]
            except KeyError:
                continue

            if (static if player.static else skin) == str(player.skin_static if player.static else player.skin):
                player.skin = skin  # o modo inativo também mudou: o player guarda as duas
                player.skin_static = static
                player.custom_skin_data = skins.normal
                player.custom_skin_static_data = skins.static
                player.last_data = {}
                player.update = True
                continue

            try:
                await player.destroy_message()
            except Exception:
                logger.exception("Falha ao apagar a mensagem do player")

            player.skin = skin
            player.skin_static = static

            player.custom_skin_data = skins.normal
            player.custom_skin_static_data = skins.static
            player.setup_features()

            player.setup_hints()
            player.process_hint()
            player.set_command_log(text=tr("skin.command_log", mention=mention), emoji="🎨")

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

        for bot in self.bot.pool.get_all_bots():
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

        for bot in self.bot.pool.get_all_bots():
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

        # antes de qualquer espera (o defer incluído): apagamento depois disto descarta a gravação
        read_at = self.bot.pool.erasure_tombstones.now()
        await inter.response.defer(ephemeral=True)

        data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)
        new_token = inter.text_values["token_input"]

        if new_token == data["token"]:
            await inter.send("Seu token é igual ao token atual!", ephemeral=True)
            return

        await self.bot.get_cog("RPCCog").close_presence(inter)  # rede: antes do lock do usuário

        change = await change_user_doc(
            self.bot, inter.author.id, lambda doc: doc.__setitem__("token", new_token), read_at=read_at
        )
        if change.erased:
            await inter.edit_original_message(await guild_text(self.bot, inter.guild, "erasure.erased_while_saving"))
            return

        await inter.edit_original_message(
            f"O seu token foi importado/editado com sucesso!\nNota: Adicione/Atualize o token no app de RPC e reinicie o app."
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

        for b in self.bot.pool.get_all_bots():
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
            [await b.is_owner(inter.author) for b in self.bot.pool.get_all_bots()]
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

    async def _store_rpc_token(self, inter: disnake.MessageInteraction, token: str, read_at: float) -> bool:
        """Grava o token do RPC do autor sob o lock do usuário; ``False`` (com o aviso já enviado) se os dados dele foram apagados
        desde ``read_at``."""
        change = await change_user_doc(
            self.bot, inter.author.id, lambda doc: doc.__setitem__("token", token), read_at=read_at
        )
        if change.erased:
            await inter.send(await guild_text(self.bot, inter.guild, "erasure.erased_while_saving"), ephemeral=True)
        return not change.erased

    @commands.Cog.listener("on_button_click")
    async def rpc_button_event(self, inter: disnake.MessageInteraction):
        if not inter.data.custom_id.startswith("rpc_"):
            return

        button_id, user_id = inter.data.custom_id.split(".")

        if user_id != str(inter.author.id):
            await inter.send(f"Apenas <@{user_id}> pode usar os botões da mensagem!", ephemeral=True)
            return

        # antes de qualquer espera (o defer incluído): apagamento depois disto descarta a gravação
        read_at = self.bot.pool.erasure_tombstones.now()

        if button_id == "rpc_gen":
            await inter.response.defer()

            data = await self.bot.get_global_data(id_=user_id, db_name=DBModel.users)

            if data["token"]:
                await self.close_presence(inter)  # rede: antes do lock do usuário

            new_token = generate_rpc_token()
            if not await self._store_rpc_token(inter, new_token, read_at):
                return
            msg = (
                f"O token para usar no app de RPC (Rich Presence) foi gerado com sucesso!\n\n"
                f"`Token gerado:` ||{new_token}||\n\n"
                f"Cole o token no app de RPC e **reinicie o app**."
            )

        elif button_id == "rpc_create":
            kwargs = {}

            try:
                data = await self.bot.get_global_data(id_=user_id, db_name=DBModel.users)

                if len(data["token"]) == TOKEN_LENGTH:
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
                        min_length=TOKEN_LENGTH,
                        max_length=TOKEN_LENGTH,
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

            await self.close_presence(inter)  # rede: antes do lock do usuário

            if not await self._store_rpc_token(inter, "", read_at):
                return
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
        for b in self.bot.pool.get_all_bots():
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
