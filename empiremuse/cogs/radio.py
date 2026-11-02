"""Rádio 24/7 (item 8, R1): os comandos de dono. ``/radio ligar|desligar|status`` entram nesta mesma classe.

R-1: premium, bots só-rádio e "pausar todas" são só de quem está em ``OWNER_IDS`` (``bot.env_owner_ids``). Nada de ``is_owner()``, porque ele
também aceita o dono e a equipe da aplicação; nem ``manage_guild`` nem administrador do servidor valem. Lista vazia = ninguém. Como todo bot
carrega este cog, os comandos de dono só rodam no principal (senão cada bot responderia uma vez).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional, Union

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.rules.i18n import fit
from empiremuse.rules.radio import (
    KIND_LIVE,
    KINDS,
    PANEL_MENU_ID,
    VOLUME_DEFAULT,
    VOLUME_STEP,
    clamp_volume,
    menu_action,
    panel_action,
    radio_id,
    station_neighbor,
)
from empiremuse.rules.radio_stations import Station
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.others import CustomContext
from empiremuse.utils.radio import actions, migrate_r5, setup_flow, station_commands
from empiremuse.utils.radio.actions import Actor, Refused
from empiremuse.utils.radio.control import MainBotRefused
from empiremuse.utils.radio.custom_ports import CustomRadioPorts
from empiremuse.utils.radio.discord_ports import DiscordChatPorts, DiscordRadioPorts
from empiremuse.utils.radio.live_ports import LiveRadioPorts
from empiremuse.utils.radio.favorites import MAX_FAVORITES
from empiremuse.utils.radio.picker import RadioPicker, VolumePicker
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.setup import jukebox_channel_of
from empiremuse.utils.radio.store import RadioConflict

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
CUSTOM_ACTIONS = frozenset(
    {"station_previous", "station_next"}
)  # só no painel da personalizada: anterior e próxima ESTAÇÃO do servidor
LIVE_ACTIONS = frozenset(
    {"previous", "mute", "volume_down", "volume_up"}
)  # só no painel da rádio ao vivo (o "próxima" existe nos dois, com outro sentido)
VOLUME_GAP_S = 1.0
STATION_GAP_S = 5.0  # anterior, próxima e o seletor de estação: no máximo uma troca a cada 5 s por rádio (cada uma reabre o fluxo e consulta o diretório)
LOOKUP_GAP_S = 5.0  # informações e favoritar: no máximo uma consulta ao diretório a cada 5 s por rádio
MAX_CHOICES = 25
NAME_LIMIT = 80


async def bot_autocomplete(inter: disnake.ApplicationCommandInteraction, current: str) -> dict[str, str]:
    """Os bots reserva livres do servidor como ``{nome: id}``. Só informa: quem decide é o ``configure`` em execução. Sem ``manage_guild`` ou sem premium, vazio."""
    if inter.guild_id is None or not inter.author.guild_permissions.manage_guild:
        return {}
    hub = inter.bot.pool.radio
    try:
        if await hub.premium.get(inter.guild_id) is None:
            return {}
        options = await hub.setup.options(inter.guild_id)
    except RadioUnavailable:
        return {}
    wanted = (current or "").strip().casefold()
    return {fit(name, 100): str(bot_id) for bot_id, name in options.bots if wanted in name.casefold()}


class Radio(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot
        self._boot_task: Any = None
        self._gap_at: dict[tuple[str, str], float] = {}  # a última troca de estação e a última consulta de cada rádio
        self._volume_at: dict[
            str, float
        ] = {}  # quando o volume de cada rádio mudou pela última vez (no máximo 1 por segundo)

    @property
    def hub(self):
        return self.bot.pool.radio

    # ------------------------------------------------------------------ subida: instala as portas reais e retoma as rádios

    async def cog_load(self) -> None:
        """Se o cog carrega DEPOIS do primeiro ``on_ready`` (extensão recarregada, carga tardia), o ouvinte nunca dispara: sobe aqui também.
        O guard de ``ports_factory`` em ``radio_boot`` garante que subir pelos dois caminhos é o mesmo que subir uma vez."""
        if self.bot.is_ready():
            self._boot_task = self.bot.loop.create_task(self.radio_boot())

    def cog_unload(self) -> None:
        """Descarregar o cog enquanto o boot ainda corre não deixa a tarefa solta instalando portas de um cog que já saiu."""
        if self._boot_task is not None and not self._boot_task.done():
            self._boot_task.cancel()
        self._boot_task = None

    @commands.Cog.listener("on_ready")
    async def radio_boot(self) -> None:
        """No principal, uma vez: instala o adaptador do Discord, liga o passo de presença e retoma as rádios ligadas (fila global, R-3)."""
        manager = self.hub.manager
        if not self._only_main() or manager.ports_factory is not None:
            return
        pool = self.bot.pool
        manager.ports_factory = lambda radio: (
            LiveRadioPorts(pool, radio)
            if radio.kind == "live"
            else CustomRadioPorts(pool, radio)
            if "station" in radio.source
            else DiscordRadioPorts(pool, radio)
        )
        manager.chat_ports_factory = lambda radio: DiscordChatPorts(pool, radio)
        manager.start_ticker()
        try:
            await migrate_r5.run(
                self.hub
            )  # as rádios de playlist viram estações do servidor ANTES de retomar (idempotente)
        except Exception as error:  # noqa: BLE001 - uma migração que falha não impede a subida: o legado segue tocando; só o tipo vai ao log (R-12)
            logger.warning("Migração das rádios para estações falhou (%s)", type(error).__name__)
        await self.hub.health.refresh()
        self.hub.health_checker.start()
        # com nova tentativa (30 s, 60 s... 5 min): uma falha passageira do banco na subida não deixa as rádios caídas até o restart
        manager._spawn(manager.resume_all_with_retry())

    async def _tr(self, ctx: CustomContext):
        return await self.bot.pool.i18n.for_guild(ctx.guild)

    def _only_main(self) -> bool:
        return self.bot.pool.servers.main_bot() is self.bot

    async def owner_check(self, ctx: CustomContext) -> bool:
        """Os subcomandos de dono do prefixo ``radio`` (premium, bot, pauseall, resumeall): só ``OWNER_IDS`` e só no bot principal."""
        return self._only_main() and is_env_owner(self.bot, ctx.author.id)

    async def manager_check(self, ctx: CustomContext) -> bool:
        """O prefixo ``radio`` e seus subcomandos públicos: ``manage_guild`` no servidor, conferido em execução, e só no bot principal."""
        return self._only_main() and ctx.guild is not None and bool(ctx.author.guild_permissions.manage_guild)

    async def group_check(self, ctx: CustomContext) -> bool:
        return await self.manager_check(ctx) or await self.owner_check(ctx)

    # ------------------------------------------------------------------ painel na call (R-6)

    @commands.Cog.listener("on_raw_message_delete")
    async def radio_panel_deleted(self, payload: disnake.RawMessageDeleteEvent) -> None:
        self.hub.manager.panel_deleted(payload.message_id)  # painel apagado volta na hora (no máximo um a cada 5 s)

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def radio_panel_bulk_deleted(self, payload: disnake.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids:
            self.hub.manager.panel_deleted(message_id)

    async def _panel_radio(self, interaction: Any, tr: Any):
        """A rádio do painel em que se clicou e o runner dela. **R-6:** vem de ``interaction.guild_id`` mais ESTE bot (o dono da mensagem), nunca do
        ``custom_id`` (que é fixo). **Regra do dono:** TODO controle do painel é só de quem está AGORA na call da rádio (membro, não bot); quem não está
        recebe uma recusa que só ele vê."""
        radio = await self.hub.store.get(radio_id(self.bot.user.id, interaction.guild_id))
        runner = self.hub.manager.runner(radio.id) if radio is not None else None
        if radio is None or not radio.enabled or runner is None or interaction.channel_id != radio.channel_id:
            raise GenericError(tr("radio.panel.gone"))
        if not self._in_call(interaction, radio.channel_id):
            raise GenericError(tr("radio.panel.call_only"))
        return radio, runner

    @staticmethod
    def _in_call(interaction: Any, channel_id: int) -> bool:
        author = interaction.author
        voice = getattr(author, "voice", None)
        channel = getattr(voice, "channel", None)
        return not getattr(author, "bot", False) and channel is not None and channel.id == channel_id

    @commands.Cog.listener("on_button_click")
    async def radio_panel_click(self, interaction: disnake.MessageInteraction) -> None:
        """Os botões dos dois painéis. Cada painel tem os SEUS: a ao vivo (anterior, volume, mudo, próxima) e a personalizada (pausar, pular, desligar); um
        painel antigo na tela não mexe no outro tipo de rádio."""
        action = panel_action(interaction.data.custom_id)
        if action is None or interaction.guild_id is None:
            return
        await interaction.response.defer(ephemeral=True, with_message=True)  # resposta só de quem apertou
        tr = await self.bot.pool.i18n.for_guild(interaction.guild)
        try:
            radio, runner = await self._panel_radio(interaction, tr)
            live = radio.kind == KIND_LIVE
            if (action in LIVE_ACTIONS and not live) or (
                action in CUSTOM_ACTIONS and live
            ):  # um painel antigo na tela não faz a personalizada andar de estação nem mexer em volume
                raise GenericError(tr("radio.panel.not_here"))
            await self._run_action(interaction, tr, radio, runner, action)
        except GenericError as error:
            await interaction.edit_original_message(content=error.text)
        except Refused as refusal:
            await interaction.edit_original_message(content=tr(f"radio.refused.{refusal.code}"))
        except RadioUnavailable:
            await interaction.edit_original_message(content=tr("radio.owner.unavailable"))
        except Exception as error:  # noqa: BLE001 - o clique não pode ficar sem resposta (3 s); só o tipo vai ao log
            logger.warning("Botão do painel da rádio falhou (%s)", type(error).__name__)
            await interaction.edit_original_message(content=tr("radio.owner.unavailable"))

    @commands.Cog.listener("on_dropdown")
    async def radio_panel_menu(self, interaction: disnake.MessageInteraction) -> None:
        """O select "Mais opções" do painel ao vivo: trocar de estação, favoritar no servidor, volume, informações e desligar."""
        if interaction.data.custom_id != PANEL_MENU_ID or interaction.guild_id is None:
            return
        values = getattr(interaction.data, "values", None) or []
        action = menu_action(values[0]) if len(values) == 1 else None
        await interaction.response.defer(ephemeral=True, with_message=True)
        tr = await self.bot.pool.i18n.for_guild(interaction.guild)
        try:
            radio, runner = await self._panel_radio(interaction, tr)
            if action is None or radio.kind != KIND_LIVE:
                raise GenericError(tr("radio.panel.not_here"))
            await self._run_menu(interaction, tr, radio, runner, action)
        except GenericError as error:
            await interaction.edit_original_message(content=error.text)
        except Refused as refusal:
            await interaction.edit_original_message(content=tr(f"radio.refused.{refusal.code}"))
        except RadioUnavailable:
            await interaction.edit_original_message(content=tr("radio.owner.unavailable"))
        except Exception as error:  # noqa: BLE001
            logger.warning("Menu do painel da rádio falhou (%s)", type(error).__name__)
            await interaction.edit_original_message(content=tr("radio.owner.unavailable"))
        finally:
            ports = getattr(self.hub.manager.runner(radio_id(self.bot.user.id, interaction.guild_id)), "ports", None)
            refresh = getattr(ports, "refresh_panel", None)
            if refresh is not None:
                refresh()  # o select volta a "Mais opções" (a escolha feita fica marcada no cliente até a mensagem ser editada)

    @staticmethod
    def _authorized(interaction: Any) -> Actor:
        """Quem passou por ``_panel_radio`` está na call da rádio: o painel vale como autorização para trocar estação, volume e favoritas, então o ``Actor``
        leva ``manage_guild=True``. Desligar NÃO usa isto (``_turn_off`` confere ``manage_guild`` de verdade). O premium continua sendo conferido por quem grava."""
        return Actor(user_id=interaction.author.id, manage_guild=True)

    async def _run_action(self, interaction: Any, tr: Any, radio: Any, runner: Any, action: str) -> None:
        reply = interaction.edit_original_message
        if action == "pause":
            paused = not runner.admin_paused
            if not paused and self._too_soon(radio.id, "station", STATION_GAP_S):
                await reply(
                    content=tr("radio.panel.wait")
                )  # retomar ocupa uma vaga da fila global e gasta uma ficha do play: pausar segue imediato
                return
            await runner.set_admin_pause(paused)
            await reply(content=tr("radio.panel.paused" if paused else "radio.panel.resumed"))
        elif action == "mute":
            muted = (
                not runner.admin_paused
            )  # parar o fluxo: ao voltar, a rádio entra no ponto AO VIVO, não no que ficou guardado
            if not muted and self._too_soon(radio.id, "station", STATION_GAP_S):
                await reply(content=tr("radio.panel.wait"))  # desmudar é retomar: mesma regra
                return
            await runner.set_admin_pause(muted)
            await reply(content=tr("radio.panel.muted" if muted else "radio.panel.unmuted"))
        elif action in CUSTOM_ACTIONS:
            await reply(
                content=await self._walk_stations(interaction, tr, radio, 1 if action == "station_next" else -1)
            )
        elif action == "next" and radio.kind == KIND_LIVE or action == "previous":
            await reply(content=await self._walk(interaction, tr, radio, 1 if action == "next" else -1))
        elif action == "next":
            skipped = await runner.skip()
            await reply(content=tr("radio.panel.skipped" if skipped else "radio.panel.skip_wait"))
        elif action in ("volume_down", "volume_up"):
            step = VOLUME_STEP if action == "volume_up" else -VOLUME_STEP
            await reply(
                content=await self._volume(
                    interaction, tr, radio, runner, radio.source.get("volume", VOLUME_DEFAULT) + step
                )
            )
        else:
            await self._turn_off(interaction, tr, radio)

    async def _run_menu(self, interaction: Any, tr: Any, radio: Any, runner: Any, action: str) -> None:
        reply = interaction.edit_original_message
        if action == "stations":
            await self._open_station_picker(interaction, tr, radio)
        elif action == "favorite":
            await reply(content=await self._favorite(interaction, tr, radio))
        elif action == "volume":
            await self._open_volume_picker(interaction, tr, radio, runner)
        elif action == "info":
            await reply(content=await self._info(tr, radio), allowed_mentions=disnake.AllowedMentions.none())
        else:
            await self._turn_off(interaction, tr, radio)

    async def _turn_off(self, interaction: Any, tr: Any, radio: Any) -> None:
        """Desligar continua exigindo ``manage_guild`` (matriz do R1 §7): estar na call libera os outros controles, não este. Recusado, a rádio segue ligada."""
        actor = Actor(
            user_id=interaction.author.id, manage_guild=bool(interaction.author.guild_permissions.manage_guild)
        )
        try:
            await actions.turn_off(self.hub, guild_id=radio.guild_id, channel_id=radio.channel_id, actor=actor)
        except Refused:
            raise  # sem permissão (ou nada ligado ali): nada é parado
        except BaseException:
            await self.hub.manager.stop_radios(self.hub.manager.running_in(radio.guild_id, radio.channel_id))
            raise
        await self.hub.manager.stop_radios(self.hub.manager.running_in(radio.guild_id, radio.channel_id))
        await interaction.edit_original_message(content=tr("radio.panel.stopped"))

    def _too_soon(self, radio_id: str, kind: str, gap: float) -> bool:
        """``True`` se a última ação do mesmo tipo nesta rádio foi há menos de ``gap`` s; senão registra esta e devolve ``False``. Um membro na call
        clicando em rajada não esvazia o balde do diretório nem reabre o fluxo a cada clique (V190-1)."""
        now = time.monotonic()
        last = self._gap_at.get((radio_id, kind))
        if last is not None and now - last < gap:
            return True
        self._gap_at[(radio_id, kind)] = now
        return False

    async def _walk(self, interaction: Any, tr: Any, radio: Any, step: int) -> str:
        """Anterior e próxima: a estação vizinha na lista de onde a atual foi escolhida (dá a volta e pula as indisponíveis)."""
        stations = list(radio.source.get("list") or ())
        unavailable = {uuid for uuid in stations if self.hub.health.unavailable(uuid)}
        neighbor = station_neighbor(stations, radio.source["station"], step, unavailable)
        if neighbor is None:
            return tr("radio.panel.no_other_station")
        if self._too_soon(radio.id, "station", STATION_GAP_S):
            return tr("radio.panel.wait")
        station = await self.hub.directory.by_uuid(neighbor)
        if station is None:
            return tr("radio.live.not_found")
        await actions.change_station(self.hub, radio_id=radio.id, station=station, actor=self._authorized(interaction))
        return tr("radio.local.changed", station=external_text(station.name, NAME_LIMIT))

    async def _walk_stations(self, interaction: Any, tr: Any, radio: Any, step: int) -> str:
        """Anterior e próxima ESTAÇÃO do servidor (por nome) na rádio personalizada: dá a volta; uma só estação não tem por onde andar."""
        stations = await self.hub.stations.list_for_guild(radio.guild_id)
        neighbor = station_neighbor([s.id for s in stations], radio.source.get("station"), step)
        if neighbor is None:
            return tr("radio.panel.no_other_station")
        if self._too_soon(radio.id, "station", STATION_GAP_S):
            return tr("radio.panel.wait")
        await actions.change_custom_station(
            self.hub, radio_id=radio.id, station_id=neighbor, actor=self._authorized(interaction)
        )
        name = next(s.name for s in stations if s.id == neighbor)
        return tr("radio.local.changed", station=external_text(name, NAME_LIMIT))

    async def _volume(self, interaction: Any, tr: Any, radio: Any, runner: Any, wanted: int) -> str:
        """Grava o volume (com ``rev``) e o aplica ao player. No máximo uma mudança por segundo e por rádio."""
        now = time.monotonic()
        if now - self._volume_at.get(radio.id, -VOLUME_GAP_S) < VOLUME_GAP_S:
            return tr("radio.panel.volume_wait")
        self._volume_at[radio.id] = now
        volume = clamp_volume(wanted)
        try:
            await self.hub.store.set_volume(
                radio.id, volume, expected_rev=radio.rev, updated_by=str(interaction.author.id)
            )
        except RadioConflict:
            return tr("radio.refused.changed")
        apply = getattr(runner.ports, "set_volume", None)
        if apply is not None:
            await apply(volume)
        return tr("radio.panel.volume_set", volume=volume)

    async def _station_of(self, radio: Any) -> Station | None:
        return await self.hub.directory.by_uuid(radio.source["station"])

    async def _favorite(self, interaction: Any, tr: Any, radio: Any) -> str:
        if self._too_soon(radio.id, "lookup", LOOKUP_GAP_S):
            return tr("radio.panel.wait")
        station = await self._station_of(radio)
        if station is None:
            return tr("radio.live.not_found")
        result = await self.hub.favorites.add(radio.guild_id, station)
        return tr(
            f"radio.panel.favorite_{result.value}", station=external_text(station.name, NAME_LIMIT), max=MAX_FAVORITES
        )

    async def _info(self, tr: Any, radio: Any) -> str:
        if self._too_soon(radio.id, "lookup", LOOKUP_GAP_S):
            return tr("radio.panel.wait")
        station = await self._station_of(radio)
        if station is None:
            return tr("radio.live.not_found")
        return tr(
            "radio.panel.info",
            station=external_text(station.name, NAME_LIMIT),
            codec=external_text(station.codec, 20),
            bitrate=station.bitrate or "?",
            country=external_text(station.country, 40) or "?",
        )

    async def _open_volume_picker(self, interaction: Any, tr: Any, radio: Any, runner: Any) -> None:
        async def apply(click: disnake.MessageInteraction, level: int) -> str:
            fresh, fresh_runner = await self._panel_radio(click, tr)
            return await self._volume(click, tr, fresh, fresh_runner, level)

        picker = VolumePicker(
            tr,
            author_id=interaction.author.id,
            current=radio.source.get("volume", VOLUME_DEFAULT),
            apply=apply,
            check=lambda click: self._in_call(click, radio.channel_id),
        )
        await interaction.edit_original_message(content=tr("radio.panel.volume.title"), view=picker)

    # ------------------------------------------------------------------ /radio (manage_guild; a execução confere de novo)

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

    @radio.sub_command(name="setup", description=localized("cmd.radio.setup.description"))
    async def radio_setup(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
            name=localized("cmd.radio.setup.opt.channel.name"),
            description=localized("cmd.radio.setup.opt.channel.description"),
        ),
        bot: str = commands.Param(
            name=localized("cmd.radio.setup.opt.bot.name"),
            description=localized("cmd.radio.setup.opt.bot.description"),
            autocomplete=bot_autocomplete,
        ),
        kind: str = commands.Param(
            name=localized("cmd.radio.setup.opt.kind.name"),
            description=localized("cmd.radio.setup.opt.kind.description"),
            choices=list(KINDS),
        ),
    ) -> None:
        await setup_flow.setup(self, inter, channel, bot, kind)

    @radio.sub_command(name="on", description=localized("cmd.radio.on.description"))
    async def radio_on(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
            name=localized("cmd.radio.on.opt.channel.name"),
            description=localized("cmd.radio.on.opt.channel.description"),
        ),
    ) -> None:
        tr = await self._gate(inter)
        text = await self._turn_on(inter.author, inter.guild_id, tr, channel)
        await inter.edit_original_message(content=text, allowed_mentions=disnake.AllowedMentions.none())

    @radio.sub_command(name="reset", description=localized("cmd.radio.reset.description"))
    async def radio_reset(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
            name=localized("cmd.radio.reset.opt.channel.name"),
            description=localized("cmd.radio.reset.opt.channel.description"),
        ),
    ) -> None:
        await setup_flow.reset(self, inter, channel)

    def _bot_state(self, radio: Any, channel: Any) -> tuple[bool, bool]:
        """O bot FIXO da rádio visto deste canal: ``(consegue entrar, está livre)``. Sem ``view_channel`` e ``connect`` no canal ele não entra; em call ou com player no servidor não está livre."""
        pool = self.bot.pool
        bot = next((b for b in pool.get_all_bots() if b.user is not None and b.user.id == radio.bot_id), None)
        guild = bot.get_guild(radio.guild_id) if bot is not None else None
        if bot is None or guild is None or not bot.bot_ready:
            return False, False
        seen = guild.get_channel(channel.id)
        permissions = seen.permissions_for(guild.me) if seen is not None else None
        can_join = bool(permissions and permissions.view_channel and permissions.connect)
        free = radio.guild_id not in bot.music.players and not guild.me.voice
        return can_join, free

    async def _turn_on(self, author: Any, guild_id: int, tr: Any, channel: Any) -> str:
        """Liga a rádio configurada neste canal e devolve o texto da resposta."""
        actor = Actor(
            user_id=author.id,
            manage_guild=author.guild_permissions.manage_guild,
            manage_messages_in_channel=channel.permissions_for(author).manage_messages,  # no CANAL, em execução (R-4)
        )
        try:
            configured = next(
                (r for r in await self.hub.store.list_for_guild(guild_id) if r.channel_id == channel.id), None
            )
            can_join, free = self._bot_state(configured, channel) if configured is not None else (True, True)
            jukebox = await jukebox_channel_of(self.bot.pool, guild_id)
            radio = await actions.turn_on(
                self.hub,
                guild_id=guild_id,
                channel_id=channel.id,
                actor=actor,
                bot_can_join=can_join,
                bot_free=free,
                jukebox_channel_id=jukebox,
            )
        except Refused as refusal:
            raise GenericError(tr(f"radio.refused.{refusal.code}")) from None
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable")) from None
        await self.hub.manager.activate(
            radio, wait=False
        )  # a partida segue em segundo plano: sem prender a resposta ao token
        logger.info("radio.toggle: ligada (%d neste processo)", self.hub.manager.counts().on)
        return tr("radio.on.ready", channel=channel.mention, bot=f"<@{radio.bot_id}>")

    # ------------------------------------------------------------------ seletor de estação por local (R4)

    @staticmethod
    def _can_manage(interaction: Any) -> bool:
        return bool(interaction.author.guild_permissions.manage_guild)

    def _picker(
        self,
        interaction: Any,
        tr: Any,
        apply: Any,
        confirm_label: str,
        *,
        check: Any = None,
        deny_key: str = "radio.refused.no_manage_guild",
    ) -> RadioPicker:
        tree = self.hub.locations
        if tree is None:  # o arquivo de dados não carregou: o filtro some e a busca por nome (``estacao:``) segue
            raise GenericError(tr("radio.local.unavailable"))
        guild_locale = getattr(interaction.guild, "preferred_locale", None)
        return RadioPicker(
            self.hub,
            tree,
            tr,
            author_id=interaction.author.id,
            guild_id=interaction.guild_id,
            locale=tr.locale,
            guild_locale=str(getattr(guild_locale, "value", guild_locale)) if guild_locale else None,
            apply=apply,
            confirm_label=confirm_label,
            check=check or self._can_manage,
            deny_key=deny_key,
        )

    async def _open_station_picker(self, interaction: disnake.MessageInteraction, tr: Any, radio: Any) -> None:
        """ "Trocar estação" do painel ao vivo: escolhe uma estação por local (ou nas favoritas) e troca a principal da rádio que já toca. Quem abre já está na
        call da rádio; o premium é conferido aqui e de novo por quem grava."""
        try:
            if await self.hub.premium.get(interaction.guild_id) is None:
                raise GenericError(tr(f"radio.refused.{actions.NO_PREMIUM}"))
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable")) from None

        async def apply(click: disnake.MessageInteraction, station: Station, station_list: tuple[str, ...]) -> str:
            fresh, _ = await self._panel_radio(
                click, tr
            )  # a rádio de AGORA (o seletor vive 5 min: ela pode ter sido desligada ou ter mudado de canal)
            if self._too_soon(fresh.id, "station", STATION_GAP_S):
                return tr("radio.panel.wait")
            try:
                await actions.change_station(
                    self.hub,
                    radio_id=fresh.id,
                    station=station,
                    actor=self._authorized(click),
                    station_list=station_list or None,
                )
            except Refused as refusal:
                raise GenericError(tr(f"radio.refused.{refusal.code}")) from None
            return tr("radio.local.changed", station=external_text(station.name, NAME_LIMIT))

        picker = self._picker(
            interaction,
            tr,
            apply,
            tr("radio.local.confirm_change"),
            check=lambda click: self._in_call(click, radio.channel_id),
            deny_key="radio.panel.call_only",
        )
        await interaction.edit_original_message(**await picker.render())

    @radio.sub_command_group(name="station", description=localized("cmd.radio.station.description"))
    async def radio_station(self, inter: disnake.ApplicationCommandInteraction) -> None:
        return

    @radio_station.sub_command(name="create", description=localized("cmd.radio.station.create.description"))
    async def radio_station_create(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            name=localized("cmd.radio.station.create.opt.name.name"),
            description=localized("cmd.radio.station.create.opt.name.description"),
            max_length=32,
        ),
        kind: Optional[str] = commands.Param(
            default=None,
            name=localized("cmd.radio.station.create.opt.kind.name"),
            description=localized("cmd.radio.station.create.opt.kind.description"),
            choices=["genre", "artists", "playlists"],
        ),
    ) -> None:
        await station_commands.create(self, inter, name, kind)

    @radio_station.sub_command(name="edit", description=localized("cmd.radio.station.edit.description"))
    async def radio_station_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            name=localized("cmd.radio.station.edit.opt.name.name"),
            description=localized("cmd.radio.station.edit.opt.name.description"),
            autocomplete=station_commands.server_station_autocomplete,
        ),
    ) -> None:
        await station_commands.edit(self, inter, name)

    @radio_station.sub_command(name="delete", description=localized("cmd.radio.station.delete.description"))
    async def radio_station_delete(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            name=localized("cmd.radio.station.delete.opt.name.name"),
            description=localized("cmd.radio.station.delete.opt.name.description"),
            autocomplete=station_commands.server_station_autocomplete,
        ),
    ) -> None:
        await station_commands.delete(self, inter, name)

    @radio_station.sub_command(name="list", description=localized("cmd.radio.station.list.description"))
    async def radio_station_list(self, inter: disnake.ApplicationCommandInteraction) -> None:
        await station_commands.list_stations(self, inter)

    @radio.sub_command(name="off", description=localized("cmd.radio.off.description"))
    async def radio_off(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
            name=localized("cmd.radio.off.opt.channel.name"),
            description=localized("cmd.radio.off.opt.channel.description"),
        ),
    ) -> None:
        tr = await self._gate(inter)
        await inter.edit_original_message(content=await self._switch_off(inter.author, inter.guild_id, tr, channel))

    async def _switch_off(self, author: Any, guild_id: int, tr: Any, channel: Any) -> str:
        """Desliga a rádio deste canal (qualquer bot do servidor) e devolve o texto da resposta."""
        actor = Actor(user_id=author.id, manage_guild=author.guild_permissions.manage_guild)
        try:
            off = await actions.turn_off(self.hub, guild_id=guild_id, channel_id=channel.id, actor=actor)
        except Refused as refusal:
            raise GenericError(tr(f"radio.refused.{refusal.code}")) from None
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable")) from None
        finally:
            # mesmo que o banco tenha falhado no meio, o que este processo está tocando nesse canal para (e o bot sai da call)
            if actor.manage_guild:
                await self.hub.manager.stop_radios(self.hub.manager.running_in(guild_id, channel.id))
        logger.info("radio.toggle: desligada (%d neste processo)", self.hub.manager.counts().on)
        return tr("radio.off.done", count=len(off), channel=channel.mention)

    @radio.sub_command(name="status", description=localized("cmd.radio.status.description"))
    async def radio_status(self, inter: disnake.ApplicationCommandInteraction) -> None:
        tr = await self._gate(inter)
        await inter.edit_original_message(
            content=await self._status_text(tr, inter.guild_id), allowed_mentions=disnake.AllowedMentions.none()
        )

    async def _status_text(self, tr: Any, guild_id: int) -> str:
        try:
            result = await actions.status(self.hub, guild_id=guild_id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable")) from None
        if result.premium is None:
            return tr("radio.status.no_premium")
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
        return "\n".join(lines)

    # ------------------------------------------------------------------ prefixo ``radio`` (aliases ``rd`` e ``247``)

    @commands.group(name="radio", aliases=["rd", "247"], invoke_without_command=True)
    @commands.check(lambda ctx: ctx.cog.group_check(ctx))
    async def radio_prefix(self, ctx: CustomContext):
        """Sozinho, o status das rádios do servidor; os subcomandos fazem o resto."""
        await self.prefix_status(ctx)

    async def _prefix_gate(self, ctx: CustomContext):
        """Traduz e aplica o cooldown de 10 s por servidor, como o slash."""
        tr = await self._tr(ctx)
        bucket = RADIO_COOLDOWN.get_bucket(ctx.message)
        if bucket is not None and (retry := bucket.update_rate_limit()):
            raise GenericError(tr("radio.cooldown", seconds=int(retry) + 1))
        return tr

    @staticmethod
    def _prefix_channel(ctx: CustomContext, channel: Any, tr: Any) -> Any:
        found = channel or getattr(getattr(ctx.author, "voice", None), "channel", None)
        if found is None:
            raise GenericError(tr("radio.prefix.channel_needed"))
        return found

    @radio_prefix.command(name="status")
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_status(self, ctx: CustomContext):
        tr = await self._prefix_gate(ctx)
        await ctx.send(await self._status_text(tr, ctx.guild.id), allowed_mentions=disnake.AllowedMentions.none())

    @radio_prefix.command(name="on", aliases=["ligar"])
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_on(
        self, ctx: CustomContext, channel: Optional[Union[disnake.VoiceChannel, disnake.StageChannel]] = None
    ):
        tr = await self._prefix_gate(ctx)
        target = self._prefix_channel(ctx, channel, tr)
        await ctx.send(
            await self._turn_on(ctx.author, ctx.guild.id, tr, target), allowed_mentions=disnake.AllowedMentions.none()
        )

    @radio_prefix.command(name="off", aliases=["desligar"])
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_off(
        self, ctx: CustomContext, channel: Optional[Union[disnake.VoiceChannel, disnake.StageChannel]] = None
    ):
        tr = await self._prefix_gate(ctx)
        target = self._prefix_channel(ctx, channel, tr)
        await ctx.send(
            await self._switch_off(ctx.author, ctx.guild.id, tr, target),
            allowed_mentions=disnake.AllowedMentions.none(),
        )

    @radio_prefix.command(name="reset", aliases=["redefinir"])
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_reset(
        self, ctx: CustomContext, channel: Optional[Union[disnake.VoiceChannel, disnake.StageChannel]] = None
    ):
        tr = await self._prefix_gate(ctx)
        content, view = await setup_flow.prepare_reset(
            self, tr, ctx.author, ctx.guild.id, self._prefix_channel(ctx, channel, tr)
        )
        await ctx.send(content, view=view, allowed_mentions=disnake.AllowedMentions.none())

    @radio_prefix.command(name="setup", aliases=["configurar"])
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_setup(self, ctx: CustomContext):
        """A configuração tem seletores (bot, tipo e estação): só existe no comando de barra."""
        tr = await self._tr(ctx)
        await ctx.send(tr("radio.prefix.use_slash"))

    # ------------------------------------------------------------------ premium

    @radio_prefix.command(name="premium", hidden=True)
    @commands.check(lambda ctx: ctx.cog.owner_check(ctx))
    async def radio_premium(self, ctx: CustomContext, action: str = "", guild: str = ""):
        """``radio premium conceder|revogar <id do servidor>`` e ``radio premium listar``."""
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

    @radio_prefix.command(name="pauseall", aliases=["pausartodas"], hidden=True)
    @commands.check(lambda ctx: ctx.cog.owner_check(ctx))
    async def radio_pause_all(self, ctx: CustomContext):
        tr = await self._tr(ctx)
        try:
            changed = await self.hub.control.set_paused_all(True, by=ctx.author.id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
        logger.info("Rádios: pausa geral %s", "ligada" if changed else "já estava ligada")
        await ctx.send(tr("radio.owner.paused_all" if changed else "radio.owner.paused_all_already"))

    @radio_prefix.command(name="resumeall", aliases=["retomartodas"], hidden=True)
    @commands.check(lambda ctx: ctx.cog.owner_check(ctx))
    async def radio_resume_all(self, ctx: CustomContext):
        tr = await self._tr(ctx)
        try:
            changed = await self.hub.control.set_paused_all(False, by=ctx.author.id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
        logger.info("Rádios: pausa geral %s", "desligada" if changed else "já estava desligada")
        await ctx.send(tr("radio.owner.resumed_all" if changed else "radio.owner.resumed_all_already"))

    # ------------------------------------------------------------------ bots só-rádio

    @radio_prefix.command(name="bot", hidden=True)
    @commands.check(lambda ctx: ctx.cog.owner_check(ctx))
    async def radio_bot(self, ctx: CustomContext, action: str = "", identifier: str = ""):
        """``radio bot adicionar|remover <TOKEN_2>`` e ``radio bot listar``: os bots reservas dedicados a rádio."""
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
