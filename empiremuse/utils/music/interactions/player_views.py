# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import List, Union, Optional, TYPE_CHECKING

import disnake
from disnake.ext import commands

from empiremuse.utils.music.converters import time_format, fix_characters, md_link
from empiremuse.utils.music.models import LavalinkPlayer, LavalinkTrack
from empiremuse.utils.others import (
    check_cmd,
    CustomContext,
)

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

from empiremuse.utils.music.interactions.view_modal import ViewModal
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class VolumeInteraction(disnake.ui.View):
    def __init__(self, inter):
        self.inter = inter
        self.volume = None
        super().__init__(timeout=30)
        self.process_buttons()

    def process_buttons(self):
        opts = []

        for l in [5, 20, 40, 60, 80, 100, 120, 150]:
            if l > 100:
                description = "Acima de 100% o audio pode ficar bem ruim."
            else:
                description = None
            opts.append(disnake.SelectOption(label=f"{l}%", value=f"vol_{l}", description=description))

        select = disnake.ui.Select(placeholder="Nível:", options=opts)
        select.callback = self.callback
        self.add_item(select)

    async def callback(self, interaction: disnake.MessageInteraction):
        await interaction.response.edit_message(content=f"Volume alterado!", embed=None, view=None)
        self.volume = int(interaction.data.values[0][4:])
        self.stop()


class QueueInteraction(disnake.ui.View):
    def __init__(self, bot: BotCore, user: disnake.Member, timeout=120):
        self.bot = bot
        self.user = user
        self.track_pages = []
        self.select_options = []
        self.current_page = 0
        self.max_page = 1
        self.max_items = 8
        self.message: Optional[disnake.Message] = None
        self.current_track: Optional[LavalinkTrack] = None
        super().__init__(timeout=timeout)
        self.embed = disnake.Embed(color=self.bot.get_color(user.guild.me))
        self.update_pages()
        self.update_embed()

    def update_pages(self, reset_page=True):
        player: LavalinkPlayer = self.bot.music.players[self.user.guild.id]

        if reset_page:
            self.current_page = 0

        self.track_pages.clear()
        self.track_pages = list(disnake.utils.as_chunks(player.queue or player.queue_autoplay, max_size=self.max_items))
        self.current_track = self.track_pages[self.current_page][0]
        self.max_page = len(self.track_pages) - 1
        self.update_components()

    async def on_timeout(self) -> None:
        if not self.message:
            return

        self.embed.set_footer(text="Tempo para interagir esgotado!")

        for c in self.children:
            c.disabled = True

        await self.message.edit(embed=self.embed, view=self)

    def update_components(self):
        if not self.select_options:
            return

        self.clear_items()

        track_select = disnake.ui.Select(
            placeholder="Tocar uma música específica da página:",
            options=self.select_options,
            custom_id="queue_track_selection",
            max_values=1,
        )

        track_select.callback = self.track_select_callback

        self.add_item(track_select)

        first = disnake.ui.Button(emoji=app_emoji(self.bot, "backward"), style=disnake.ButtonStyle.grey)
        first.callback = self.first
        self.add_item(first)

        back = disnake.ui.Button(emoji=app_emoji(self.bot, "arrow_left"), style=disnake.ButtonStyle.grey)
        back.callback = self.back
        self.add_item(back)

        next = disnake.ui.Button(emoji=app_emoji(self.bot, "arrow_right"), style=disnake.ButtonStyle.grey)
        next.callback = self.next
        self.add_item(next)

        last = disnake.ui.Button(emoji=app_emoji(self.bot, "skip"), style=disnake.ButtonStyle.grey)
        last.callback = self.last
        self.add_item(last)

        stop_interaction = disnake.ui.Button(emoji=app_emoji(self.bot, "stop"), style=disnake.ButtonStyle.grey)
        stop_interaction.callback = self.stop_interaction
        self.add_item(stop_interaction)

        play = disnake.ui.Button(
            emoji=app_emoji(self.bot, "play"), label="Tocar", style=disnake.ButtonStyle.grey, custom_id="queue_skip"
        )
        play.callback = self.invoke_command
        self.add_item(play)

        move = disnake.ui.Button(
            emoji=app_emoji(self.bot, "drag"), label="Mover", style=disnake.ButtonStyle.grey, custom_id="queue_move"
        )
        move.callback = self.move_callback
        self.add_item(move)

        rotate_q = disnake.ui.Button(
            emoji=app_emoji(self.bot, "repeat_all"),
            label="Rotacionar Fila",
            style=disnake.ButtonStyle.grey,
            custom_id="queue_rotate",
        )
        rotate_q.callback = self.invoke_command
        self.add_item(rotate_q)

        update_q = disnake.ui.Button(
            emoji=app_emoji(self.bot, "refresh"), label="Recarregar", style=disnake.ButtonStyle.grey
        )
        update_q.callback = self.update_q
        self.add_item(update_q)

    async def move_callback(self, inter: disnake.MessageInteraction):
        try:
            await check_cmd(self.bot.get_slash_command("move"), inter)
        except Exception as e:
            inter.bot.dispatch("interaction_player_error", inter, e)
            return

        await inter.response.send_modal(
            ViewModal(
                view=self,
                title="Mover música selecionada",
                custom_id="queue_move_modal",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Posição da fila:",
                        custom_id="queue_move_position",
                        max_length=4,
                        required=True,
                    ),
                ],
            )
        )

    async def modal_handler(self, inter: disnake.ModalInteraction):
        try:
            if inter.data.custom_id == "queue_move_modal":
                if not inter.text_values["queue_move_position"].isdigit():
                    await inter.send("Você deve usar um número válido...", ephemeral=True)
                    return

                try:
                    await check_cmd(self.bot.get_slash_command("move"), inter)
                except AttributeError:
                    pass

                player: LavalinkPlayer = self.bot.music.players[self.user.guild.id]

                move_func = None

                try:
                    player.queue.remove(self.current_track)
                    move_func = player.queue.insert
                except ValueError:
                    try:
                        player.queue_autoplay.remove(self.current_track)
                        move_func = player.queue_autoplay.insert
                    except ValueError:
                        pass

                if move_func:
                    move_func((int(inter.text_values["queue_move_position"]) or 1) - 1, self.current_track)
                    player.update = True

                self.update_pages(reset_page=False)

                if self.current_page > self.max_page:
                    self.current_page = 0

                self.update_embed()

                if self.message:
                    await inter.response.edit_message(embed=self.embed, view=self)
                else:
                    await inter.edit_original_message(embed=self.embed, view=self)

            else:
                await inter.send(f"Método ainda não implementado: {inter.data.custom_id}", ephemeral=True)

        except Exception as e:
            self.bot.dispatch("interaction_player_error", inter, e)

    def update_embed(self):
        self.embed.title = f"**Músicas da fila [Página: {self.current_page + 1} / {self.max_page + 1}]**"

        opts = []

        txt = ""

        for n, t in enumerate(self.track_pages[self.current_page]):
            duration = time_format(t.duration) if not t.is_stream else "🔴 Livestream"

            index = (self.max_items * self.current_page) + n + 1

            if self.current_track == t:
                txt += (
                    f"`╔{'=' * 50}`\n`║` **{index}º) {md_link(t.title, t.uri, code=False, limit=37)}**\n"
                    f"`║` {app_emoji(self.bot, 'history')}  **{duration}**"
                    + (f" - `Repetições: {t.track_loops}`" if t.track_loops else "")
                    + " **|** "
                    + (
                        f"{app_emoji(self.bot, 'user')} <@{t.requester}>"
                        if not t.autoplay
                        else f"{app_emoji(self.bot, 'like')} `Recomendada`"
                    )
                    + f"\n`╚{'=' * 50}`\n"
                )
            else:
                txt += (
                    f"`┌ {index})` {md_link(t.title, t.uri, limit=45)}\n"
                    f"`└` {app_emoji(self.bot, 'history')} `{duration}`"
                    + (f" - `Repetições: {t.track_loops}`" if t.track_loops else "")
                    + f" **|** "
                    + (
                        f"{app_emoji(self.bot, 'user')} <@{t.requester}>"
                        if not t.autoplay
                        else f"{app_emoji(self.bot, 'like')} `Recomendada`"
                    )
                    + "\n"
                )

            opts.append(
                disnake.SelectOption(
                    label=fix_characters(f"{index}. {t.single_title}", 47),
                    description=f"[{duration}]. {t.authors_string}"[:50],
                    value=f"queue_select_{t.unique_id}",
                    default=t == self.current_track,
                )
            )

        self.embed.description = txt
        self.select_options = opts
        self.embed.set_thumbnail(self.current_track.thumb)
        self.update_components()

    async def track_select_callback(self, interaction: disnake.MessageInteraction):
        track_id = interaction.values[0][13:]

        track = None

        try:
            player: LavalinkPlayer = self.bot.music.players[self.user.guild.id]
        except KeyError:
            self.stop()
            return

        for t in player.queue + player.queue_autoplay:
            if t.unique_id == track_id:
                track = t
                break

        if not track:
            await interaction.send(f'Música com id "{track_id}" não encontrada na fila do player...', ephemeral=True)
            return

        self.current_track = track
        self.update_embed()

        if self.message:
            await interaction.response.edit_message(embed=self.embed, view=self)
        else:
            await interaction.edit_original_message(embed=self.embed, view=self)

    async def invoke_command(self, interaction: disnake.MessageInteraction):
        try:
            player = self.bot.music.players[self.user.guild.id]
        except KeyError:
            await interaction.send("O player já foi finalizado...", ephemeral=True)
            self.stop()
            return

        update_inter = False

        try:
            if self.current_track is None:
                await interaction.send("Nenhuma música selecionada...", ephemeral=True)
                return

            if interaction.data.custom_id == "queue_skip":
                if player.current and player.current.unique_id == self.current_track.unique_id:
                    await check_cmd(self.bot.get_slash_command("seek"), interaction)
                    await player.seek(0)
                    player.set_command_log(
                        emoji="⏪", text=f"{interaction.author.mention} retrocedeu da música para: `0:00`"
                    )
                    player.update = True
                    await interaction.response.defer()
                    return
                else:
                    command = self.bot.get_slash_command("skip")
                    kwargs = {"query": f"{self.current_track.title} || ID > {self.current_track.unique_id}"}

            elif interaction.data.custom_id == "queue_rotate":
                command = self.bot.get_slash_command("rotate")
                kwargs = {"query": f"{self.current_track.title} || ID > {self.current_track.unique_id}"}
                update_inter = True

            else:
                await interaction.send(f"Comando não implementado: {interaction.data.custom_id}", ephemeral=True)
                return

            interaction.music_bot = self.bot
            interaction.music_guild = self.user.guild

            await check_cmd(command, interaction)
            await command(interaction, **kwargs)

            if update_inter:
                self.update_pages()
                self.update_embed()
                if self.message:
                    await interaction.response.edit_message(embed=self.embed, view=self)
                else:
                    await interaction.edit_original_message(embed=self.embed, view=self)

        except Exception as e:
            self.bot.dispatch("interaction_player_error", interaction, e)

    async def first(self, interaction: disnake.MessageInteraction):
        self.current_page = 0
        self.current_track = self.track_pages[self.current_page][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def back(self, interaction: disnake.MessageInteraction):
        if self.current_page == 0:
            self.current_page = self.max_page
        else:
            self.current_page -= 1
        self.current_track = self.track_pages[self.current_page][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def next(self, interaction: disnake.MessageInteraction):
        if self.current_page == self.max_page:
            self.current_page = 0
        else:
            self.current_page += 1
        self.current_track = self.track_pages[self.current_page][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def last(self, interaction: disnake.MessageInteraction):
        self.current_page = self.max_page
        self.current_track = self.track_pages[self.current_page][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def stop_interaction(self, interaction: disnake.MessageInteraction):
        await interaction.response.edit_message(content="Queue fechada", embed=None, view=None)
        self.stop()

    async def update_q(self, interaction: disnake.MessageInteraction):
        self.current_page = 0
        self.max_page = len(self.track_pages) - 1
        self.update_pages()
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def interaction_check(self, interaction: disnake.MessageInteraction):
        if interaction.author != self.user:
            await interaction.send(f"Apenas o membro {self.user.mention} pode interagir aqui.", ephemeral=True)
            return

        try:
            self.bot.music.players[self.user.guild.id]
        except KeyError:
            await interaction.response.edit_message(content="O player foi finalizado...", embed=None, view=None)
            self.stop()
            return

        return True


class EmbedPaginatorInteraction(disnake.ui.View):
    def __init__(self, user: disnake.Member, embeds: List[disnake.Embed], *, timeout=180, max_itens=25):
        super().__init__(timeout=timeout)
        self.user = user
        self.current_page = 0
        self.max_page = len(embeds) - 1
        self.inter: Optional[disnake.MessageInteraction] = None
        self.embeds = embeds
        self.load_components()

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.user.id == self.user.id:
            return True

        await interaction.send(f"Apenas {self.user.mention} pode interagir aqui.", ephemeral=True)

    def load_components(self):
        self.clear_items()

        back_button = disnake.ui.Button(emoji="⬅")
        if self.current_page == 0:
            back_button.disabled = True
        else:
            back_button.label = str(self.current_page)
        back_button.callback = self.back_callback
        self.add_item(back_button)

        next_button = disnake.ui.Button(emoji="➡")
        if self.current_page == self.max_page:
            next_button.disabled = True
            next_button.label = f"{self.current_page + 1} / {self.max_page + 1}"
        else:
            next_button.label = f"{self.current_page + 2} / {self.max_page + 1}"
        next_button.callback = self.next_callback
        self.add_item(next_button)

        button = disnake.ui.Button(label="Fechar", emoji="❌")
        button.callback = self.cancel_callback
        self.add_item(button)

    async def back_callback(self, interaction: disnake.MessageInteraction):
        self.inter = interaction
        if self.current_page == 0:
            self.current_page = self.max_page
        else:
            self.current_page -= 1
        self.load_components()
        kwargs = {}
        if self.embeds:
            kwargs["embed"] = self.embeds[self.current_page]
        await interaction.response.edit_message(view=self, **kwargs)

    async def next_callback(self, interaction: disnake.MessageInteraction):
        self.inter = interaction
        if self.current_page == self.max_page:
            self.current_page = 0
        else:
            self.current_page += 1
        self.load_components()
        kwargs = {}
        if self.embeds:
            kwargs["embed"] = self.embeds[self.current_page]
        await interaction.response.edit_message(view=self, **kwargs)

    async def cancel_callback(self, interaction: disnake.MessageInteraction):
        self.inter = interaction
        self.selected = False
        self.inter = interaction
        self.stop()

    async def callback(self, interaction: disnake.MessageInteraction):
        self.selected = interaction.data.values[0]
        self.inter = interaction
        self.stop()


class ButtonInteraction(disnake.ui.View):
    def __init__(self, user: disnake.Member, buttons: List[disnake.ui.Button], *, timeout=180):
        super().__init__(timeout=timeout)
        self.user = user
        self.selected = None
        self.current_page = 0
        self.inter = None
        self.embeds = []

        self.load_components(buttons)

    def load_components(self, buttons: List[disnake.ui.Button]):
        for b in buttons:
            b.callback = self.callback
            self.add_item(b)

        button = disnake.ui.Button(label="Cancelar", emoji="❌")
        button.callback = self.cancel_callback
        self.add_item(button)

    async def cancel_callback(self, interaction: disnake.MessageInteraction):
        self.selected = False
        self.inter = interaction
        self.stop()

    async def callback(self, interaction: disnake.MessageInteraction):
        self.selected = interaction.data.custom_id
        self.inter = interaction
        self.stop()


class SelectInteraction(disnake.ui.View):
    def __init__(self, user: disnake.Member, opts: List[disnake.SelectOption], *, timeout=180, max_itens=25):
        super().__init__(timeout=timeout)
        self.user = user
        self.selected = None
        self.item_pages = list(disnake.utils.as_chunks(opts, max_itens))
        self.current_page = 0
        self.max_page = len(self.item_pages) - 1
        self.inter = None
        self.embeds = []

        self.load_components()

    def load_components(self):
        self.clear_items()

        select_menu = disnake.ui.Select(placeholder="Selecione uma opção:", options=self.item_pages[self.current_page])
        select_menu.callback = self.callback
        self.add_item(select_menu)
        self.selected = self.item_pages[self.current_page][0].value

        if len(self.item_pages) > 1:
            back_button = disnake.ui.Button(emoji="⬅")
            if self.current_page == 0:
                back_button.disabled = True
            else:
                back_button.label = str(self.current_page)
            back_button.callback = self.back_callback
            self.add_item(back_button)

            next_button = disnake.ui.Button(emoji="➡")
            if self.current_page == self.max_page:
                next_button.disabled = True
                next_button.label = f"{self.current_page + 1} / {self.max_page + 1}"
            else:
                next_button.label = f"{self.current_page + 2} / {self.max_page + 1}"
            next_button.callback = self.next_callback
            self.add_item(next_button)

        button = disnake.ui.Button(label="Cancelar", emoji="❌")
        button.callback = self.cancel_callback
        self.add_item(button)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.user.id == self.user.id:
            return True

        await interaction.send(f"Apenas {self.user.mention} pode interagir aqui.", ephemeral=True)

    async def back_callback(self, interaction: disnake.MessageInteraction):
        if self.current_page == 0:
            self.current_page = self.max_page
        else:
            self.current_page -= 1
        self.load_components()
        kwargs = {}
        if self.embeds:
            kwargs["embed"] = self.embeds[self.current_page]
        await interaction.response.edit_message(view=self, **kwargs)

    async def next_callback(self, interaction: disnake.MessageInteraction):
        if self.current_page >= self.max_page:
            self.current_page = 0
        else:
            self.current_page += 1
        self.load_components()
        kwargs = {}
        if self.embeds:
            kwargs["embed"] = self.embeds[self.current_page]
        await interaction.response.edit_message(view=self, **kwargs)

    async def cancel_callback(self, interaction: disnake.MessageInteraction):
        self.selected = False
        self.inter = interaction
        self.stop()

    async def callback(self, interaction: disnake.MessageInteraction):
        self.selected = interaction.data.values[0]
        self.inter = interaction
        self.stop()


class AskView(disnake.ui.View):
    def __init__(self, *, ctx: Union[commands.Context, disnake.Interaction], timeout=None):
        super().__init__(timeout=timeout)
        self.selected = None
        self.ctx = ctx
        self.interaction_resp: Optional[disnake.MessageInteraction] = None

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.user != self.ctx.author:
            await interaction.send("Você não pode usar este botão!", ephemeral=True)
            return False

        return True

    @disnake.ui.button(label="Sim", emoji="✅")
    async def allow(self, button, interaction: disnake.MessageInteraction):
        self.selected = True
        self.interaction_resp = interaction
        self.stop()

    @disnake.ui.button(label="Não", emoji="❌")
    async def deny(self, button, interaction: disnake.MessageInteraction):
        self.selected = False
        self.interaction_resp = interaction
        self.stop()


class SelectBotVoice(disnake.ui.View):
    def __init__(
        self,
        inter: Union[
            disnake.ApplicationCommandInteraction, disnake.MessageInteraction, disnake.ModalInteraction, CustomContext
        ],
        guild: disnake.Guild,
        freebots: List[BotCore],
    ):
        super().__init__(timeout=45)
        self.inter = inter
        self.message: Optional[disnake.Message] = None
        self.voice_channel = inter.author.voice.channel
        self.guild = guild
        self.build_interactions(freebots)
        self.bot: Optional[BotCore] = None
        self.status = None

    def build_interactions(self, freebots: List[BotCore] = None):
        self.clear_items()

        bot_select_opts = []

        if freebots:
            bot_select_opts.extend(
                [disnake.SelectOption(label=b.user.display_name, value=f"bot_voice_{b.user.id}") for b in freebots]
            )

        else:
            for b in self.inter.bot.pool.get_guild_bots(self.guild.id):
                if not b.bot_ready:
                    continue

                guild = b.get_guild(self.inter.guild_id)

                if not guild:
                    continue

                player: LavalinkPlayer = b.music.players.get(self.inter.guild_id)

                if player and self.inter.author.id not in player.last_channel.voice_states:
                    continue

                bot_select_opts.append(disnake.SelectOption(label=b.user.display_name, value=f"bot_voice_{b.user.id}"))

        if not bot_select_opts:
            self.status = False
            self.stop()
            return

        bot_select = disnake.ui.Select(min_values=0, max_values=1, required=False, options=bot_select_opts)
        bot_select.callback = self.bot_select_callback
        self.add_item(bot_select)

        refresh_btn = disnake.ui.Button(label="Atualizar lista", emoji="🔄", style=disnake.ButtonStyle.blurple)
        refresh_btn.callback = self.reload_callback
        self.add_item(refresh_btn)

        cancel_btn = disnake.ui.Button(label="Cancelar", emoji="❌")
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    async def update_message(self):
        self.build_interactions()
        if self.message:
            await self.message.edit(view=self)
        else:
            await self.inter.edit_original_message(view=self)

    async def reload_callback(self, inter: disnake.MessageInteraction):
        self.build_interactions()
        await inter.response.edit_message(view=self)

    async def cancel_callback(self, inter: disnake.MessageInteraction):
        self.status = False
        self.inter = inter
        self.stop()

    async def bot_select_callback(self, inter: disnake.MessageInteraction):
        bot_id = int(inter.values[0][10:])

        try:
            bot = [
                b for b in self.inter.bot.pool.get_guild_bots(inter.guild_id) if b.bot_ready and b.user.id == bot_id
            ][0]
        except IndexError:
            await inter.send(f"<@{bot_id}> não está mais presente no servidor...", ephemeral=True)
            await self.update_message()
            return

        guild = bot.get_guild(inter.guild_id)

        if not guild:
            await inter.send(f"{bot.user.mention} não está mais no servidor...", ephemeral=True)
            await self.update_message()
            return

        player = bot.music.players.get(inter.guild_id)

        if player:
            try:
                vc = player.guild.me.voice.channel
            except AttributeError:
                vc = player.last_channel

            if not vc:
                await inter.send(
                    f"{bot.user.mention} está com player ativo mas não está conectado em um canal de voz...",
                    ephemeral=True,
                )
                await self.update_message()
                return

            if inter.author.id not in vc.voice_states:
                await inter.send(f"{bot.user.mention} já está em uso no canal {vc.mention}", ephemeral=True)
                await self.update_message()
                return

        inter.author = guild.get_member(inter.author.id)

        try:
            inter.music_guild = guild
            inter.music_bot = bot
        except AttributeError:
            pass

        self.bot = bot
        self.inter = inter
        self.guild = guild
        self.status = True
        self.stop()
