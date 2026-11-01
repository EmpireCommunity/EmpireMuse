# -*- coding: utf-8 -*-
import asyncio
import datetime
import logging
from copy import deepcopy

import disnake
from disnake.ext import commands

import empiremuse.wavelink as wavelink
from empiremuse.i18n import localized
from empiremuse.utils.client import BotCore
from empiremuse.utils.safe_error import HTTPStatusError, safe_error
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    is_dj,
)
from empiremuse.utils.music.converters import md_code
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
    native_sources,
)
from empiremuse.utils.others import (
    CustomContext,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.

logger = logging.getLogger("empiremuse.wavelink")


class NodesMixin:
    @check_voice()
    @has_player()
    @is_dj()
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.slash_command(description=localized("cmd.change_node.description"))
    @commands.contexts(guild=True)
    async def change_node(
        self,
        inter: disnake.ApplicationCommandInteraction,
        node: str = commands.Param(
            name=localized("cmd.change_node.opt.server.name"),
            description=localized("cmd.change_node.opt.server.description"),
        ),
    ):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        if node not in bot.music.nodes:
            raise GenericError(f"O servidor de música **{node}** não foi encontrado.")

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if node == player.node.identifier:
            raise GenericError(f"O player já está no servidor de música **{node}**.")

        await inter.response.defer(ephemeral=True)

        await player.change_node(node)

        player.native_yt = True

        embed = disnake.Embed(
            description=f"**O player foi migrado para o servidor de música:** {md_code(node)}",
            color=self.bot.get_color(player.guild.me),
        )

        try:
            if bot.user.id != self.bot.user.id:
                embed.set_footer(text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
        except AttributeError:
            pass

        player.set_command_log(
            text=f"{inter.author.mention} migrou o player para o servidor de música **{node}**", emoji="🌎"
        )

        player.update = True

        await inter.edit_original_message(embed=embed)

    @commands.command(aliases=["puptime"], description="Ver informações de tempo que o player está ativo no servidor.")
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def playeruptime(self, ctx: CustomContext):
        uptime_info = []
        for bot in self.bot.pool.get_guild_bots(ctx.guild.id):
            try:
                player = bot.music.players[ctx.guild.id]
                uptime_info.append(
                    f"**Bot:** {bot.user.mention}\n"
                    f"**Uptime:** <t:{player.uptime}:R>\n"
                    f"**Canal:** {player.guild.me.voice.channel.mention}"
                )
            except KeyError:
                continue

        if not uptime_info:
            raise GenericError("**Não há players ativos no servidor.**")

        await ctx.reply(
            embed=disnake.Embed(
                title="**Player Uptime:**",
                description="\n-----\n".join(uptime_info),
                color=self.bot.get_color(ctx.guild.me),
            ),
            fail_if_not_exists=False,
        )

    @commands.Cog.listener("on_wavelink_node_connection_closed")
    async def node_connection_closed(self, node: wavelink.Node):
        metrics = getattr(getattr(self.bot, "pool", None), "metrics", None)
        if metrics:
            metrics.error("node_unavailable")

        # Uma linha ERROR por transição: quando o último nó cai; o node_ready rearma para a próxima queda total.
        if not any(n.is_available for n in self.bot.music.nodes.values()) and not getattr(
            self.bot, "all_nodes_unavailable", False
        ):
            self.bot.all_nodes_unavailable = True
            logger.error("Todos os servidores de música estão indisponíveis.")

        try:
            self.bot.wavelink_node_reconnect_tasks[node.identifier].cancel()
        except Exception:
            pass

        self.bot.wavelink_node_reconnect_tasks[node.identifier] = self.bot.loop.create_task(self.node_reconnect(node))

    async def node_reconnect(self, node: wavelink.Node):
        retries = 0
        backoff = 7

        if ((dt_now := datetime.datetime.now(datetime.timezone.utc)) - node._retry_dt).total_seconds() < 7:
            node._retry_count += 1
        if node._retry_count >= 4:
            logger.error(
                "[%s / v%s] Reconexão cancelada.",
                node.identifier,
                node.version,
                extra={"aggregate_key": node.identifier},
            )
            node._retry_count = 0
            return
        else:
            node._retry_dt = dt_now

        logger.warning(
            "[%s / v%s] Conexão perdida - reconectando em %s segundos.",
            node.identifier,
            node.version,
            int(backoff),
            extra={"aggregate_key": node.identifier},
        )

        while True:
            if node.is_available:
                return

            for player in list(node.players.values()):
                try:
                    player._new_node_task.cancel()
                except Exception:
                    pass

                player._new_node_task = player.bot.loop.create_task(player._wait_for_new_node())

            if (
                self.bot.config["LAVALINK_RECONNECT_RETRIES"]
                and retries == self.bot.config["LAVALINK_RECONNECT_RETRIES"]
            ):
                logger.error(
                    "[%s] Todas as tentativas de reconectar falharam...",
                    node.identifier,
                    extra={"aggregate_key": node.identifier},
                )
                return

            await self.bot.wait_until_ready()

            try:
                async with self.bot.session.get(f"{node.rest_uri}/v4/info", timeout=45, headers=node.headers) as r:
                    if r.status == 200:
                        node.version = 4
                        node.update_info(await r.json())
                    elif r.status != 404:
                        raise HTTPStatusError(r.status)
                    else:
                        node.version = 3
                        node.info["sourceManagers"] = ["youtube", "soundcloud", "http"]

                await node._websocket._connect()
                return
            except Exception as e:
                error = safe_error(e)

            backoff *= 1.5
            if node.identifier != "LOCAL":
                logger.warning(
                    "Falha ao reconectar no servidor [%s] nova tentativa em %s segundos. Erro: %s",
                    node.identifier,
                    int(backoff),
                    error,
                    extra={"aggregate_key": node.identifier},
                )
            await asyncio.sleep(backoff)
            retries += 1

    def remove_provider(self, lst, queries: list):
        for q in queries:
            try:
                lst.remove(q)
            except Exception:
                continue

    def add_provider(self, lst, queries: list):
        for q in queries:
            if q in lst:
                lst.remove(q)
            lst.append(q)

    @commands.Cog.listener("on_wavelink_node_ready")
    async def node_ready(self, node: wavelink.Node):
        self.bot.all_nodes_unavailable = False
        logger.info("Servidor de música: [%s / v%s] está pronto para uso!", node.identifier, node.version)
        retries = 25
        while retries > 0:
            if not node._websocket.is_connected:
                return

            if not node.stats:
                await asyncio.sleep(5)
                retries -= 1
                continue

            if "deezer" not in node.info["sourceManagers"]:
                self.remove_provider(node.search_providers, ["dzsearch"])
                self.remove_provider(
                    node.partial_providers,
                    ["dzisrc:{isrc}", "dzsearch:{author} - {title}", "dzsearch:{title} - {author}"],
                )
                try:
                    node.native_sources.remove("deezer")
                except Exception:
                    pass
            elif "dzsearch" not in node.search_providers:
                node.native_sources.add("deezer")
                self.add_provider(node.search_providers, ["dzsearch"])
                self.add_provider(node.partial_providers, ["dzisrc:{isrc}", "dzsearch:{title} - {author}"])
            else:
                node.native_sources.add("deezer")

            if "tidal" not in node.info["sourceManagers"] or node.only_use_native_search_providers is True:
                self.remove_provider(node.search_providers, ["tdsearch"])
                self.remove_provider(
                    node.partial_providers, ["tdsearch:{author} - {title}", "tdsearch:{title} - {author}"]
                )
            elif "tdsearch" not in node.search_providers and node.only_use_native_search_providers is False:
                self.add_provider(node.search_providers, ["tdsearch"])
                self.add_provider(node.partial_providers, ["tdsearch:{title} - {author}"])

            if "applemusic" not in node.info["sourceManagers"] or node.only_use_native_search_providers is True:
                self.remove_provider(node.search_providers, ["amsearch"])
                self.remove_provider(
                    node.partial_providers, ["amsearch:{author} - {title}", "amsearch:{title} - {author}"]
                )
            elif "amsearch" not in node.search_providers and node.only_use_native_search_providers is False:
                self.add_provider(node.search_providers, ["amsearch"])
                self.add_provider(node.partial_providers, ["amsearch:{title} - {author}"])

            if "bandcamp" not in node.info["sourceManagers"]:
                self.remove_provider(node.search_providers, ["bcsearch"])
                self.remove_provider(
                    node.partial_providers, ["bcsearch:{author} - {title}", "bcsearch:{title} - {author}"]
                )

            if "spotify" not in node.info["sourceManagers"] or node.only_use_native_search_providers is True:
                self.remove_provider(node.search_providers, ["spsearch"])
                self.remove_provider(
                    node.partial_providers, ["spsearch:{author} - {title}", "spsearch:{title} - {author}"]
                )
            elif "spsearch" not in node.search_providers and node.only_use_native_search_providers is False:
                self.add_provider(node.search_providers, ["spsearch"])
                self.add_provider(node.partial_providers, ["spsearch:{title} - {author}"])

            if "youtube" not in node.info["sourceManagers"] and "ytsearch" not in node.original_providers:
                self.remove_provider(node.search_providers, ["ytsearch"])
                self.remove_provider(node.partial_providers, ['ytsearch:"{isrc}"', 'ytsearch:"{title} - {author}"'])
            elif "ytsearch" not in node.search_providers:
                if "ytsearch" in node.original_providers:
                    self.add_provider(node.search_providers, ["ytsearch"])
                    self.add_provider(node.partial_providers, ['ytsearch:"{isrc}"', 'ytsearch:"{title} - {author}"'])

            if "youtube" not in node.info["sourceManagers"] and "ytmsearch" not in node.original_providers:
                self.remove_provider(node.search_providers, ["ytmsearch"])
                self.remove_provider(node.partial_providers, ['ytmsearch:"{isrc}"', 'ytmsearch:"{title} - {author}"'])
            elif "ytmsearch" not in node.search_providers:
                if "ytmsearch" in node.original_providers:
                    self.add_provider(node.search_providers, ["ytmsearch"])
                    self.add_provider(node.partial_providers, ['ytmsearch:"{isrc}"', 'ytmsearch:"{title} - {author}"'])

            if "soundcloud" not in node.info["sourceManagers"]:
                self.remove_provider(node.search_providers, ["scsearch"])
                self.remove_provider(
                    node.partial_providers, ["scsearch:{author} - {title}", "scsearch:{title} - {author}"]
                )
            elif "scsearch" not in node.search_providers:
                self.add_provider(node.search_providers, ["scsearch"])
                self.add_provider(node.partial_providers, ["scsearch:{title} - {author}"])

            if "jiosaavn" not in node.info["sourceManagers"]:
                self.remove_provider(node.search_providers, ["jssearch"])
                # self.remove_provider(node.partial_providers, ["jssearch:{title} - {author}"])
            elif "jssearch" not in node.search_providers:
                self.add_provider(node.search_providers, ["jssearch"])
                # self.add_provider(node.partial_providers, ["jssearch:{title} {author}"])

            if node.stats.uptime < 600000:
                node.open()
            return

    async def connect_node(self, data: dict):
        if data["identifier"] in self.bot.music.nodes:
            node = self.bot.music.nodes[data["identifier"]]
            try:
                if not node._websocket.is_connected:
                    await node.connect()
            except AttributeError:
                pass
            return

        data = deepcopy(data)

        data["rest_uri"] = ("https" if data.get("secure") else "http") + f"://{data['host']}:{data['port']}"
        # data['user_agent'] = self.bot.pool.current_useragent
        search = data.pop("search", True)
        node_website = data.pop("website", "")
        region_label = data["region"] if "region" in data else None
        region = data.pop("region", "us_central")
        heartbeat = int(data.pop("heartbeat", 30))
        search_providers = data.pop("search_providers", None) or ["ytsearch", "scsearch"]
        info = data.pop("info", {})

        try:
            max_retries = int(data.pop("retries"))
        except TypeError, KeyError:
            max_retries = 1

        node = await self.bot.music.initiate_node(
            auto_reconnect=False, region=region, heartbeat=heartbeat, max_retries=max_retries, **data
        )
        node.info = info
        # Texto do dono (ou da lista externa) só para exibir; ``node.region`` segue sendo o valor interno de escolha de nó.
        node.region_label = region_label if isinstance(region_label, str) else None
        node.search = search
        node.website = node_website
        node.search_providers = search_providers
        node.original_providers = set(node.search_providers)
        node.partial_providers = []
        node.native_sources = deepcopy(native_sources)
        node.prefer_youtube_native_playback = data.pop("prefer_youtube_native_playback", True)
        node.only_use_native_search_providers = data.pop("only_use_native_search_providers", True)

        for p in node.search_providers:
            if p == "dzsearch":
                node.partial_providers.append("dzisrc:{isrc}")
                node.partial_providers.append("dzsearch:{title} - {author}")
            elif p == "tdsearch":
                node.partial_providers.append("tdsearch:{title} - {author}")
            elif p == "amsearch":
                node.partial_providers.append("amsearch:{title} - {author}")
            elif p == "spsearch":
                node.partial_providers.append("spsearch:{title} - {author}")
            elif p == "bcsearch":
                node.partial_providers.append("bcsearch:{title} - {author}")
            elif p == "ytsearch":
                node.partial_providers.append('ytsearch:"{isrc}"')
                node.partial_providers.append('ytsearch:"{title} - {author}"')
            elif p == "ytmsearch":
                node.partial_providers.append('ytmsearch:"{isrc}"')
                node.partial_providers.append('ytmsearch:"{title} - {author}"')
            elif p == "scsearch":
                node.partial_providers.append("scsearch:{title} - {author}")

        await node.connect(info=info)

    async def get_best_node(self, bot: BotCore = None):
        if not bot:
            bot = self.bot

        try:
            return sorted(
                [n for n in bot.music.nodes.values() if n.stats and n.is_available and n.available],
                key=lambda n: n.stats.players,
            )[0]

        except IndexError:
            try:
                node = bot.music.nodes["LOCAL"]
            except KeyError:
                pass
            else:
                if not node._websocket.is_connected:
                    await node.connect()
                return node

            raise GenericError("**Não há servidores de música disponível.**")
