"""As portas reais do ``RadioRunner`` (item 8, R1): Discord e Lavalink de UM bot reserva tocando UMA rádio.

Isto é só cola fina: as decisões (recuo, presença, limites, premium) são do ``RadioRunner``. Cada rádio é um ``LavalinkPlayer`` comum,
marcado com ``radio_id`` (os ganchos de ``player_voice`` mandam presença e queda ao ``RadioManager``), sem painel de skin, com
``keep_connected`` e a fila dando a volta (``loop = "queue"``). A faixa virtual é a lista de faixas da playlist do servidor girando no
tempo desde que a rádio ligou (``rules.radio.virtual_position``): ao voltar de uma pausa a fila é rotacionada para a faixa e a posição
certas. O texto externo (título, nome de playlist) nunca é publicado por aqui.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.radio import Radio
from empiremuse.utils.radio.chat import PermissionLost
from empiremuse.utils.radio.runner import Allowed

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore, BotPool

logger = logging.getLogger(__name__)

STAGE_MODERATOR_PERMISSIONS = (
    "manage_channels",
    "mute_members",
    "move_members",
)  # o que criar um evento de palco exige
STAGE_TOPIC_LIMIT = 120
PANEL_CLEANUP_LIMIT = 50  # mensagens do canal que se olham atrás de painéis antigos do próprio bot


class RadioBotUnavailable(Exception):
    """O bot da rádio (ou o servidor dele) ainda não está pronto: o runner trata como "não sei agora" e tenta de novo."""


class DiscordRadioPorts:
    def __init__(self, pool: BotPool, radio: Radio) -> None:
        self.pool = pool
        self.radio = radio
        self._tracks: list[Any] = []
        self._stage_created = False  # o evento de palco foi criado por este bot (e só esse ele encerra)
        self._stage_noticed = False

    # ------------------------------------------------------------------ o mundo

    def _bot(self) -> Optional[BotCore]:
        return next(
            (b for b in self.pool.get_all_bots() if b.user is not None and b.user.id == self.radio.bot_id), None
        )

    def _guild(self) -> Optional[disnake.Guild]:
        bot = self._bot()
        return bot.get_guild(self.radio.guild_id) if bot is not None else None

    def _channel(self) -> Any:
        guild = self._guild()
        return guild.get_channel(self.radio.channel_id) if guild is not None else None

    def _player(self) -> Any:
        bot = self._bot()
        return bot.music.players.get(self.radio.guild_id) if bot is not None else None

    # ------------------------------------------------------------------ RadioPorts

    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def clock_origin(self) -> float | None:
        """A época gravada em que a programação virtual começou (sobrevive ao reinício do bot)."""
        return self.radio.clock_started_at

    def jitter(self) -> float:
        return random.random()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def allowed(self) -> Allowed:
        """R-1: o premium (cache de 30 s) e o canal/permissão de agora. Falha do banco propaga e o runner trata como "não sei"."""
        bot, guild = self._bot(), self._guild()
        if bot is None or not getattr(bot, "bot_ready", True) or guild is None:
            # na subida o bot reserva (ou o servidor) ainda não está no cache: "não sei agora", NUNCA "canal apagado"
            raise RadioBotUnavailable
        if await self.pool.radio.premium.get(self.radio.guild_id) is None:
            return Allowed.NO_PREMIUM
        stored = await self.pool.radio.store.get(self.radio.id)
        if stored is None or not stored.enabled:
            return Allowed.DISABLED  # desligada por fora (comando, painel, revogação): este runner não pode seguir
        channel = self._channel()
        if channel is None:
            return Allowed.NO_CHANNEL
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.connect):
            return Allowed.NO_PERMISSION
        return Allowed.YES

    async def connect(self) -> bool:
        """Entra (ou volta) na call; no palco pede a palavra UMA vez por conexão. Qualquer falha é ``False`` e o runner recua."""
        bot, guild, channel = self._bot(), self._guild(), self._channel()
        if bot is None or guild is None or channel is None:
            return False
        try:
            player = self._player() or await self._create_player(bot, guild, channel)
            await player.connect(channel.id, self_deaf=True)
            if isinstance(channel, disnake.StageChannel):
                await self._speak(guild, channel, player)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Rádio não conectou (%s)", type(error).__name__)
            return False
        return True

    async def _create_player(self, bot: BotCore, guild: disnake.Guild, channel: Any) -> Any:
        from empiremuse.utils.music.models import LavalinkPlayer  # tarde: modelos importam o cliente

        node = await self._pick_node(bot)
        player = bot.music.get_player(
            guild_id=guild.id,
            cls=LavalinkPlayer,
            node_id=node.identifier,
            guild=guild,
            channel=channel,
            player_creator=bot.user.id,
            static=False,
            keep_connected=True,
        )
        player.radio_id = self.radio.id
        player.controller_mode = False
        self._configure_player(player)
        player.tr = await bot.pool.i18n.for_guild(guild)
        return player

    async def _pick_node(self, bot: BotCore) -> Any:
        return await bot.get_cog("Music").get_best_node(bot)

    def _configure_player(self, player: Any) -> None:
        player.loop = "queue"  # a playlist dá a volta; a ao vivo troca isto (``LiveRadioPorts``)

    async def _speak(self, guild: disnake.Guild, channel: disnake.StageChannel, player: Any) -> None:
        """Palco: garante o evento de palco ao vivo (sem ele o Discord recusa o pedido de fala com 403 50001), depois ``suppress=False`` com
        ``MUTE_MEMBERS``; sem isso, ``request_to_speak``. Um 403 vale UMA tentativa por conexão (R-5)."""
        try:
            await self._ensure_stage_instance(guild, channel, player)
            if channel.permissions_for(guild.me).mute_members:
                await guild.me.edit(suppress=False)
            else:
                await guild.me.request_to_speak()
        except disnake.Forbidden:
            logger.warning("Rádio sem permissão para falar no palco")

    async def _stage_name(self) -> object:
        return await self._panel_name(self.radio)

    async def _ensure_stage_instance(self, guild: disnake.Guild, channel: disnake.StageChannel, player: Any) -> None:
        """Sem evento de palco ao vivo, cria um (tópico = nome da estação ou da playlist, já limpo; só para o servidor) se o bot é moderador de palco;
        sem a permissão, avisa UMA vez quais faltam. Só o evento que o próprio bot criou é encerrado quando a rádio desliga."""
        if channel.instance is not None:
            return
        permissions = channel.permissions_for(guild.me)
        missing = [name for name in STAGE_MODERATOR_PERMISSIONS if not getattr(permissions, name, False)]
        if missing:
            await self._stage_notice(channel, player, missing)
            return
        from empiremuse.rules.text import external_text  # tarde: só o palco usa

        topic = external_text(await self._stage_name(), STAGE_TOPIC_LIMIT, markdown=False) or player.tr(
            "radio.stage.topic"
        )
        try:
            await channel.create_instance(
                topic=topic, privacy_level=disnake.StagePrivacyLevel.guild_only, reason="Rádio"
            )
        except (disnake.Forbidden, disnake.HTTPException) as error:
            logger.warning("Evento de palco da rádio não criado (%s)", type(error).__name__)
            return
        self._stage_created = True

    async def _stage_notice(self, channel: disnake.StageChannel, player: Any, missing: list[str]) -> None:
        """Um aviso claro e traduzido no chat do palco com as permissões que faltam (no máximo um por palco a cada minuto e um por ativação)."""
        if self._stage_noticed or not self.pool.radio.claim_notice(("stage", channel.id)):
            return
        self._stage_noticed = True
        names = ", ".join(f"**{player.tr(f'radio.stage.perm.{name}')}**" for name in missing)
        try:
            await channel.send(
                player.tr("radio.stage.no_instance", permissions=names), allowed_mentions=disnake.AllowedMentions.none()
            )
        except (disnake.Forbidden, disnake.HTTPException) as error:
            logger.warning("Aviso do palco não enviado (%s)", type(error).__name__)

    async def end_stage(self) -> None:
        """A rádio desligou: encerra o evento de palco SE foi o próprio bot quem o criou (o de uma pessoa nunca é tocado)."""
        if not self._stage_created:
            return
        self._stage_created = False
        instance = getattr(self._channel(), "instance", None)
        if instance is None:
            return
        try:
            await instance.delete(reason="Rádio desligada")
        except (disnake.Forbidden, disnake.NotFound, disnake.HTTPException) as error:
            logger.warning("Evento de palco da rádio não encerrado (%s)", type(error).__name__)

    async def _load_tracks(self) -> list[Any]:
        if self._tracks:
            return self._tracks
        bot, guild = self._bot(), self._guild()
        playlist = await self.pool.servers.playlists.find(self.radio.guild_id, self.radio.source.get("playlist", ""))
        if bot is None or guild is None or playlist is None:
            return []
        music = bot.get_cog("Music")
        shim = SimpleNamespace(guild=guild, guild_id=guild.id, author=guild.me, bot=bot, channel=self._channel())
        player = self._player()
        # source=False: a URL exata, sem "ytsearch:" nem Deezer/Spotify; no nó do player quando há (como o controlador, controller_embed.py)
        found, _ = await music.get_tracks(
            playlist.url, shim, guild.me, source=False, node=getattr(player, "node", None), bot=bot
        )
        self._tracks = list(getattr(found, "tracks", found) or [])
        return self._tracks

    async def prepare(self) -> None:
        if not await self._load_tracks():
            raise RuntimeError("playlist sem faixas")

    async def play_virtual(self, index: int, position_ms: int) -> None:
        player = self._player()
        tracks = await self._load_tracks()
        if player is None or not tracks:
            raise RuntimeError("sem player ou sem faixas")
        player.queue.clear()
        player.queue.extend(tracks[index:] + tracks[:index])
        await player.process_next(start_position=position_ms)

    async def skip_track(self) -> None:
        """Botão "próxima": a fila da rádio dá a volta (``loop = "queue"``), então o próximo da fila toca; o relógio virtual segue."""
        player = self._player()
        if player is not None:
            await player.process_next()

    async def _panel_name(self, current: Radio) -> object:
        return current.source.get("playlist", "")

    def _panel_view(self) -> Any:
        return None  # a ao vivo mostra título, reserva no ar e fora do ar (``LiveRadioPorts``)

    async def _custom_view(self) -> Any:
        return None  # a personalizada por estação mostra a faixa no ar e o crédito do Last.fm (``CustomRadioPorts``)

    async def _station_card(self, current: Radio) -> Any:
        return None  # só a rádio ao vivo tem estação (``LiveRadioPorts``)

    @staticmethod
    def _avatar_url(guild: disnake.Guild) -> str | None:
        """A foto do bot para a miniatura de reserva; sem ela (cache incompleto) o painel sai sem miniatura em vez de falhar."""
        try:
            return guild.me.display_avatar.replace(size=256).url
        except AttributeError:
            return None

    async def _logo(self, current: Radio) -> Any:
        return None  # só a rádio ao vivo tem logo de estação (``LiveRadioPorts``)

    @staticmethod
    def edit_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
        """Os argumentos de ``message.edit``: com um anexo novo (o logo), troca os anexos de antes em vez de empilhar."""
        return {**payload, "attachments": []} if "file" in payload else dict(payload)

    async def _panel_payload(self, bot: BotCore, guild: disnake.Guild, current: Radio) -> dict[str, Any]:
        """O painel do tipo desta rádio (ao vivo ou personalizada) com o visual da Jukebox: ícones do bot, miniatura e cor."""
        from empiremuse.utils.music.app_emojis import emoji as app_emoji  # tarde: o módulo importa o cliente
        from empiremuse.utils.radio.panel import panel_payload

        tr = await bot.pool.i18n.for_guild(guild)
        live = self.radio.kind == "live"
        return panel_payload(
            tr,
            playlist_name="" if live else await self._panel_name(current),
            color=bot.get_color(guild.me),
            live=live,
            view=self._panel_view(),
            custom=await self._custom_view(),
            station=await self._station_card(current),
            emojis=lambda name: app_emoji(bot, name),
            avatar=self._avatar_url(guild),
            logo=await self._logo(current),
        )

    async def _refresh_existing(self, message: Any, bot: BotCore, guild: disnake.Guild, current: Radio) -> None:
        """O painel que já existia (de antes de uma atualização do bot, ou do reinício) é redesenhado uma vez com o visual de agora."""
        try:
            payload = await self._panel_payload(bot, guild, current)
            await message.edit(allowed_mentions=disnake.AllowedMentions.none(), **self.edit_kwargs(payload))
        except Exception as error:  # noqa: BLE001 - o painel é conveniência: só o tipo vai ao log (R-12)
            logger.warning("Painel da rádio não redesenhado (%s)", type(error).__name__)

    async def _remove_old_panels(self, channel: Any, bot: BotCore) -> None:
        """Apaga os painéis de rádio que ESTE bot deixou no canal (mensagens dele com os botões do painel). Só dele: o painel da Jukebox e os avisos de
        outros bots não têm esses botões e ficam. Falha só vai para o log (tipo)."""
        from empiremuse.utils.radio.panel import is_radio_panel  # tarde: o painel importa a camada de texto

        try:
            old = [
                message
                async for message in channel.history(limit=PANEL_CLEANUP_LIMIT)
                if message.author.id == bot.user.id and not message.pinned and is_radio_panel(message)
            ]
            for message in old:
                await message.delete()
        except Exception as error:  # noqa: BLE001 - limpar é conveniência: o painel novo sai de qualquer jeito; só o tipo vai ao log (R-12)
            logger.warning("Painéis antigos da rádio não apagados (%s)", type(error).__name__)

    async def ensure_panel(self) -> None:
        """Garante a mensagem de controle no chat da call: mantém a que ainda existe, manda uma nova se sumiu. Sem permissão de enviar: só avisa."""
        bot, guild, channel = self._bot(), self._guild(), self._channel()
        hub = self.pool.radio
        current = await hub.store.get(self.radio.id)
        if bot is None or guild is None or channel is None or current is None:
            return
        if current.panel_message_id:
            try:
                existing = await channel.fetch_message(current.panel_message_id)
                hub.manager.note_panel(self.radio.id, existing.id)
                await self._refresh_existing(existing, bot, guild, current)
                return
            except disnake.NotFound:
                pass  # sumiu: manda outra
            except disnake.Forbidden:
                logger.warning("Painel da rádio sem permissão para ler o chat da call")
                return
        payload = await self._panel_payload(bot, guild, current)
        await self._remove_old_panels(channel, bot)  # um id de painel perdido não deixa painéis se acumulando no canal
        try:
            message = await channel.send(allowed_mentions=disnake.AllowedMentions.none(), **payload)
        except disnake.Forbidden:
            logger.warning("Painel da rádio sem permissão para enviar no chat da call")
            return
        await hub.store.set_panel_message(self.radio.id, message.id)
        hub.manager.note_panel(self.radio.id, message.id)

    async def stop_stream(self) -> None:
        player = self._player()
        if player is None:
            return
        try:
            player.queue.clear()
            await player.stop()
        except Exception as error:  # noqa: BLE001
            logger.warning("Rádio sem fechar o fluxo (%s)", type(error).__name__)

    def listeners(self) -> int:
        channel = self._channel()
        members = getattr(channel, "members", None) or []
        return sum(1 for m in members if not m.bot and not (m.voice.deaf or m.voice.self_deaf))

    def paused_all(self) -> bool:
        return self.pool.radio.control.paused_all

    async def turn_off(self, reason: Allowed) -> None:
        """Perdeu o direito: a rádio vira DESLIGADA (a transição do contador é da store) e o bot sai da call."""
        hub = self.pool.radio
        try:
            await hub.store.set_enabled(self.radio.id, False, updated_by="system")
        finally:
            await hub.manager.deactivate(self.radio.id)
            player = self._player()
            if player is not None:
                try:
                    await player.destroy()
                except Exception as error:  # noqa: BLE001
                    logger.warning("Rádio sem sair da call (%s)", type(error).__name__)

    def durations_ms(self) -> list[int]:
        return [int(getattr(t, "duration", 0) or 0) for t in self._tracks]


class DiscordChatPorts:
    """O chat da call de UMA rádio para o ``ChatSweeper``: amarrado ao canal da rádio (nunca ao da Jukebox), 403 vira ``PermissionLost``."""

    def __init__(self, pool: BotPool, radio: Radio) -> None:
        self.pool = pool
        self.radio = radio

    def _channel(self) -> Any:
        bot = next((b for b in self.pool.get_all_bots() if b.user is not None and b.user.id == self.radio.bot_id), None)
        guild = bot.get_guild(self.radio.guild_id) if bot is not None else None
        return guild.get_channel(self.radio.channel_id) if guild is not None else None

    @property
    def stage(self) -> bool:
        return isinstance(self._channel(), disnake.StageChannel)

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def fetch_recent(self, limit: int) -> list[tuple[int, float, bool]]:
        """``(id, criada_em, protegida)``. Protegidas: a fixada, o painel da rádio, o que qualquer bot do pool escreveu e mensagem de sistema."""
        channel = self._channel()
        if channel is None:
            return []
        current = await self.pool.radio.store.get(self.radio.id)  # o id do painel pode ter mudado depois da ativação
        panel_id = current.panel_message_id if current is not None else self.radio.panel_message_id
        pool_bots = {
            b.user.id for b in self.pool.get_all_bots() if b.user
        }  # o painel da Jukebox e os avisos de outros bots do pool ficam
        try:
            messages = [m async for m in channel.history(limit=limit)]
        except disnake.Forbidden as error:
            raise PermissionLost from error
        from empiremuse.utils.radio.panel import is_radio_panel  # tarde: o painel importa a camada de texto

        def orphan(m: Any) -> bool:
            """Painel de rádio do PRÓPRIO bot que já não é o de agora (o id se perdeu): lixo que se acumularia, não aviso a proteger."""
            return (
                panel_id is not None
                and m.author.id == self.radio.bot_id
                and m.id != panel_id
                and not m.pinned
                and is_radio_panel(m)
            )

        return [
            (
                m.id,
                m.created_at.timestamp(),
                bool(
                    m.pinned
                    or (
                        not orphan(m)
                        and (
                            m.id == panel_id
                            or m.author.id == self.radio.bot_id
                            or m.author.id in pool_bots
                            or m.is_system()
                        )
                    )
                ),
            )
            for m in messages
        ]

    async def bulk_delete(self, ids: Sequence[int]) -> None:
        channel = self._channel()
        if channel is None or not ids:
            return
        try:
            await channel.delete_messages([disnake.Object(id=i) for i in ids])
        except disnake.Forbidden as error:
            raise PermissionLost from error
