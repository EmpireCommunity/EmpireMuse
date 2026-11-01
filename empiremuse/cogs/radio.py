"""Rádio 24/7 (item 8, R1): os comandos de dono. ``/radio ligar|desligar|status`` entram nesta mesma classe.

R-1: premium, bots só-rádio e "pausar todas" são só de quem está em ``OWNER_IDS`` (``bot.env_owner_ids``). Nada de ``is_owner()``, porque ele
também aceita o dono e a equipe da aplicação; nem ``manage_guild`` nem administrador do servidor valem. Lista vazia = ninguém. Como todo bot
carrega este cog, os comandos de dono só rodam no principal (senão cada bot responderia uma vez).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, Union

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.rules.i18n import fit
from empiremuse.rules.radio import BotCandidate, panel_action, radio_id
from empiremuse.rules.servers import is_main_identifier
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.others import CustomContext
from empiremuse.utils.radio import actions
from empiremuse.utils.radio.actions import Actor, Refused
from empiremuse.utils.radio.control import MainBotRefused
from empiremuse.utils.radio.discord_ports import DiscordChatPorts, DiscordRadioPorts
from empiremuse.utils.radio.live_ports import LiveRadioPorts
from empiremuse.utils.radio.premium import RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

GRANT = ("conceder", "grant", "dar")
REVOKE = ("revogar", "revoke", "tirar")
LIST = ("listar", "list")
ADD = ("adicionar", "add")
REMOVE = ("remover", "remove")


def is_env_owner(bot: BotCore, user_id: int) -> bool:
    """O usuário está em ``OWNER_IDS``? Só isso: a equipe e o dono da aplicação do Discord NÃO contam."""
    return user_id in bot.env_owner_ids


def _guild_id(raw: str) -> int | None:
    return int(raw) if raw.isdecimal() and 0 < int(raw) < 1 << 64 else None


RADIO_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.guild)
MAX_CHOICES = 25
NAME_LIMIT = 80


async def playlist_autocomplete(inter: disnake.ApplicationCommandInteraction, current: str) -> list[str]:
    """Até 25 nomes de playlist do servidor; o texto digitado só filtra e quem decide é o ``find`` da store em execução."""
    if inter.guild_id is None or not inter.author.guild_permissions.manage_guild:
        return []
    from empiremuse.utils.server_playlists import safe_list  # tarde: o módulo importa muita coisa

    wanted = (current or "").strip().casefold()
    names = [p.name for p in await safe_list(inter.bot, inter.guild_id) if wanted in p.name.casefold()]
    return names[:MAX_CHOICES]


async def station_autocomplete(inter: disnake.ApplicationCommandInteraction, current: str) -> dict[str, str]:
    """Até 25 estações do Radio Browser como ``{nome mostrado: uuid}``. Sem ``manage_guild`` ou sem premium (cache): vazio, e NENHUMA requisição ao diretório."""
    if inter.guild_id is None or not inter.author.guild_permissions.manage_guild:
        return {}
    hub = inter.bot.pool.radio
    try:
        if await hub.premium.get(inter.guild_id) is None:
            return {}
    except RadioUnavailable:
        return {}
    tr = await inter.bot.pool.i18n.for_guild(inter.guild)
    choices: dict[str, str] = {}
    for station in (await hub.directory.search(current))[:MAX_CHOICES]:
        detail = f"{station.codec} {station.bitrate}k" if station.bitrate else station.codec
        label = f"{station.name} · {detail}" + (f" · {station.country}" if station.country else "")
        if hub.health.unavailable(station.uuid):
            label = f"⛔ {label} · {tr('radio.live.unavailable')}"
        choices[fit(label, 100)] = station.uuid
    return choices


class Radio(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot

    @property
    def hub(self):
        return self.bot.pool.radio

    # ------------------------------------------------------------------ subida: instala as portas reais e retoma as rádios

    @commands.Cog.listener("on_ready")
    async def radio_boot(self) -> None:
        """No principal, uma vez: instala o adaptador do Discord, liga o passo de presença e retoma as rádios ligadas (fila global, R-3)."""
        manager = self.hub.manager
        if not self._only_main() or manager.ports_factory is not None:
            return
        pool = self.bot.pool
        manager.ports_factory = lambda radio: (
            LiveRadioPorts(pool, radio) if radio.kind == "live" else DiscordRadioPorts(pool, radio)
        )
        manager.chat_ports_factory = lambda radio: DiscordChatPorts(pool, radio)
        manager.start_ticker()
        await self.hub.health.refresh()
        self.hub.health_checker.start()
        # com nova tentativa (30 s, 60 s... 5 min): uma falha passageira do banco na subida não deixa as rádios caídas até o restart
        manager._spawn(manager.resume_all_with_retry())

    async def _tr(self, ctx: CustomContext):
        return await self.bot.pool.i18n.for_guild(ctx.guild)

    def _only_main(self) -> bool:
        return self.bot.pool.servers.main_bot() is self.bot

    async def cog_check(self, ctx: CustomContext) -> bool:  # type: ignore[override]
        # só os comandos de dono vivem aqui como prefixo; a conferência vale para TODOS eles
        return self._only_main() and is_env_owner(self.bot, ctx.author.id)

    # ------------------------------------------------------------------ painel na call (R-6)

    @commands.Cog.listener("on_raw_message_delete")
    async def radio_panel_deleted(self, payload: disnake.RawMessageDeleteEvent) -> None:
        self.hub.manager.panel_deleted(payload.message_id)  # painel apagado volta na hora (no máximo um a cada 5 s)

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def radio_panel_bulk_deleted(self, payload: disnake.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids:
            self.hub.manager.panel_deleted(message_id)

    @commands.Cog.listener("on_button_click")
    async def radio_panel_click(self, interaction: disnake.MessageInteraction) -> None:
        """Botões pausar/próxima/desligar. **R-6:** a rádio vem de ``interaction.guild_id`` mais ESTE bot (o dono da mensagem), nunca do
        ``custom_id`` (que é fixo); ``manage_guild`` é conferido aqui, na execução; a interação tem de ser do canal da rádio."""
        action = panel_action(interaction.data.custom_id)
        if action is None or interaction.guild_id is None:
            return
        await interaction.response.defer(ephemeral=True, with_message=True)  # resposta só de quem apertou
        tr = await self.bot.pool.i18n.for_guild(interaction.guild)
        try:
            if not interaction.author.guild_permissions.manage_guild:
                raise GenericError(tr("radio.refused.no_manage_guild"))
            radio = await self.hub.store.get(radio_id(self.bot.user.id, interaction.guild_id))
            runner = self.hub.manager.runner(radio.id) if radio is not None else None
            if radio is None or not radio.enabled or runner is None or interaction.channel_id != radio.channel_id:
                raise GenericError(tr("radio.panel.gone"))
            if action == "pause":
                paused = not runner.admin_paused
                await runner.set_admin_pause(paused)
                await interaction.edit_original_message(
                    content=tr("radio.panel.paused" if paused else "radio.panel.resumed")
                )
            elif action == "next":
                skipped = await runner.skip()
                await interaction.edit_original_message(
                    content=tr("radio.panel.skipped" if skipped else "radio.panel.skip_wait")
                )
            else:
                actor = Actor(user_id=interaction.author.id, manage_guild=True)
                try:
                    await actions.turn_off(self.hub, guild_id=radio.guild_id, channel_id=radio.channel_id, actor=actor)
                finally:
                    await self.hub.manager.stop_radios(self.hub.manager.running_in(radio.guild_id, radio.channel_id))
                await interaction.edit_original_message(content=tr("radio.panel.stopped"))
        except GenericError as error:
            await interaction.edit_original_message(content=str(error))
        except Refused, RadioUnavailable:
            await interaction.edit_original_message(content=tr("radio.owner.unavailable"))
        except Exception as error:  # noqa: BLE001 - o clique não pode ficar sem resposta (3 s); só o tipo vai ao log
            logger.warning("Botão do painel da rádio falhou (%s)", type(error).__name__)
            await interaction.edit_original_message(content=tr("radio.owner.unavailable"))

    # ------------------------------------------------------------------ /radio (manage_guild; a execução confere de novo)

    def _candidates(self, guild_id: int, channel: Any) -> tuple[list[BotCandidate], bool]:
        """Os bots do pool vistos deste servidor e este canal. Sem ``view_channel`` e ``connect`` no canal, o bot conta como ocupado."""
        pool = self.bot.pool
        candidates: list[BotCandidate] = []
        can_join = False
        reserves_here = False
        for bot in pool.get_all_bots():
            if bot.user is None:
                continue
            guild = bot.get_guild(guild_id)
            exclusive = getattr(bot, "exclusive_guild_id", None)
            in_guild = guild is not None and not (exclusive and exclusive != guild_id)
            main = is_main_identifier(bot.identifier)
            perms_ok = False
            if in_guild:
                seen = guild.get_channel(channel.id)
                permissions = seen.permissions_for(guild.me) if seen is not None else None
                perms_ok = bool(permissions and permissions.view_channel and permissions.connect)
            busy = not perms_ok or guild_id in bot.music.players or bool(in_guild and guild.me.voice)
            reserves_here = reserves_here or (in_guild and not main)
            can_join = can_join or (perms_ok and not main)
            candidates.append(
                BotCandidate(
                    bot.identifier,
                    bot.user.id,
                    main,
                    pool.radio.roles.is_radio_only(bot.identifier),
                    bool(bot.bot_ready),
                    bool(in_guild),
                    busy,
                )
            )
        # sem nenhum reserva no servidor a recusa certa é "não há bot livre", não "nenhum consegue entrar"
        return candidates, can_join or not reserves_here

    @commands.slash_command(
        name="radio",
        description=localized("cmd.radio.description"),
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    @commands.contexts(guild=True)
    async def radio(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    async def _gate(self, inter: disnake.ApplicationCommandInteraction):
        """Confirma em 3 s, traduz e aplica o cooldown de 10 s por servidor."""
        await inter.response.defer(ephemeral=True)
        tr = await self.bot.pool.i18n.for_guild(inter.guild)
        bucket = RADIO_COOLDOWN.get_bucket(inter)
        if bucket is not None and (retry := bucket.update_rate_limit()):
            raise GenericError(tr("radio.cooldown", seconds=int(retry) + 1))
        return tr

    @radio.sub_command(name="ligar", description=localized("cmd.radio.ligar.description"))
    async def radio_on(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
            name=localized("cmd.radio.ligar.opt.channel.name"),
            description=localized("cmd.radio.ligar.opt.channel.description"),
        ),
        playlist: Optional[str] = commands.Param(
            default=None,
            name=localized("cmd.radio.ligar.opt.playlist.name"),
            description=localized("cmd.radio.ligar.opt.playlist.description"),
            autocomplete=playlist_autocomplete,
        ),
        station: Optional[str] = commands.Param(
            default=None,
            name=localized("cmd.radio.ligar.opt.station.name"),
            description=localized("cmd.radio.ligar.opt.station.description"),
            autocomplete=station_autocomplete,
        ),
    ) -> None:
        tr = await self._gate(inter)
        actor = Actor(
            user_id=inter.author.id,
            manage_guild=inter.author.guild_permissions.manage_guild,
            manage_messages_in_channel=channel.permissions_for(
                inter.author
            ).manage_messages,  # no CANAL, em execução (R-4)
        )
        found = await self.bot.pool.servers.playlists.find(inter.guild_id, playlist) if playlist else None
        picked = None
        if station:  # o valor vem do autocomplete, mas o usuário pode digitar qualquer coisa: o diretório confere (uuid, codec, porta, hls)
            picked = await self.hub.directory.by_uuid(station)
            if picked is None:
                raise GenericError(tr("radio.live.not_found"))
        request_channel = (await self.bot.pool.servers.request.get(inter.guild_id)).channel
        candidates, can_join = self._candidates(inter.guild_id, channel)
        try:
            radio = await actions.turn_on(
                self.hub,
                guild_id=inter.guild_id,
                channel_id=channel.id,
                playlist=found.name
                if found
                else playlist,  # com estação também, o turn_on recusa (choose_one): nada é ignorado em silêncio
                station=picked,
                actor=actor,
                candidates=candidates,
                playlist_exists=found is not None,
                bot_can_join=can_join,
                request_channel_id=request_channel,
            )
        except Refused as refusal:
            raise GenericError(tr(f"radio.refused.{refusal.code}")) from None
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable")) from None
        await self.hub.manager.activate(
            radio, wait=False
        )  # a partida segue em segundo plano: sem prender a resposta ao token
        logger.info("radio.toggle: ligada (%d neste processo)", self.hub.manager.counts().on)
        name = external_text(radio.source.get("playlist"), NAME_LIMIT)
        await inter.edit_original_message(
            content=tr("radio.on.done", channel=channel.mention, playlist=name, bot=f"<@{radio.bot_id}>"),
            allowed_mentions=disnake.AllowedMentions.none(),
        )

    @radio.sub_command(name="desligar", description=localized("cmd.radio.desligar.description"))
    async def radio_off(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
            name=localized("cmd.radio.desligar.opt.channel.name"),
            description=localized("cmd.radio.desligar.opt.channel.description"),
        ),
    ) -> None:
        tr = await self._gate(inter)
        actor = Actor(user_id=inter.author.id, manage_guild=inter.author.guild_permissions.manage_guild)
        try:
            off = await actions.turn_off(self.hub, guild_id=inter.guild_id, channel_id=channel.id, actor=actor)
        except Refused as refusal:
            raise GenericError(tr(f"radio.refused.{refusal.code}")) from None
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable")) from None
        finally:
            # mesmo que o banco tenha falhado no meio, o que este processo está tocando nesse canal para (e o bot sai da call)
            if actor.manage_guild:
                await self.hub.manager.stop_radios(self.hub.manager.running_in(inter.guild_id, channel.id))
        logger.info("radio.toggle: desligada (%d neste processo)", self.hub.manager.counts().on)
        await inter.edit_original_message(content=tr("radio.off.done", count=len(off), channel=channel.mention))

    @radio.sub_command(name="status", description=localized("cmd.radio.status.description"))
    async def radio_status(self, inter: disnake.ApplicationCommandInteraction) -> None:
        tr = await self._gate(inter)
        try:
            result = await actions.status(self.hub, guild_id=inter.guild_id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable")) from None
        if result.premium is None:
            await inter.edit_original_message(content=tr("radio.status.no_premium"))
            return
        lines = [tr("radio.status.header", active=result.premium.active, cap=3)]
        for line in result.lines:
            state = line.state
            key = (
                "radio.state.paused_owner"
                if state and state.paused_by_owner
                else "radio.state.offline"
                if state and state.offline
                else "radio.state.playing"
                if state and state.playing
                else "radio.state.paused"
                if state
                else "radio.state.off"
            )
            lines.append(tr("radio.status.line", channel=f"<#{line.radio.channel_id}>", state=tr(key)))
        if not result.lines:
            lines.append(tr("radio.status.none"))
        if result.paused_all:
            lines.append(tr("radio.status.paused_all"))
        await inter.edit_original_message(content="\n".join(lines), allowed_mentions=disnake.AllowedMentions.none())

    # ------------------------------------------------------------------ premium

    @commands.command(name="radio-premium", hidden=True)
    async def radio_premium(self, ctx: CustomContext, action: str = "", guild: str = ""):
        """``radio-premium conceder|revogar <id do servidor>`` e ``radio-premium listar``."""
        tr = await self._tr(ctx)
        action = action.casefold()
        try:
            if action in LIST:
                lines = [f"`{p.guild_id}` — {p.active}/3" for p in await self.hub.premium.all()]
                await ctx.send(tr("radio.owner.premium_list", lines="\n".join(lines) or tr("radio.owner.none")))
                return
            guild_id = _guild_id(guild)
            if action not in GRANT + REVOKE or guild_id is None:
                raise GenericError(tr("radio.owner.premium_usage"))
            if action in GRANT:
                await self.hub.premium.grant(guild_id, by=ctx.author.id)
                logger.info("Premium de rádio concedido")
                await ctx.send(tr("radio.owner.premium_granted", guild=guild_id))
            else:
                off = await self.hub.manager.revoke_premium(guild_id, updated_by=str(ctx.author.id))
                logger.info("Premium de rádio revogado (%d rádios desligadas)", off)
                await ctx.send(tr("radio.owner.premium_revoked", guild=guild_id, count=off))
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))

    # ------------------------------------------------------------------ pausar todas

    @commands.command(name="radio-pausar-todas", hidden=True)
    async def radio_pause_all(self, ctx: CustomContext):
        tr = await self._tr(ctx)
        try:
            changed = await self.hub.control.set_paused_all(True, by=ctx.author.id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
        logger.info("Rádios: pausa geral %s", "ligada" if changed else "já estava ligada")
        await ctx.send(tr("radio.owner.paused_all" if changed else "radio.owner.paused_all_already"))

    @commands.command(name="radio-retomar-todas", hidden=True)
    async def radio_resume_all(self, ctx: CustomContext):
        tr = await self._tr(ctx)
        try:
            changed = await self.hub.control.set_paused_all(False, by=ctx.author.id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
        logger.info("Rádios: pausa geral %s", "desligada" if changed else "já estava desligada")
        await ctx.send(tr("radio.owner.resumed_all" if changed else "radio.owner.resumed_all_already"))

    # ------------------------------------------------------------------ bots só-rádio

    @commands.command(name="radio-bot", hidden=True)
    async def radio_bot(self, ctx: CustomContext, action: str = "", identifier: str = ""):
        """``radio-bot adicionar|remover <TOKEN_2>`` e ``radio-bot listar``: os bots reservas dedicados a rádio."""
        tr = await self._tr(ctx)
        action = action.casefold()
        try:
            if action in LIST:
                await self.hub.roles.load()
                bots = ", ".join(f"`{i}`" for i in self.hub.roles.radio_only) or tr("radio.owner.none")
                await ctx.send(tr("radio.owner.bots_list", bots=bots), allowed_mentions=disnake.AllowedMentions.none())
                return
            if action not in ADD + REMOVE or not identifier.isascii() or not 1 <= len(identifier) <= 40:
                raise GenericError(tr("radio.owner.bots_usage"))
            if action in ADD:
                await self.hub.roles.add(identifier)
            else:
                await self.hub.roles.remove(identifier)
            await ctx.send(
                tr("radio.owner.bots_changed", bot=identifier), allowed_mentions=disnake.AllowedMentions.none()
            )
        except MainBotRefused:
            raise GenericError(tr("radio.owner.main_refused"))
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))


def setup(bot: BotCore) -> None:
    bot.add_cog(Radio(bot))
