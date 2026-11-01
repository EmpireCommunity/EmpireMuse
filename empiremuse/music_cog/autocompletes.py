# -*- coding: utf-8 -*-

import disnake

from empiremuse.utils.music.checks import (
    check_pool_bots,
)
from empiremuse.utils.music.errors import (
    GenericError,
    PoolException,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)

# Os comandos decorados moram nos outros mixins; este módulo importa os donos, e nenhum deles importa este.
from empiremuse.music_cog.nodes import NodesMixin
from empiremuse.music_cog.play_command import PlayCommandMixin
from empiremuse.music_cog.play_entry import PlayEntryMixin
from empiremuse.music_cog.queue_edit import QueueEditMixin
from empiremuse.music_cog.queue_group import QueueGroupMixin
from empiremuse.music_cog.skip import SkipMixin


class AutocompletesMixin:
    @QueueEditMixin.rotate.autocomplete("name")
    @QueueGroupMixin.move.autocomplete("name")
    @SkipMixin.skip.autocomplete("name")
    @SkipMixin.skipto.autocomplete("name")
    @QueueEditMixin.remove.autocomplete("name")
    async def queue_tracks(self, inter: disnake.ApplicationCommandInteraction, query: str):
        try:
            if not inter.author.voice:
                return
        except AttributeError:
            pass

        if not self.bot.bot_ready or not self.bot.is_ready():
            return [query]

        try:
            if not await check_pool_bots(inter, only_voiced=True):
                return
        except PoolException:
            pass
        except Exception:
            return

        try:
            player: LavalinkPlayer = inter.music_bot.music.players[inter.guild_id]
        except KeyError:
            return

        results = []

        count = 0

        for track in player.queue + player.queue_autoplay:
            if count == 20:
                break

            title = track.title.lower().split()

            query_words = query.lower().split()

            word_count = 0

            for query_word in query.lower().split():
                for title_word in title:
                    if query_word in title_word:
                        title.remove(title_word)
                        word_count += 1
                        break

            if word_count == len(query_words):
                results.append(f"{track.title[:81]} || ID > {track.unique_id}")
                count += 1

        return (
            results
            or [
                f"{track.title[:81]} || ID > {track.unique_id}"
                for n, track in enumerate(player.queue + player.queue_autoplay)
                if query.lower() in track.title.lower()
            ][:20]
        )

    @PlayEntryMixin.search.autocomplete("server")
    @PlayCommandMixin.play.autocomplete("server")
    @NodesMixin.change_node.autocomplete("server")
    async def node_suggestions(self, inter: disnake.Interaction, query: str):
        if not self.bot.bot_ready or not self.bot.is_ready():
            return []

        try:
            await check_pool_bots(inter)
            bot = inter.music_bot
        except GenericError:
            return
        except Exception:
            bot = inter.bot

        try:
            node = bot.music.players[inter.guild_id].node
        except KeyError:
            node = None

        if not query:
            return [n.identifier for n in bot.music.nodes.values() if n != node and n.available and n.is_available]

        return [
            n.identifier
            for n in bot.music.nodes.values()
            if n != node and query.lower() in n.identifier.lower() and n.available and n.is_available
        ]
