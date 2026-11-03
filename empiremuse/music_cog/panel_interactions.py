# -*- coding: utf-8 -*-
"""Jukebox (item 8, J1): o que o painel do canal da Jukebox faz além dos botões comuns.

- Autorrecuperação: mensagem do painel apagada (uma ou em massa) volta na hora (``JukeboxPanel.on_messages_deleted``).
- Botão da fila: abre, só para quem apertou e em resposta efêmera, a fila do player da PRÓPRIA call (``resolve_player``), 25 músicas por
  página; a escolhida vira ``skipto``.
- Select de gênero: abre, também efêmero, as playlists do servidor daquele gênero; a escolhida toca na call de quem pediu.

Nenhum ``custom_id`` leva bot, player ou nome: o player vem sempre de ``resolve_player`` e o gênero é um índice conferido de novo contra
a lista de agora.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import disnake
from disnake.ext import commands

from empiremuse.music_cog.play_guild_menu import GuildPlaylistSelect
from empiremuse.rules.i18n import fit
from empiremuse.rules.playlists import filter_by_genre, genres_of
from empiremuse.rules.jukebox_panel import GENRE_SELECT_ID, QUEUE_BUTTON_ID, genre_for_key
from empiremuse.utils.music.checks import resolve_player, update_attr
from empiremuse.utils.music.converters import fix_characters, time_format
from empiremuse.utils.music.interactions import SelectInteraction
from empiremuse.utils.server_playlists import safe_list

PROMPT_TIMEOUT = 60


def queue_options(queue: Sequence[Any]) -> list[disnake.SelectOption]:
    """Uma opção por música da fila, na ordem; o valor é o ``unique_id`` da faixa (a escolha é relida na fila de AGORA, nunca confiada)."""
    return [
        disnake.SelectOption(
            label=fix_characters(f"{n + 1}. {track.single_title}", 90),
            description=fix_characters(
                f"[{time_format(track.duration) if not track.is_stream else '🔴 Live'}] {track.authors_string}", 90
            ),
            value=str(track.unique_id),
        )
        for n, track in enumerate(queue)
    ]


ID_SEPARATOR = (
    " || ID > "  # o formato que o ``skipto`` já entende: casa o ``unique_id`` inteiro, não o começo do título
)


def queue_query_for(queue: Sequence[Any], unique_id: object) -> str | None:
    """O pedido de ``skipto`` para a faixa escolhida, achada pelo ``unique_id`` na fila de AGORA; ``None`` se ela já saiu da fila.

    Pela posição, uma fila que andou com a view aberta mandaria para outra música; pelo título, duas faixas que começam igual
    mandariam para a primeira.
    """
    track = next((t for t in queue if isinstance(unique_id, str) and str(t.unique_id) == unique_id), None)
    if track is None:
        return None
    return f"{str(track.title).replace(ID_SEPARATOR, ' ')}{ID_SEPARATOR}{track.unique_id}"


async def _say(flow: Any, interaction: disnake.MessageInteraction, **kwargs: Any) -> None:
    """Responde no fim de um fluxo: se o usuário clicou em algo da view (``flow.inter``: Cancelar, a escolha), é ESSA interação que se
    confirma; só no tempo esgotado se edita a mensagem efêmera original."""
    clicked = flow.inter
    if clicked is not None and not clicked.response.is_done():
        await clicked.response.edit_message(**kwargs)
    else:
        await interaction.edit_original_message(**kwargs)


class PanelInteractionsMixin:
    # ------------------------------------------------------------------ painel apagado

    @commands.Cog.listener("on_raw_message_delete")
    async def jukebox_panel_deleted(self, payload: disnake.RawMessageDeleteEvent) -> None:
        if payload.guild_id is not None and self.bot.pool.jukebox_panel is not None:
            await self.bot.pool.jukebox_panel.on_messages_deleted(payload.guild_id, (payload.message_id,))

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def jukebox_panel_bulk_deleted(self, payload: disnake.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is not None and self.bot.pool.jukebox_panel is not None:
            await self.bot.pool.jukebox_panel.on_messages_deleted(payload.guild_id, payload.message_ids)

    # ------------------------------------------------------------------ fila

    @commands.Cog.listener("on_button_click")
    async def jukebox_panel_queue(self, interaction: disnake.MessageInteraction) -> None:
        if interaction.data.custom_id != QUEUE_BUTTON_ID:
            return
        # Numa resposta de componente só ``with_message=True`` cria uma mensagem efêmera nova; sem isso o ``edit_original_message``
        # editaria o PAINEL (público). Confirma antes de ler o idioma (3 s).
        await interaction.response.defer(ephemeral=True, with_message=True)
        tr = await self.bot.pool.i18n.for_guild(interaction.guild)
        try:
            resolved = resolve_player(self.bot.pool, interaction.guild_id, interaction.author)
            if resolved is None:
                await interaction.edit_original_message(content=tr("player.panel.join_a_call"))
                return
            bot, player = resolved
            if not player.queue:
                await interaction.edit_original_message(content=tr("player.panel.queue.empty"))
                return
            view = SelectInteraction(user=interaction.author, opts=queue_options(player.queue), timeout=PROMPT_TIMEOUT)
            await interaction.edit_original_message(
                content=tr("player.panel.queue.prompt", count=len(player.queue)), view=view
            )
            await view.wait()
            if not view.inter or view.selected is False:
                await _say(view, interaction, content=tr("player.operation_cancelled.description"), view=None)
                return
            query = queue_query_for(player.queue, view.selected)
            if query is None:
                await _say(view, interaction, content=tr("player.panel.queue.changed"), view=None)
                return
            update_attr(view.inter, bot, bot.get_guild(interaction.guild_id))
            cog = bot.get_cog("Music")
            await cog.process_player_interaction(
                interaction=view.inter,
                command=bot.get_slash_command("skipto"),
                kwargs={"query": query, "case_sensitive": True},
            )
        except Exception as e:
            self.bot.dispatch("interaction_player_error", interaction, e)

    # ------------------------------------------------------------------ gênero

    @commands.Cog.listener("on_dropdown")
    async def jukebox_panel_genre(self, interaction: disnake.MessageInteraction) -> None:
        if interaction.data.custom_id != GENRE_SELECT_ID:
            return
        if not interaction.data.values:
            await interaction.response.defer()
            return
        await interaction.response.defer(
            ephemeral=True, with_message=True
        )  # ver o botão da fila: sem isso o painel seria editado
        tr = await self.bot.pool.i18n.for_guild(interaction.guild)
        try:
            if not interaction.user.voice:
                await interaction.edit_original_message(content=tr("player.you_must_join_a_voice_2"))
                return
            playlists = await safe_list(self.bot, interaction.guild_id)
            genres = genres_of(
                playlists
            )  # TODOS os gêneros de agora: o painel só mostra os primeiros, mas a chave pode ser de qualquer um
            genre = genre_for_key(
                genres, interaction.data.values[0]
            )  # a chave do gênero, não a posição: a lista pode ter mudado
            chosen = filter_by_genre(playlists, genre) if genre is not None else ()
            if not chosen:
                await interaction.edit_original_message(content=tr("player.panel.genre.none"))
                return
            view = GuildPlaylistSelect(interaction.author, self.bot, chosen, timeout=PROMPT_TIMEOUT)
            await interaction.edit_original_message(
                content=fit(tr("player.panel.genre.prompt", genre=genre), 1900), view=view
            )
            await view.wait()
            if not view.inter or view.selected is False:
                await _say(view, interaction, content=tr("player.operation_cancelled.description"), view=None)
                return
            kwargs = {
                "query": view.selected,  # já vem como ``> pin: <nome>``; o /play o resolve de novo no store
                "position": 0,
                "options": False,
                "manual_selection": True,
                "server": None,
                "force_play": "no",
            }
            cog = await self.request_executor(view.inter)
            await cog.play.callback(self=cog, inter=view.inter, **kwargs)
        except Exception as e:
            self.bot.dispatch("interaction_player_error", interaction, e)
