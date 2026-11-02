# -*- coding: utf-8 -*-
"""Menus de favoritos, integrações, músicas recentes e fila salva do `/play`."""

import datetime
import logging
from typing import TYPE_CHECKING

import disnake

from empiremuse.music_cog.play_guild_menu import GuildPlaylistSelect
from empiremuse.rules.limits import MAX_SERVER_PLAYLISTS
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.converters import fix_characters, md_link
from empiremuse.utils.music.errors import (
    GenericError,
    EmptyFavIntegration,
)
from empiremuse.utils.music.saved_queue import has_saved_queue
from empiremuse.utils.server_playlists import safe_list, select_options
from empiremuse.utils.music.interactions import (
    SelectInteraction,
)
from empiremuse.utils.others import (
    music_source_emoji_url,
    jukebox_buttons,
    get_source_emoji_cfg,
)
from empiremuse.utils.music.app_emojis import emoji as app_emoji

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from empiremuse.music_cog.play_state import PlayState

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.


class PlayMenusMixin:
    async def _play_menu_options(self, req: "PlayState") -> None:
        """Monta o texto e as opções do menu de favoritos, integrações e fila salva."""
        bot = req.bot
        ephemeral = req.ephemeral
        fav_slashcmd = req.fav_slashcmd
        guild_data = req.guild_data
        inter = req.inter
        savequeue_slashcmd = req.savequeue_slashcmd
        user_data = req.user_data

        opts = []

        txt = "### `[⭐] Favoritos [⭐]`\n"

        if user_data["fav_links"]:
            opts.append(disnake.SelectOption(label="Usar favorito", value=">> [⭐ Favoritos ⭐] <<", emoji="⭐"))
            txt += f"`Tocar música ou playlist que você curtiu ou que você tenha adicionado nos seus favoritos.`\n"

        else:
            txt += f"`Você não possui favoritos...`\n"

        txt += f"-# Você pode gerenciar seus favoritos usando o comando {fav_slashcmd}.\n### `[💠] Integrações [💠]`\n"

        if user_data["integration_links"]:
            opts.append(disnake.SelectOption(label="Usar integração", value=">> [💠 Integrações 💠] <<", emoji="💠"))
            txt += f"`Tocar playlist pública de um canal do youtube (ou de um perfil de usuário de alguma plataforma de música) da sua lista de integrações.`\n"

        else:
            txt += (
                f"`Você não possui integração adicionada... "
                f"Use as integrações para adicionar links de canais do youtube (ou link de perfil de algum usuário de alguma plataforma de música) para ter acesso facilita a todas a playlists públicas que o mesmo possui.`\n"
            )

        txt += (
            f'-# Para gerenciar suas integrações use o comando {fav_slashcmd} e em seguida selecione a opção "integrações".\n'
            f"### `[💾] Fila Salva [💾]`\n"
        )

        if has_saved_queue(inter.author.id):
            txt += f"`Usar fila de música que você salvou via comando` {savequeue_slashcmd}.\n"
            opts.append(disnake.SelectOption(label="Usar fila salva", value=">> [💾 Fila Salva 💾] <<", emoji="💾"))

        else:
            txt += (
                "`Você não possui uma fila de música salva`\n"
                f"-# Pra ter uma fila salva você pode usar o comando {savequeue_slashcmd} quando houver no mínimo 3 músicas adicionadas no player."
            )

        if user_data["last_tracks"]:
            txt += (
                "### `[📑] Músicas recentes [📑]`\n`Tocar uma música que você tenha ouvido/adicionado recentemente.`\n"
            )
            opts.append(
                disnake.SelectOption(
                    label="Adicionar música recente",
                    value=">> [📑 Músicas recentes 📑] <<",
                    emoji=app_emoji(self.bot, "music_queue"),
                )
            )

        if isinstance(inter, disnake.MessageInteraction) and not inter.response.is_done():
            await inter.response.defer(ephemeral=ephemeral)

        if not guild_data:
            guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        if await safe_list(bot, inter.guild_id):
            txt += (
                "### `[📌] Favoritos do servidor [📌]`\n"
                "`Usar favorito do servidor (adicionados por staffs do servidor).`\n"
            )
            opts.append(
                disnake.SelectOption(
                    label="Usar favorito do servidor", value=">> [📌 Favoritos do servidor 📌] <<", emoji="📌"
                )
            )

        if not opts:
            raise EmptyFavIntegration()

        req.guild_data = guild_data
        req.opts = opts
        req.txt = txt

    async def _play_ask_menu(self, req: "PlayState") -> bool:
        """Mostra o menu inicial e aplica a opção escolhida pelo usuário."""
        guild = req.guild
        inter = req.inter
        msg = req.msg
        opts = req.opts
        txt = req.txt

        embed = disnake.Embed(
            color=self.bot.get_color(guild.me),
            description=f"{txt}## Selecione uma opção abaixo:"
            f"\n-# Nota: Essa solicitação será cancelada automaticamente <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=180)).timestamp())}:R> caso não seja selecionado uma opção abaixo.",
        )

        kwargs = {"content": "", "embed": embed}

        try:
            if inter.message.author.bot:
                kwargs["content"] = inter.author.mention
        except AttributeError:
            pass

        view = SelectInteraction(user=inter.author, timeout=180, opts=opts)

        try:
            await msg.edit(view=view, **kwargs)
        except AttributeError:
            try:
                await inter.edit_original_message(view=view, **kwargs)
            except AttributeError:
                msg = await inter.send(view=view, **kwargs)

        await view.wait()

        select_interaction = view.inter

        try:
            func = inter.edit_original_message
        except AttributeError:
            func = msg.edit

        if not select_interaction or view.selected is False:
            embed.set_footer(
                text="⚠️ " + ("Tempo de seleção esgotado!" if view.selected is not False else "Cancelado pelo usuário.")
            )

            try:
                await func(
                    embed=embed,
                    components=jukebox_buttons(self.bot, await self.bot.pool.i18n.for_guild(inter.guild)),
                )
            except AttributeError:
                logger.exception("Falha ao atualizar a mensagem de pedido de música")
                pass
            return True

        if select_interaction.data.values[0] == "cancel":
            await func(
                embed=disnake.Embed(description="**Seleção cancelada!**", color=self.bot.get_color(guild.me)),
                components=None,
            )
            return True

        try:
            inter.store_message = msg
        except AttributeError:
            pass

        inter.token = select_interaction.token
        inter.id = select_interaction.id
        inter.response = select_interaction.response
        query = select_interaction.data.values[0]
        await inter.response.defer()

        req.embed = embed
        req.msg = msg
        req.query = query
        return False

    async def _play_fav_options(self, req: "PlayState") -> None:
        """Monta as opções de favoritos, integrações, músicas recentes ou favoritos do servidor."""
        bot = req.bot
        emoji = req.emoji
        guild = req.guild
        guild_data = req.guild_data
        inter = req.inter
        is_pin = req.is_pin
        query = req.query
        user_data = req.user_data

        fav_opts = []

        menu = None
        selected_title = ""

        if query.startswith(">> [💠 Integrações 💠] <<"):
            query = ""
            menu = "integrations"
            for k, v in user_data["integration_links"].items():
                update = False

                if not isinstance(v, dict):
                    v = {"url": v, "avatar": None}
                    user_data["integration_links"][k] = v
                    update = True

                if update:
                    await self.bot.update_global_data(inter.author.id, user_data, db_name=DBModel.users)

                emoji, platform = music_source_emoji_url(v["url"], self.bot)

                fav_opts.append(
                    {
                        "url": v["url"],
                        "option": disnake.SelectOption(
                            label=fix_characters(k[6:], 45),
                            value=f"> itg: {k}",
                            description=f"[💠 Integração 💠] -> {platform}",
                            emoji=emoji,
                        ),
                    }
                )

        elif query.startswith(">> [⭐ Favoritos ⭐] <<"):
            query = ""
            menu = "favs"
            for k, v in user_data["fav_links"].items():
                emoji, platform = music_source_emoji_url(v, self.bot)
                fav_opts.append(
                    {
                        "url": v,
                        "option": disnake.SelectOption(
                            label=fix_characters(k, 45),
                            value=f"> fav: {k}",
                            description=f"[⭐ Favorito ⭐] -> {platform}",
                            emoji=emoji,
                        ),
                    }
                )

        elif query.startswith(">> [📑 Músicas recentes 📑] <<"):
            if not user_data["last_tracks"]:
                raise GenericError(
                    "**Você não possui músicas registradas no seu histórico...**\n"
                    "Elas vão aparecer a medida que for adicionando músicas via busca ou link."
                )

            query = ""
            menu = "latest"
            for i, d in enumerate(user_data["last_tracks"]):
                emoji, platform = music_source_emoji_url(d["url"], self.bot)
                fav_opts.append(
                    {
                        "url": d["url"],
                        "option": disnake.SelectOption(
                            label=d["name"],
                            value=f"> lst: {i}",
                            description=f"[📑 Músicas recentes 📑] -> {platform}",
                            emoji=emoji,
                        ),
                    }
                )

        elif query.startswith(">> [📌 Favoritos do servidor 📌] <<"):
            if not guild_data:
                guild_data = await bot.get_data(guild.id, db_name=DBModel.guilds)

            playlists = await safe_list(bot, guild.id)
            if not playlists:
                raise GenericError("**O servidor não possui links fixos/favoritos.**")

            menu = "guild_favs"

            # as 25 primeiras por ordem; o valor "> pin: <nome>" é resolvido de novo no store ao tocar
            for playlist, option in zip(playlists, select_options(self.bot, playlists, MAX_SERVER_PLAYLISTS)):
                option.value = f"> pin: {playlist.name}"
                fav_opts.append({"url": playlist.url, "option": option})

            is_pin = False

        req.emoji = emoji
        req.fav_opts = fav_opts
        req.guild_data = guild_data
        req.is_pin = is_pin
        req.menu = menu
        req.query = query
        req.selected_title = selected_title

    async def _play_fav_select(self, req: "PlayState") -> bool:
        """Pede ao usuário que escolha um dos itens da lista montada."""
        bot = req.bot
        embed = req.embed
        ephemeral = req.ephemeral
        fav_opts = req.fav_opts
        fav_slashcmd = req.fav_slashcmd
        guild = req.guild
        inter = req.inter
        menu = req.menu
        msg = req.msg
        query = req.query
        selected_title = req.selected_title

        if fav_opts:
            if len(fav_opts) == 1:
                query = list(fav_opts)[0]["option"].value

            else:
                check_url = lambda i: f"{i}/playlists" if (".spotify." in i or ".deezer." in i) else i

                embed = disnake.Embed(
                    color=self.bot.get_color(guild.me),
                    description="\n".join(
                        f"{get_source_emoji_cfg(bot, i['url']) or ''} {md_link(i['option'].label, check_url(i['url']), limit=45)}"
                        for i in fav_opts
                    ),
                )

                if menu == "favs":
                    embed.description = (
                        "### `[⭐] ⠂Favoritos ⠂[⭐]`\n"
                        "`Tocar música ou playlist que você curtiu ou que você tenha adicionado na sua lista de favoritos.`\n"
                        f"-# Você pode gerenciar seus favoritos usando o comando {fav_slashcmd}.\n\n"
                        f"{embed.description}\n\n**Selecione um favorito abaixo:**"
                    )

                elif menu == "integrations":
                    embed.description = (
                        "### `[💠] ⠂Integrações ⠂[💠]`\n"
                        "`Tocar playlist pública de um canal do youtube (ou de um perfil de usuário de alguma plataforma de música) da sua lista de integrações.`\n"
                        f'-# Para gerenciar suas integrações use o comando {fav_slashcmd} e em seguida selecione a opção "integrações".\n\n'
                        f"{embed.description}\n\n**Selecione uma integração abaixo:**"
                    )

                elif menu == "guild_favs":
                    embed.description = (
                        f"### `[📌] ⠂Favoritos do servidor ⠂[📌]\n"
                        "`Usar favorito do servidor (adicionados por staffs do servidor).`\n\n"
                        f"{embed.description}\n\n**Selecione um favorito abaixo:**"
                    )

                elif menu == "latest":
                    embed.description = (
                        f"### 📑 ⠂Tocar música/playlist recente:\n{embed.description}\n\n**Selecione um item abaixo:**"
                    )

                embed.description += f"\n-# Nota: Essa solicitação será cancelada automaticamente <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=75)).timestamp())}:R> caso não seja selecionado uma opção abaixo."

                try:
                    if bot.user.id != self.bot.user.id:
                        embed.set_footer(
                            text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                        )
                except AttributeError:
                    pass

                kwargs = {"content": "", "embed": embed}

                try:
                    if inter.message.author.bot:
                        kwargs["content"] = inter.author.mention
                except AttributeError:
                    pass

                if menu == "guild_favs":
                    playlists = await safe_list(
                        bot, guild.id
                    )  # relida: pode ter esvaziado desde que o menu foi montado
                    if not playlists:
                        raise GenericError("**O servidor não possui links fixos/favoritos.**")
                    view = GuildPlaylistSelect(inter.author, bot, playlists)
                else:
                    view = SelectInteraction(user=inter.author, timeout=75, opts=[i["option"] for i in fav_opts])

                if isinstance(inter, disnake.MessageInteraction) and not inter.response.is_done():
                    await inter.response.defer(ephemeral=ephemeral)

                try:
                    func = msg.edit
                except AttributeError:
                    try:
                        if inter.response.is_done():
                            func = inter.edit_original_message
                        else:
                            func = inter.response.send_message
                            kwargs["ephemeral"] = ephemeral
                    except AttributeError:
                        kwargs["ephemeral"] = ephemeral
                        try:
                            func = inter.followup.send
                        except AttributeError:
                            func = inter.send

                msg = await func(view=view, **kwargs)

                await view.wait()

                select_interaction = view.inter

                if not select_interaction or view.selected is False:
                    embed.description = "\n".join(embed.description.split("\n")[:-3])
                    embed.set_footer(
                        text="⚠️ Tempo esgotado!" if not view.selected is False else "⚠️ Cancelado pelo usuário."
                    )

                    try:
                        await msg.edit(
                            embed=embed,
                            components=jukebox_buttons(self.bot, await self.bot.pool.i18n.for_guild(inter.guild)),
                        )
                    except AttributeError:
                        try:
                            await select_interaction.response.edit_message(
                                embed=embed,
                                components=jukebox_buttons(self.bot, await self.bot.pool.i18n.for_guild(inter.guild)),
                            )
                        except AttributeError:
                            logger.exception("Falha ao atualizar a mensagem de pedido de música")
                    return True

                if select_interaction.data.values[0] == "cancel":
                    embed.description = "\n".join(embed.description.split("\n")[:-3])
                    embed.set_footer(text="⚠️ Seleção cancelada!")
                    await msg.edit(embed=embed, components=None)
                    return True

                try:
                    inter.store_message = msg
                except AttributeError:
                    pass

                inter.token = select_interaction.token
                inter.id = select_interaction.id
                inter.response = select_interaction.response
                query = select_interaction.data.values[0]
                selected_title = ":".join(query.split(":")[2:])

        elif not query:
            raise EmptyFavIntegration()

        req.embed = embed
        req.msg = msg
        req.query = query
        req.selected_title = selected_title
        return False
