# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import datetime
import json
import platform
import logging
from copy import deepcopy
from itertools import cycle
from os import getpid
from random import shuffle
from typing import TYPE_CHECKING

import aiofiles
import disnake
import psutil
from aiohttp import ClientSession
from disnake.ext import commands

from empiremuse import paths
from empiremuse.i18n import localized
from empiremuse.rules.text import format_int, format_mib
from empiremuse.rules.bot_roles import Peer, invite_order, main_missing_notice, notifier
from empiremuse.utils import branding
from empiremuse.utils.branding import asset_url
from empiremuse.utils.db import DBModel, db_models
from empiremuse.utils.guild_ban import bans_of
from empiremuse.utils.music.checks import check_requester_channel
from empiremuse.utils.music.converters import time_format, URL_REG, md_bracketed, md_link, fix_characters
from empiremuse.utils.others import select_bot_pool, CustomContext, paginator
from empiremuse.utils.servers import CONTROLLER_KEYS
from empiremuse.utils.usage import usage_of
from empiremuse.version import image_version, running_from_image

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore


def remove_blank_spaces(d):
    for k, v in list(d.items()):
        new_k = k.strip()
        if new_k != k:
            d[new_k] = d.pop(k)

        if isinstance(v, str):
            new_v = v.strip()
            if new_v != v:
                d[new_k] = new_v
        elif isinstance(v, dict):
            remove_blank_spaces(v)


logger = logging.getLogger(__name__)


def can_send_welcome(channel, me) -> bool:
    """O aviso de novo servidor usa embeds: precisa de ``send_messages`` e ``embed_links`` no canal."""
    permissions = channel.permissions_for(me)
    return bool(permissions.send_messages and permissions.embed_links)


class Misc(commands.Cog):
    emoji = "🔰"
    name = "Diversos"
    desc_prefix = f"[{emoji} {name}] | "

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.task = self.bot.loop.create_task(self.presences())

    def placeholders(self, text: str):
        if not text:
            return ""

        try:
            text = text.replace("{owner}", str(self.bot.owner))
        except AttributeError:
            pass

        if [
            i
            for i in (
                "{players_count}",
                "{players_user_count}",
                "{players_count_allbotchannels}",
                "{players_count_allbotservers}",
            )
            if i in text
        ]:
            channels = set()
            guilds = set()
            users = set()
            player_count = 0

            for bot in self.bot.pool.bots:
                for player in bot.music.players.values():
                    if not player.auto_pause and not player.paused:
                        if bot == self.bot:
                            player_count += 1
                        try:
                            vc = player.guild.me.voice.channel
                        except AttributeError:
                            continue
                        channels.add(vc.id)
                        guilds.add(player.guild.id)
                        for u in vc.members:
                            if u.bot or u.voice.deaf or u.voice.self_deaf:
                                continue
                            users.add(u.id)

                if "{players_count}" in text:
                    if not player_count:
                        return
                    text = text.replace("{players_count}", str(player_count))

            if "{players_count_allbotchannels}" in text:
                if not channels:
                    return

                text = text.replace("{players_count_allbotchannels}", str(len(channels)))

            if "{players_count_allbotservers}" in text:
                if not guilds:
                    return

                text = text.replace("{players_count_allbotservers}", str(len(guilds)))

            if "{players_user_count}" in text:
                if not users:
                    return

                text = text.replace("{players_user_count}", str(len(users)))

        return (
            text.replace("{users}", format_int(len([m for m in self.bot.users if not m.bot])))
            .replace("{playing}", format_int(len(self.bot.music.players)))
            .replace("{guilds}", format_int(len(self.bot.guilds)))
            .replace(
                "{uptime}",
                time_format((disnake.utils.utcnow() - self.bot.uptime).total_seconds() * 1000, use_names=True),
            )
        )

    async def presences(self):
        try:
            activities = []

            for i in self.bot.config["LISTENING_PRESENCES"].split("||"):
                if i:
                    activities.append({"name": i, "type": "listening"})

            for i in self.bot.config["WATCHING_PRESENCES"].split("||"):
                if i:
                    activities.append({"name": i, "type": "watching"})

            for i in self.bot.config["PLAYING_PRESENCES"].split("||"):
                if i:
                    activities.append({"name": i, "type": "playing"})

            for i in self.bot.config["CUSTOM_STATUS_PRESENCES"].split("||"):
                if i:
                    activities.append({"name": i, "type": "custom_status"})

            for i in self.bot.config["STREAMING_PRESENCES"].split("|||"):
                if i:
                    try:
                        name, url = i.split("||")
                        activities.append({"name": name, "url": url.strip(" "), "type": "streaming"})
                    except Exception:
                        logger.exception("Falha ao processar uma atividade de presença")

            if not activities:
                return

            shuffle(activities)

            activities = cycle(activities)

            ignore_sleep = False

            await asyncio.sleep(120)

            while True:
                try:
                    if self.bot._presence_loop_started and ignore_sleep is False:
                        await asyncio.sleep(self.bot.config["PRESENCE_INTERVAL"])
                except AttributeError:
                    self.bot._presence_loop_started = True

                await self.bot.wait_until_ready()

                activity_data = next(activities)

                activity_name = self.placeholders(activity_data["name"])

                if not activity_name:
                    await asyncio.sleep(15)
                    ignore_sleep = True
                    continue

                ignore_sleep = False

                if activity_data["type"] == "listening":
                    activity = disnake.Activity(
                        type=disnake.ActivityType.listening,
                        name=activity_name,
                    )

                elif activity_data["type"] == "watching":
                    activity = disnake.Activity(
                        type=disnake.ActivityType.watching,
                        name=activity_name,
                    )

                elif activity_data["type"] == "streaming":
                    activity = disnake.Activity(
                        type=disnake.ActivityType.streaming, name=activity_name, url=activity_data["url"]
                    )

                elif activity_data["type"] == "playing":
                    activity = disnake.Game(name=activity_name)

                else:
                    activity = disnake.Activity(
                        name="customstatus",
                        type=disnake.ActivityType.custom,
                        state=activity_name,
                    )

                await self.bot.change_presence(activity=activity)

                await asyncio.sleep(self.bot.config["PRESENCE_INTERVAL"])

        except Exception:
            logger.exception("Falha no loop de presença do bot")

    async def reserve_join_notice(self, guild: disnake.Guild) -> None:
        """Reserva entrou num servidor SEM o principal: um aviso no canal de sistema (S-V10).

        Um por servidor no pool (a reserva de menor ``identifier`` que está lá), no máximo a cada 24 h, só no canal de sistema (sem cair
        para outro), com permissão de envio conferida, texto fixo, convite montado aqui e nenhuma menção.
        """
        pool = self.bot.pool
        main = pool.main_bot
        if main is None or main.get_guild(guild.id) is not None:
            return

        here = {b.identifier for b in pool.bots if b.is_reserve and b.get_guild(guild.id) is not None}
        if notifier(here | {self.bot.identifier}) != self.bot.identifier or guild.id in pool.join_notices:
            return

        url = self.bot.main_invite_url()
        channel = guild.system_channel
        if url is None or channel is None or not channel.permissions_for(guild.me).send_messages:
            return

        pool.join_notices[guild.id] = True  # antes de enviar: duas reservas entrando juntas não repetem o aviso
        try:
            await channel.send(
                main_missing_notice(main.user.name, url, self.bot.default_prefix),
                allowed_mentions=disnake.AllowedMentions.none(),
            )
        except Exception as exc:  # noqa: BLE001 - aviso é cortesia: falha vira log e não se repete por 24 h
            logger.warning("Aviso de servidor sem o principal não enviado [%s] (%s)", guild.id, type(exc).__name__)

    @commands.Cog.listener("on_guild_join")
    async def guild_add(self, guild: disnake.Guild):
        if bans := bans_of(self.bot):
            await bans.ensure_loaded()
            if bans.is_banned(guild.id):
                return  # a saída e o único log ficam com o GuildLog

        if (
            self.bot.is_reserve
        ):  # a reserva não tem comandos de barra: em vez da boas-vindas de "/", o aviso de servidor sem o principal
            await self.reserve_join_notice(guild)
            return

        bots_in_guild = []
        bots_outside_guild = []

        for bot in self.bot.pool.bots:
            if bot == self.bot:
                continue

            if not bot.bot_ready:
                continue

            if bot.user in guild.members:
                bots_in_guild.append(bot)
            else:
                bots_outside_guild.append(bot)

        components = (
            [disnake.ui.Button(custom_id="bot_invite", label="Precisa de mais bots de música? Clique aqui.")]
            if [b for b in self.bot.pool.bots if b.appinfo and b.appinfo.bot_public]
            else []
        )

        if cmd := self.bot.get_command("setup"):
            cmd_text = (
                f"Se desejar, use o comando **/{cmd.name}** para criar um canal dedicado pra pedir "
                "músicas sem comandos e deixar o music player fixo em um canal dedicado.\n\n"
            )
        else:
            cmd_text = ""

        if self.bot.config["SUPPORT_SERVER"]:
            support_server = f"Caso tenha alguma dúvida ou queira acompanhar as últimas novidades, você pode entrar no meu {md_link('servidor de suporte', self.bot.config['SUPPORT_SERVER'])}"
        else:
            support_server = ""

        if self.bot.default_prefix and not self.bot.config["INTERACTION_COMMAND_ONLY"]:
            guild_data = await self.bot.get_global_data(guild.id, db_name=DBModel.guilds)
            prefix = disnake.utils.escape_markdown(guild_data["prefix"], as_needed=True)
        else:
            prefix = ""

        image = asset_url("ui_rainbow_bar_2.gif")

        color = self.bot.get_color()

        send_video = ""

        try:
            channel = guild.system_channel if can_send_welcome(guild.system_channel, guild.me) else None
        except AttributeError:
            channel = None

        if not channel:
            if guild.me.guild_permissions.view_audit_log:
                async for entry in guild.audit_logs(action=disnake.AuditLogAction.integration_create, limit=50):
                    if entry.target.application_id == self.bot.user.id:
                        embeds = []

                        embeds.append(
                            disnake.Embed(
                                color=color,
                                description=f"Olá! Agradeço muito por ter me adicionado no servidor: **{guild.name}** :)",
                            ).set_image(url=image)
                        )

                        embeds.append(
                            disnake.Embed(
                                color=color,
                                description=f"Para ver todos os meus comandos use barra (**/**) no servidor "
                                f"**{guild.name}**",
                            ).set_image(url=image)
                        )

                        if prefix:
                            prefix_msg = f"Meu prefixo no servidor **{guild.name}** é: **{prefix}**"
                        else:
                            prefix = self.bot.default_prefix
                            prefix_msg = f"Meu prefixo padrão é **{prefix}**"

                        embeds.append(
                            disnake.Embed(
                                color=color,
                                description=f"Também tenho comandos de texto por prefixo. {prefix_msg} (minha menção "
                                f"também funciona como prefixo). Pra ver todos os meus comandos de texto "
                                f"use **{prefix}help** em um canal do servidor **{guild.name}**. "
                                f"Caso queira alterar meu prefixo use o comando **{prefix}setprefix** "
                                f"(você pode ter um prefixo pessoal usando o comando "
                                f"**{prefix}setmyprefix**).",
                            ).set_image(url=image)
                        )

                        if bots_in_guild:
                            msg = (
                                f"Notei que há outros bots no servidor **{guild.name}** no qual sou compatível com "
                                f"o sistema de multi-voice: {', '.join(b.user.mention for b in bots_in_guild)}\n\n"
                                f"Ao usar usar os comandos de música (ex: play) sem um dos bots conectado no canal, "
                                "será usado um dos bots que estiver livre no servidor."
                            )

                            if not self.bot.pool.config.get("MULTIVOICE_VIDEO_DEMO_URL"):
                                embeds.append(disnake.Embed(color=color, description=msg).set_image(url=image))

                            else:
                                send_video = msg

                        elif bots_outside_guild and self.bot.config.get("MULTIVOICE_VIDEO_DEMO_URL"):
                            send_video = (
                                "**Caso tenha demanda no seu servidor você também pode adicionar mais bots de músicas extras.\n"
                                "Todos os bots compartilham o mesmo prefixo e comando de barra o que descarta a necessidade "
                                f"de ficar decorando prefixos e comandos de barra de cada bot individualmente.\n\n"
                                f"Confira o {md_link('vídeo', self.bot.config['MULTIVOICE_VIDEO_DEMO_URL'], code=False)} demonstrando o uso de multi-bot na prática.**"
                            )

                        if support_server:
                            embeds.append(disnake.Embed(color=color, description=support_server).set_image(url=image))

                        try:
                            await entry.user.send(embeds=embeds, components=components)
                            if send_video:
                                await asyncio.sleep(1)
                                await entry.user.send(send_video)
                            return
                        except disnake.Forbidden:
                            pass
                        except Exception:
                            logger.exception("Falha ao enviar a mensagem de boas-vindas")
                        break

        if not channel:
            for c in (guild.public_updates_channel, guild.rules_channel):
                if c and can_send_welcome(c, guild.me):
                    channel = c
                    break

            if not channel:
                return

        embeds = [
            disnake.Embed(
                color=color,
                description="Olá! Para ver todos os meus comandos use barra (**/**)\n"
                "`Nota: Caso os comandos estejam aparecendo no seu servidor,"
                "talvez o mesmo tenha atingido o limite de bots com comandos de barra "
                "registrados (caso tenha mais de 50 integrações/apps no seu servidor).`",
            ).set_image(url=image)
        ]

        if prefix:
            prefix_msg = f"Meu prefixo no servidor é: **{prefix}**"
        else:
            prefix = self.bot.default_prefix
            prefix_msg = f"Meu prefixo padrão é **{prefix}**"

        embeds.append(
            disnake.Embed(
                color=color,
                description=f"Também tenho comandos de texto por prefixo. {prefix_msg} (minha menção "
                f"também funciona como prefixo). Pra ver todos os meus comandos de texto use "
                f"**{prefix}help**. Caso queira alterar meu prefixo use o comando **{prefix}setprefix** "
                f"(você pode ter um prefixo pessoal usando o comando **{prefix}setmyprefix**).",
            ).set_image(url=image)
        )

        if bots_in_guild:
            msg = (
                f"Notei que há outros bots no servidor **{guild.name}** no qual sou compatível com "
                f"o sistema de multi-voice: {', '.join(b.user.mention for b in bots_in_guild)}\n\n"
                f"Ao usar usar os comandos de música (ex: play) sem um dos bots conectado no canal, será usado um "
                f"dos bots que estiver livre no servidor."
            )

            if not self.bot.config.get("MULTIVOICE_VIDEO_DEMO_URL"):
                embeds.append(disnake.Embed(color=color, description=msg).set_image(url=image))
            else:
                send_video = msg

        elif bots_outside_guild and self.bot.config.get("MULTIVOICE_VIDEO_DEMO_URL"):
            send_video = (
                "**Caso tenha demanda no seu servidor você também pode adicionar mais bots de músicas extras.\n"
                "Todos os bots compartilham o mesmo prefixo e comando de barra o que descarta a necessidade "
                f"de ficar decorando prefixos e comandos de barra de cada bot individualmente.\n\n"
                f"Confira o {md_link('vídeo', self.bot.config['MULTIVOICE_VIDEO_DEMO_URL'], code=False)} demonstrando o uso de multi-bot na prática.**"
            )

        embeds.append(disnake.Embed(color=color, description=cmd_text).set_image(url=image))

        if support_server:
            embeds.append(disnake.Embed(color=color, description=support_server).set_image(url=image))

        kwargs = {"delete_after": 60} if channel == guild.rules_channel else {"delete_after": 300}

        timestamp = int((disnake.utils.utcnow() + datetime.timedelta(seconds=kwargs["delete_after"])).timestamp())

        embeds[-1].description += f"\nEssa mensagem será deletada automaticamente <t:{timestamp}:R>"

        try:
            await channel.send(embeds=embeds, components=components, **kwargs)
            if send_video:
                if "delete_after" in kwargs:
                    kwargs["delete_after"] = 600
                await asyncio.sleep(1)
                await channel.send(
                    f"{send_video}\n\nConfira o **{md_link('vídeo', self.bot.config['MULTIVOICE_VIDEO_DEMO_URL'], code=False)}** demonstrando essa funcionalidade.",
                    **kwargs,
                )
        except disnake.Forbidden:
            logger.info(
                "Sem permissão para enviar o aviso de novo servidor no canal %s do servidor %s.", channel.id, guild.id
            )
        except Exception as e:
            logger.warning(
                "Falha ao enviar o aviso de novo servidor no canal %s (%s: %s)", channel.id, type(e).__name__, e
            )

    about_cd = commands.CooldownMapping.from_cooldown(1, 5, commands.BucketType.member)

    @commands.command(
        name="about",
        aliases=["sobre", "info", "botinfo"],
        description="Exibir informações sobre mim.",
        cooldown=about_cd,
    )
    async def about_legacy(self, ctx: CustomContext):
        await self.about.callback(self=self, interaction=ctx)

    @commands.slash_command(
        description=localized("cmd.about.description"), cooldown=about_cd, extras={"allow_private": True}
    )
    @commands.contexts(guild=True)
    async def about(self, interaction: disnake.ApplicationCommandInteraction):
        inter, bot = await select_bot_pool(interaction, first=True)

        if not bot:
            return

        await inter.response.defer(ephemeral=True)

        try:
            lavalink_ram = psutil.Process(self.bot.pool.lavalink_instance.pid).memory_info().rss
        except Exception:
            lavalink_ram = 0

        python_ram = psutil.Process(getpid()).memory_info().rss

        ram_msg = f"> 🖥️ **⠂RAM do bot (processo Python):** `{format_mib(python_ram)}`\n"

        if lavalink_ram:
            ram_msg += (
                f"> 🌋 **⠂RAM do Lavalink (processo Java inteiro):** `{format_mib(lavalink_ram)}`\n"
                f"> 🖥️ **⠂RAM total (bot + Lavalink):** `{format_mib(python_ram + lavalink_ram)}`\n"
            )

        guild = bot.get_guild(inter.guild_id) or inter.guild

        try:
            color = bot.get_color(inter.guild.me if inter.guild else guild.me)
        except Exception:
            color = bot.get_color()

        embed = disnake.Embed(description="", color=color)

        active_players_other_bots = 0
        inactive_players_other_bots = 0
        paused_players_other_bots = 0

        all_guilds_ids = set()

        allbots = self.bot.pool.get_all_bots()

        for b in allbots:
            for g in b.guilds:
                all_guilds_ids.add(g.id)

        guilds_size = len(all_guilds_ids)

        public_bot_count = 0
        private_bot_count = 0

        users = set()
        bots = set()
        listeners = set()

        user_count = 0
        bot_count = 0

        botpool_ids = [b.user.id for b in allbots]

        node_data = {}
        nodes_available = set()
        nodes_unavailable = set()

        for user in bot.users:
            if user.bot:
                bot_count += 1
            else:
                user_count += 1

        for b in allbots:
            for user in b.users:
                if user.id in botpool_ids:
                    continue

                if user.bot:
                    bots.add(user.id)
                else:
                    users.add(user.id)

            for n in b.music.nodes.values():
                if n.version == 0:
                    continue

                identifier = f"{n.identifier} (v{n.version})"

                if not identifier in node_data:
                    node_data[identifier] = {"total": 0, "available": 0, "website": n.website}

                node_data[identifier]["total"] += 1

                if n.is_available:
                    node_data[identifier]["available"] += 1

            for p in b.music.players.values():
                if not p.last_channel:
                    continue

                if p.auto_pause or not p.current:
                    inactive_players_other_bots += 1

                elif p.paused:
                    try:
                        if any(m for m in p.last_channel if not m.bot):
                            paused_players_other_bots += 1
                            continue
                    except AttributeError, TypeError:
                        pass
                    inactive_players_other_bots += 1

                else:
                    active_players_other_bots += 1
                    for u in p.last_channel.members:
                        if u.bot or u.voice.deaf or u.voice.self_deaf:
                            continue
                        listeners.add(u.id)

            if not b.appinfo or not b.appinfo.bot_public:
                private_bot_count += 1
            else:
                public_bot_count += 1

        for identifier, data in node_data.items():
            if data["available"] > 0:
                if data["website"]:
                    nodes_available.add(
                        f"> {md_link(f'✅⠂{identifier}', data['website'])} `[{data['available']}/{data['total']}]`"
                    )
                else:
                    nodes_available.add(f"> `✅⠂{identifier} [{data['available']}/{data['total']}]`")
            else:
                nodes_unavailable.add(f"> `❌⠂{identifier}`")

        node_txt_final = "\n".join(nodes_available)

        if node_txt_final:
            node_txt_final += "\n"
        node_txt_final += "\n".join(nodes_unavailable)

        if len(allbots) < 2:
            embed.description += (
                "### Estatíticas (bot atual):\n"
                f"> 🏙️ **⠂Servidor{'es'[: (svcount := len(bot.guilds)) ^ 1]}:** `{format_int(svcount)}`\n"
                f"> 👥 **⠂Usuário{'s'[: user_count ^ 1]}:** `{format_int(user_count)}`\n"
            )

            if bot_count:
                embed.description += f"> 🤖 **⠂Bot{'s'[: bot_count ^ 1]}:** `{format_int(bot_count)}`\n"

        else:
            embed.description += "### Estatísticas (totais em todos os bots):\n"

            if public_bot_count:
                embed.description += (
                    f"> 🤖 **⠂Bot{(s := 's'[: public_bot_count ^ 1])} público{s}:** `{format_int(public_bot_count)}`\n"
                )

            if private_bot_count:
                embed.description += f"> 🤖 **⠂Bot{(s := 's'[: private_bot_count ^ 1])} privado{s}:** `{format_int(private_bot_count)}`\n"

            embed.description += f"> 🏙️ **⠂Servidor{'es'[: guilds_size ^ 1]}:** `{format_int(guilds_size)}`\n"

            if users_amount := len(users):
                embed.description += f"> 👥 **⠂Usuário{'s'[: users_amount ^ 1]}:** `{format_int(users_amount)}`\n"

            if bots_amount := len(bots):
                embed.description += f"> 🤖 **⠂Bot{'s'[: bots_amount ^ 1]}:** `{format_int(bots_amount)}`\n"

        embed.description += "### Outras informações:\n"

        if active_players_other_bots:
            embed.description += f"> ▶️ **⠂Player{(s := 's'[: active_players_other_bots ^ 1])} ativo{s}:** `{format_int(active_players_other_bots)}`\n"

        if paused_players_other_bots:
            embed.description += f"> ⏸️ **⠂Player{'s'[: paused_players_other_bots ^ 1]} em pausa:** `{format_int(paused_players_other_bots)}`\n"

        if inactive_players_other_bots:
            embed.description += f"> 💤 **⠂Player{(s := 's'[: inactive_players_other_bots ^ 1])} inativo{s}:** `{format_int(inactive_players_other_bots)}`\n"

        if listeners:
            embed.description += f"> 🎧 **⠂Ouvinte{'s'[: (lcount := len(listeners)) ^ 1]} atua{'is'[: lcount ^ 1] or 'l'}:** `{format_int(lcount)}`\n"

        if bot.pool.commit:
            embed.description += f"> 📥 **⠂Commit atual:** {md_link(bot.pool.commit[:7], f'{bot.pool.remote_git_url}/commit/{bot.pool.commit}')}\n"

        if (version := image_version()) is not None:
            version_text = md_link(version, f"{branding.REPO_URL}/releases/tag/v{version}")
        else:
            version_text = "`dev`" if running_from_image() else ""

        if version_text:
            embed.description += f"> 🏷️ **⠂Versão do EmpireMuse:** {version_text}\n"

        embed.description += (
            f"> 🐍 **⠂Versão do Python:** `{platform.python_version()}`\n"
            f"> 📦 **⠂Versão do Disnake:** `{disnake.__version__}`\n"
            f"> 📶 **⠂Latencia:** `{round(bot.latency * 1000)}ms`\n"
            f"{ram_msg}"
            f"> ⏰ **⠂Uptime:** <t:{int(bot.uptime.timestamp())}:R>\n"
        )

        if not bot.config["INTERACTION_COMMAND_ONLY"]:
            guild_data = await bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)

            if guild_data["prefix"]:
                embed.description += f"> ⌨️ **⠂Prefixo do servidor:** `{disnake.utils.escape_markdown(guild_data['prefix'], as_needed=True)}`\n"
            else:
                embed.description += (
                    f"> ⌨️ **⠂Prefixo padrão:** `{disnake.utils.escape_markdown(bot.default_prefix, as_needed=True)}`\n"
                )

            user_data = await bot.get_global_data(inter.author.id, db_name=DBModel.users)

            if user_data["custom_prefix"]:
                embed.description += f"> ⌨️ **⠂Seu prefixo de usuário:** `{disnake.utils.escape_markdown(user_data['custom_prefix'], as_needed=True)}`\n"

        links = md_bracketed(md_link("Source", branding.REPO_URL))

        if bot.config["SUPPORT_SERVER"]:
            links = f"{md_bracketed(md_link('Suporte', bot.config['SUPPORT_SERVER']))}  **|** {links}"

        embed.description += f"> 🌐 **⠂**{links}\n"
        embed.description += (
            f"> 💜 **⠂**Baseado no {md_link(branding.UPSTREAM_NAME, branding.UPSTREAM_URL, code=False)} "
            f"de {branding.UPSTREAM_AUTHOR} (GPL-2.0)\n"
        )

        try:
            owner = bot.appinfo.team.owner
        except AttributeError:
            owner = bot.appinfo.owner

        if node_txt_final:
            embed.description += f"### Servidores de música (Lavalink Servers):\n{node_txt_final}"

        try:
            avatar = owner.avatar.with_static_format("png").url
        except AttributeError:
            avatar = owner.default_avatar.with_static_format("png").url

        embed.set_footer(icon_url=avatar, text=f"Dono(a): {owner} [{owner.id}]")

        components = (
            [disnake.ui.Button(custom_id="bot_invite", label="Me adicione no seu servidor")]
            if [b for b in self.bot.pool.bots if b.appinfo and (b.appinfo.bot_public or await b.is_owner(inter.author))]
            else None
        )

        try:
            await inter.edit_original_message(embed=embed, components=components)
        except AttributeError, disnake.InteractionNotEditable:
            try:
                await inter.response.edit_message(embed=embed, components=components)
            except Exception:
                await inter.send(embed=embed, ephemeral=True, components=components)

    @commands.Cog.listener("on_button_click")
    async def invite_button(self, inter: disnake.MessageInteraction, is_command=False):
        if not is_command and inter.data.custom_id != "bot_invite":
            return

        bots_invites = []
        bots_in_guild = []

        guild = None

        if inter.guild_id:
            guild = inter.guild
        else:
            for bot in self.bot.pool.bots:
                if guild := bot.get_guild(inter.guild_id):
                    break

        pool_bots = [b for b in self.bot.pool.bots if b.user is not None]
        if guild:  # no servidor: só quem falta, na ordem de convite (o principal primeiro, depois as reservas)
            by_id = {b.user.id: b for b in pool_bots}
            order = invite_order([Peer(b.identifier, b.user.id, b.is_main) for b in pool_bots], ())
            pool_bots = [by_id[peer.bot_id] for peer in order]
        else:
            pool_bots = sorted(pool_bots, key=lambda b: len(b.guilds))

        for bot in pool_bots:
            try:
                if not bot.appinfo.bot_public and not await bot.is_owner(inter.author):
                    continue
            except Exception:
                continue

            kwargs = {}

            invite = md_link(
                bot.user.name,
                disnake.utils.oauth_url(
                    bot.user.id,
                    permissions=disnake.Permissions(bot.config["INVITE_PERMISSIONS"]),
                    scopes=("bot",),
                    **kwargs,
                ),
            )

            if bot.appinfo.flags.gateway_message_content_limited:
                invite += f" `[{len(bot.guilds)}/100]`"
            else:
                invite += f" `[{len(bot.guilds)}]`"

            if guild and bot.user in guild.members:
                if inter.author.guild_permissions.manage_guild:
                    bots_in_guild.append(invite)
            else:
                bots_invites.append(invite)

        txt = ""

        if bots_invites:
            txt += "## Bots de música disponíveis:\n"
            for i in disnake.utils.as_chunks(bots_invites, 2):
                txt += " | ".join(i) + "\n"
            txt += "\n"

        if bots_in_guild:
            txt += "## Bots de música que já estão no servidor atual:\n"
            for i in disnake.utils.as_chunks(bots_in_guild, 2):
                txt += " | ".join(i) + "\n"

        if not txt:
            await inter.send(
                embed=disnake.Embed(
                    colour=self.bot.get_color(inter.guild.me if inter.guild else guild.me if guild else None),
                    description="## Não há bots públicos disponível...",
                ),
                ephemeral=True,
            )
            return

        color = self.bot.get_color(inter.guild.me if inter.guild else guild.me if guild else None)

        embeds = [
            disnake.Embed(
                colour=self.bot.get_color(inter.guild.me if inter.guild else guild.me if guild else None),
                description=p,
                color=color,
            )
            for p in paginator(txt)
        ]

        await inter.send(embeds=embeds, ephemeral=True)

    @commands.command(
        name="invite",
        aliases=["convidar"],
        description="Exibir meu link de convite para você me adicionar no seu servidor.",
    )
    async def invite_legacy(self, ctx):
        await self.invite.callback(self=self, inter=ctx)

    @commands.slash_command(
        description=localized("cmd.invite.description"),
        extras={"allow_private": True},
    )
    @commands.contexts(guild=True)
    async def invite(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)

        await self.invite_button(inter, is_command=True)

    @commands.user_command(name="Avatar")
    async def avatar(self, inter: disnake.UserCommandInteraction):
        user = inter.target

        guild = None

        bot = self.bot

        for b in self.bot.pool.get_guild_bots(inter.guild_id):
            if guild := b.get_guild(inter.guild_id):
                bot = b
                break

        if not guild:
            user = await bot.fetch_user(user.id)

            user_avatar_url = user.display_avatar.replace(static_format="png", size=512).url

            if user_banner_url := user.banner:
                user_banner_url = user.banner.replace(static_format="png", size=4096).url

            guild_avatar_url = None
            guild_banner_url = None

        else:
            async with self.bot.session.get(
                f"https://discord.com/api/v10/guilds/{inter.guild_id}/members/{user.id}",
                headers={"Authorization": f"Bot {bot.http.token}"},
            ) as r:
                data = await r.json()

            user_avatar_url = user.display_avatar.replace(static_format="png", size=512).url

            if user_banner_url := data["user"].get("banner"):
                user_banner_url = (
                    f"https://cdn.discordapp.com/banners/{user.id}/{user_banner_url}."
                    + ("gif" if user_banner_url.startswith("a_") else "png")
                    + "?size=4096"
                )

            if guild_avatar_url := data.get("avatar"):
                guild_avatar_url = (
                    f"https://cdn.discordapp.com/guilds/{inter.guild_id}/users/{user.id}/avatars/{guild_avatar_url}."
                    + ("gif" if guild_avatar_url.startswith("a_") else "png")
                    + "?size=512"
                )

            if guild_banner_url := data.get("banner"):
                guild_banner_url = (
                    f"https://cdn.discordapp.com/guilds/{inter.guild_id}/users/{user.id}/banners/{guild_banner_url}."
                    + ("gif" if guild_banner_url.startswith("a_") else "png")
                    + "?size=4096"
                )

        embeds = []

        requester = inter.author.display_avatar.with_static_format("png").url

        color = self.bot.get_color()

        if guild_avatar_url:
            embeds.append(
                disnake.Embed(
                    description=f"{user.mention} **{md_link('avatar (server)', guild_avatar_url, code=False)}**",
                    color=color,
                ).set_image(url=guild_avatar_url)
            )

        if guild_banner_url:
            embeds.append(
                disnake.Embed(
                    description=f"{user.mention} **{md_link('banner (server)', guild_banner_url, code=False)}**",
                    color=color,
                ).set_image(url=guild_banner_url)
            )

        embeds.append(
            disnake.Embed(
                description=f"{user.mention} **{md_link('avatar (user)', user_avatar_url, code=False)}**", color=color
            ).set_image(url=user_avatar_url)
        )

        if user_banner_url:
            embeds.append(
                disnake.Embed(
                    description=f"{user.mention} **{md_link('banner (user)', user_banner_url, code=False)}**",
                    color=color,
                ).set_image(url=user_banner_url)
            )

        if inter.user.id != user.id:
            embeds[-1].set_footer(text=f"Solicitado por: {inter.author}", icon_url=requester)

        await inter.send(embeds=embeds, ephemeral=True)

    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default)
    @commands.command(
        hidden=True,
        description="Comando temporário para corrigir favoritos com espaços em branco "
        "que ocasionam erros em algumas situações.",
    )
    async def fixfavs(self, ctx: CustomContext):
        backup_dir = paths.RUNTIME_DIR / "local_database" / "fixfavs_backup"
        if not backup_dir.is_dir():
            backup_dir.mkdir(parents=True, exist_ok=True)

        async with ctx.typing():
            # os links fixos dos servidores agora são as playlists do documento único (item 17): só os favoritos pessoais aqui
            documents = self.bot.pool.database.iter_data(collection="global", db_name=DBModel.users)

            async for data in self._backup_each(documents, backup_dir / "user_favs.json"):
                remove_blank_spaces(data["fav_links"])
                await self.bot.update_global_data(id_=data["_id"], data=data, db_name=DBModel.users)

            await ctx.send("os favoritos foram corrigidos com sucesso!")

    @staticmethod
    async def _backup_each(documents, path):
        """Entrega cada documento do cursor DEPOIS de gravar no backup (lista JSON) como ele estava; sem montar a lista toda."""
        async with aiofiles.open(path, "w") as f:
            await f.write("[")
            first = True
            try:
                async for data in documents:
                    await f.write(("" if first else ",") + "\n" + json.dumps(data, indent=4))
                    first = False
                    yield data
            finally:
                await f.write("\n]")

    async def cog_check(self, ctx):
        return await check_requester_channel(ctx)

    def cog_unload(self):
        try:
            self.task.cancel()
        except Exception:
            pass


class GuildLog(commands.Cog):
    def __init__(self, bot: BotCore):
        self.bot = bot
        self.hook_url: str = ""
        self._roster_tasks: set[asyncio.Task] = set()

        if bot.config["BOT_ADD_REMOVE_LOG"]:
            if URL_REG.match(bot.config["BOT_ADD_REMOVE_LOG"]):
                self.hook_url = bot.config["BOT_ADD_REMOVE_LOG"]
            else:
                logger.warning("URL do webhook inválido (para envio de logs ao adicionar/remover bot).")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: disnake.Guild):
        banned = (bans := bans_of(self.bot)) is not None and bans.is_banned(guild.id)

        if not banned:
            logger.info("Bot %s foi removido(a) do servidor [%s]", self.bot.user.name, guild.id)
            if usage := usage_of(self.bot):
                usage.record_guild("leave", guild, bot=self.bot)

        try:
            await self.bot.music.players[guild.id].destroy()
        except KeyError:
            pass
        except Exception:
            logger.exception("Falha ao tratar a remoção do bot do servidor")

        if not banned:
            self.roster_changed(guild.id)

        if banned or not self.hook_url:
            return

        try:
            await self.send_hook(guild, title="Me removeram do servidor", color=disnake.Color.red())
        except Exception:
            logger.exception("Falha ao enviar o aviso de remoção do servidor")

        await self.bot.update_appinfo()

    def cog_unload(self) -> None:
        for task in list(self._roster_tasks):
            task.cancel()

    def roster_changed(self, guild_id: int) -> None:
        """Um bot entrou ou saiu do servidor: se o bot do painel do canal de pedidos mudou, o novo reenvia o painel (em segundo plano)."""
        panel = getattr(self.bot.pool, "request_panel", None)
        if panel is None:
            return
        task = self.bot.loop.create_task(self._roster_changed(panel, guild_id))
        self._roster_tasks.add(task)
        task.add_done_callback(self._roster_tasks.discard)

    @staticmethod
    async def _roster_changed(panel, guild_id: int) -> None:
        try:
            await panel.on_roster_change(guild_id)
        except Exception as exc:  # noqa: BLE001 - o painel é enfeite sobre uma configuração já gravada
            logger.warning("Painel do canal de pedidos não reeleito [%s] (%s)", guild_id, type(exc).__name__)

    @commands.Cog.listener("on_ready")
    async def leave_banned_guilds(self):
        if bans := bans_of(self.bot):
            await bans.enforce_ready(self.bot)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: disnake.Guild):
        if (bans := bans_of(self.bot)) and await bans.enforce_join(self.bot, guild):
            return  # servidor banido: saiu, sem aviso no webhook e sem outro log

        logger.info("Bot %s foi adicionado(a) no servidor [%s]", self.bot.user.name, guild.id)
        if usage := usage_of(self.bot):
            usage.record_guild("join", guild, bot=self.bot)

        self.roster_changed(guild.id)

        try:
            # Só o documento POR BOT (canal, mensagem e favoritos voltam ao padrão): a configuração do servidor, no documento
            # único, não é lida nem gravada aqui, então um bot que entra não mexe nela (S-V5).
            database = self.bot.pool.database
            collection = str(self.bot.user.id)
            guild_data = deepcopy(await database.get_data(guild.id, db_name=DBModel.guilds, collection=collection))
            defaults = db_models[DBModel.guilds]["player_controller"]
            for key in CONTROLLER_KEYS:
                guild_data["player_controller"][key] = deepcopy(defaults[key])
            await database.update_data(guild.id, guild_data, db_name=DBModel.guilds, collection=collection)
        except Exception:
            logger.exception("Falha ao tratar a entrada do bot no servidor")

        if not self.hook_url:
            return

        try:
            await self.send_hook(guild, title="Me adicionaram em um novo servidor", color=disnake.Color.green())
        except Exception:
            logger.exception("Falha ao enviar o aviso de entrada no servidor")

        await self.bot.update_appinfo()

    async def send_hook(self, guild: disnake.Guild, title: str, color: disnake.Color):
        created_at = int(guild.created_at.timestamp())

        embed = disnake.Embed(
            description=f"__**{title}:**__\n"
            f"```{fix_characters(guild.name)}```\n"
            f"**ID:** `{guild.id}`\n"
            f"**Dono:** `{guild.owner} [{guild.owner.id}]`\n"
            f"**Criado em:** <t:{created_at}:f> - <t:{created_at}:R>\n"
            f"**Nível de verificação:** `{guild.verification_level or 'nenhuma'}`\n"
            f"**Membros:** `{len([m for m in guild.members if not m.bot])}`\n"
            f"**Bots:** `{len([m for m in guild.members if m.bot])}`\n",
            color=color,
        )

        try:
            embed.set_thumbnail(url=guild.icon.replace(static_format="png").url)
        except AttributeError:
            pass

        if (
            channel := self.bot.get_channel(self.bot.config["BOT_ADD_REMOVE_LOG_CHANNEL_ID"])
        ) and channel.permissions_for(channel.guild.me).send_messages:
            await channel.send(
                ", ".join(f"<@{owner_id}>" for owner_id in self.bot.owner_ids) or self.bot.owner.mention, embed=embed
            )

        else:
            async with ClientSession() as session:
                webhook = disnake.Webhook.from_url(self.hook_url, session=session)
                await webhook.send(
                    content=", ".join(f"<@{owner_id}>" for owner_id in self.bot.owner_ids) or self.bot.owner.mention,
                    username=self.bot.user.name,
                    avatar_url=self.bot.user.display_avatar.replace(size=256, static_format="png").url,
                    embed=embed,
                )


def setup(bot: BotCore):
    bot.add_cog(Misc(bot))
    bot.add_cog(GuildLog(bot))
