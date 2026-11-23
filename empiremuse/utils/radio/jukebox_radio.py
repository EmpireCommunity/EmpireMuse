"""O modo rádio da Jukebox (T-434 J2a, §2): o ÚNICO dono de ``player.radio_mode``, ``radio_vote``, ``radio_epoch`` e do token do relay.

Estados do player: M (música ou ocioso) e R (ao vivo). Toda transição, voto, passagem, expiração e queda roda sob ``player.radio_lock`` e
refaz a porta (``rules.radio_mode.gate``) e as pré-condições lá dentro [m3]. O que chega de fora é só o uuid da estação (relido pelo
diretório, s6) e o ``nonce`` do botão de voto (que não autoriza nada, s2). Nada de ``user_id`` com estação no log [m7].
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

import disnake

from empiremuse.rules.icy import TITLE_LIMIT as ICY_TITLE_LIMIT
from empiremuse.rules.radio_mode import (
    LIVE_RETRY_S,
    VOTE_CAP_DEFAULT,
    VOTE_TTL_S,
    Actor,
    Decision,
    PlayerState,
    Target,
    cooldown_wait,
    gate,
    next_retry,
    switch_wait,
    vote_passes,
)
from empiremuse.rules.radio_stations import Station, valid_uuid
from empiremuse.rules.text import stored_text
from empiremuse.utils.music.live_mark import mark_live
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.jukebox_station import JukeboxStationMixin
from empiremuse.utils.radio.live import StationFailure
from empiremuse.utils.radio.live_play import LOCAL_NODE, LocalNodeUnavailable, local_node, open_stream

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

FLOW_WAIT_S = 25.0  # como a 24 h: o primeiro áudio tem de chegar ao Lavalink neste prazo
Reply = tuple[str, dict[str, Any]]  # a chave do texto e os campos dela: quem responde traduz


class Refused(Exception):
    """Recusa com a chave de texto e os campos dela; a superfície traduz e responde em mensagem efêmera."""

    def __init__(self, key: str, **fields: Any) -> None:
        super().__init__(key)
        self.key, self.fields = key, fields


@dataclass
class RadioMode:
    """O ao vivo que a Jukebox toca agora. Nome e título guardados SEM escape de markdown e já sem link nem menção (MJ1).

    Só o controlador lê isto: carrega token, faixa e tarefas. As telas leem ``JukeboxRadio.view`` (§1.5.1)."""

    uuid: str
    name: str  # o nome do diretório, como veio (escapado por ``check_station``): o destino usa ``stored_text``
    country: str
    starter_id: int
    started_at: float  # relógio de parede: "no ar há {n} min"
    favicon: str = ""  # https já limpo pelo diretório; nunca vai cru para embed, componente ou painel (s5)
    codec: str = ""
    bitrate: int = 0
    place_node: Optional[str] = None  # nó da árvore de local, quando a estação veio do seletor por local
    title: str = ""  # título ICY, sem escape
    attempt: int = 0
    retry_at: Optional[float] = None  # relógio de parede da próxima tentativa
    on_air: bool = False  # o áudio chegou (``wait_flowing``); False na abertura e na reconexão
    token: Optional[str] = None
    track: Any = None  # ``None`` = abertura ou reconexão em curso: o player não age sobre o modo (rev. 3.3)
    on_air_since: Optional[float] = None  # relógio do controlador: zera as tentativas (``RETRY_RESET_S``)
    fault: bool = False  # alguma tentativa falhou por culpa da estação: conta UMA vez na saúde ao esgotar (Q5)
    retry_task: Optional[asyncio.Task] = None
    status_task: Optional[asyncio.Task] = None


@dataclass
class RadioVote:
    nonce: str
    author_id: int
    target: Target
    uuid: Optional[str]  # o alvo guardado AQUI, nunca no botão
    station_name: str
    epoch: int
    expires_at: float = 0.0  # relógio de parede, para ``<t:..:R>``
    voters: set[int] = field(default_factory=set)
    place_node: Optional[str] = None
    message: Any = None
    task: Optional[asyncio.Task] = None


class VoteView(NamedTuple):
    """A votação aberta como a tela a mostra (§1.5.1): sem votantes, autor nem ``nonce`` (m7)."""

    target: Target
    votes: int  # votos válidos agora (``voter_ok`` de cada votante, recalculado)
    needed: int  # quórum agora (Q1)
    expires_at: float


class RadioView(NamedTuple):
    """A cópia imutável do modo rádio que a apresentação lê (§1.5.1): sem token, URL, faixa, tarefa nem ``starter_id``."""

    uuid: str
    name: str
    title: str
    country: str
    place_node: Optional[str]
    favicon: str
    codec: str
    bitrate: int
    since: float
    on_air: bool
    attempt: int
    retry_at: Optional[float]
    vote: Optional[VoteView]


def voter_ok(member: Any, player: Any) -> bool:
    """O ÚNICO predicado de elegível [m5]: humano, na call do bot agora, sem ``self_deaf`` nem ``deaf``. Vale para votar e para o quórum."""
    if member is None or getattr(member, "bot", False):
        return False
    voice = getattr(member, "voice", None)
    bot_voice = getattr(getattr(getattr(player, "guild", None), "me", None), "voice", None)
    channel = getattr(bot_voice, "channel", None)
    if voice is None or channel is None or getattr(voice.channel, "id", None) != channel.id:
        return False
    return not (getattr(voice, "self_deaf", False) or getattr(voice, "deaf", False))


def eligible_humans(player: Any) -> list[Any]:
    channel = getattr(getattr(getattr(player.guild, "me", None), "voice", None), "channel", None)
    return [m for m in getattr(channel, "members", ()) if voter_ok(m, player)]


def _changed(player: Any) -> None:
    """O gancho único da apresentação (§1.5.1): algo que a visão mostra mudou. Quem desenha decide o debounce."""
    hook = getattr(player, "radio_view_changed", None)
    if callable(hook):
        with suppress(Exception):
            hook()


class JukeboxRadio(JukeboxStationMixin):
    """``pool.radio.jukebox``: o modo rádio ao vivo (R) e, pelo mixin, a estação (S, J2b). Relógio, parede e espera injetáveis (os testes não dormem)."""

    def __init__(
        self,
        pool: BotPool,
        hub: RadioHub,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.pool, self.hub = pool, hub
        self.now, self.wall, self.sleep = clock, wall, sleep
        self._tasks: set[asyncio.Task] = set()

    _changed = staticmethod(_changed)

    # ------------------------------------------------------------------ apoio

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def vote_cap(self) -> int:
        config = getattr(self.pool, "config", None)
        value = config.get("VOTE_SKIP_AMOUNT") if isinstance(config, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else VOTE_CAP_DEFAULT

    @staticmethod
    def _tr(player: Any) -> Callable[..., str]:
        return player.tr

    @staticmethod
    def state_of(player: Any) -> PlayerState:
        return PlayerState(
            playing=player.current is not None,
            queued=bool(player.queue),
            radio=player.radio_mode is not None,
            station=getattr(player, "station", None) is not None,
        )

    async def _actor(self, member: Any, player: Any) -> Actor:
        from empiremuse.utils.db import DBModel
        from empiremuse.utils.music.checks import can_act_as_dj

        guild_data = await player.bot.get_data(player.guild.id, db_name=DBModel.guilds)
        return Actor(is_dj=can_act_as_dj(member, player, guild_data), restricted=bool(player.restrict_mode))

    async def _entry_checks(self, guild_id: int, channel_id: Optional[int], bot: Any) -> None:
        """As pré-condições de entrar em R, refeitas em cada efeito (início, troca, passagem do voto): premium, P12 e nó local."""
        if channel_id is None:
            raise Refused("player.you_must_join_a_voice_2")
        if await self.hub.premium.fresh(guild_id) is None:
            raise Refused("radio.refused.no_premium")
        if self.hub.manager.running_in(guild_id, channel_id):
            raise Refused("jukebox.radio.has_24h")  # P12: a call já tem uma rádio 24 h
        try:
            local_node(bot)
        except LocalNodeUnavailable:
            raise Refused("jukebox.radio.no_local_node") from None

    # ------------------------------------------------------------------ entrar (M → R) e trocar (R → R)

    async def request_live(self, inter: Any, uuid: str, *, place_node: Optional[str] = None) -> Reply:
        """ "Tocar na minha call" (picker, busca, ``/fav_manager``): o texto de volta. ``Refused`` traz a recusa."""
        from empiremuse.utils.music.checks import has_radio_player, is_radio_only, pick_request_bot

        if not valid_uuid(uuid):
            raise Refused("radio.live.not_found")
        guild_id, member = inter.guild_id, inter.author
        voice = getattr(getattr(member, "voice", None), "channel", None)
        if voice is None:
            raise Refused("player.you_must_join_a_voice_2")
        bot = pick_request_bot(self.pool, guild_id, member)
        if bot is None or is_radio_only(bot) or has_radio_player(bot, guild_id):
            raise Refused("player.all_music_bots_are_in")  # nunca o só-rádio nem o da 24 h (A4)
        await self._entry_checks(guild_id, voice.id, bot)
        if self.hub.health.unavailable(uuid):
            raise Refused("radio.refused.station_down")
        player = bot.music.players.get(guild_id)
        if player is None:
            player = await self._create_player(inter, bot, voice)
        guild = bot.get_guild(guild_id)
        member = (guild.get_member(member.id) if guild is not None else None) or member
        async with player.radio_lock:
            if player.is_closing:
                raise Refused("jukebox.radio.player_closed")
            # dentro do lock, de novo: quem pediu está AGORA na call deste player (Minor 3); a P12 olha essa call
            channel_id = self._requester_channel(member, player)
            if switch_wait(player.radio_switched_at, self.now()) > 0:
                raise Refused("jukebox.radio.switch_wait")  # [s1, S3]: vale também para começos seguidos
            decision = gate(await self._actor(member, player), self.state_of(player), Target.LIVE)
            if decision.decision is Decision.DENY:
                raise Refused(decision.reason)
            if decision.decision is Decision.VOTE:
                self._may_open_vote(player, member)  # antes do diretório: não gasta consulta num pedido recusado (S2)
                station = await self._station_for_vote(uuid)
                vote = self._new_vote(player, member, Target.LIVE, uuid, station.name, place_node=place_node)
                if not self._passes(player, vote):
                    return await self._post_vote(player, vote)
                # sozinho (ou o único elegível): n = 1, passa no ato e segue como DJ (Q1)
            await self._entry_checks(
                guild_id, channel_id, bot
            )  # dentro do lock: nada do que valia antes de esperar vale agora
            station = await self._start(player, uuid, starter_id=member.id, place_node=place_node)
        return "jukebox.radio.started", {"station": stored_text(station.name, 80)}

    @staticmethod
    def _player_channel_id(player: Any) -> Optional[int]:
        channel = getattr(getattr(getattr(player.guild, "me", None), "voice", None), "channel", None)
        if channel is not None:
            return channel.id
        channel_id = getattr(player, "channel_id", None)
        return int(channel_id) if channel_id else None

    def _requester_channel(self, member: Any, player: Any) -> int:
        """A call do player, se quem pediu está nela agora; senão ``Refused`` (dois pedidos de calls diferentes no mesmo bot livre)."""
        voice = getattr(getattr(member, "voice", None), "channel", None)
        if voice is None:
            raise Refused("player.you_must_join_a_voice_2")
        if self._player_channel_id(player) != voice.id:
            raise Refused("jukebox.radio.other_call")
        return voice.id

    async def _station_for_vote(self, uuid: str) -> Station:
        try:
            station = await self.hub.directory.by_uuid_strict(uuid)
        except DirectoryError:
            raise Refused("radio.owner.unavailable") from None
        if station is None:
            raise Refused("radio.live.not_found")
        return station

    async def _create_player(self, inter: Any, bot: Any, voice: Any, *, local: bool = True) -> Any:
        """O player novo da Jukebox conectado na call de quem pediu; no nó local para o ao vivo (o relay só atende o Lavalink deste host),
        no melhor nó para a estação (``local=False``)."""
        from empiremuse.utils.db import DBModel
        from empiremuse.utils.music.checks import update_attr

        cog = bot.get_cog("Music")
        guild = bot.get_guild(inter.guild_id)
        if cog is None or guild is None:
            raise Refused("player.all_music_bots_are_in")
        update_attr(inter, bot, guild)
        guild_data = await bot.get_data(guild.id, db_name=DBModel.guilds)
        channel = bot.get_channel(inter.channel_id) or inter.channel
        player = await cog.create_player(
            inter=inter,
            bot=bot,
            guild=guild,
            guild_data=guild_data,
            channel=channel,
            node=local_node(bot) if local else None,
        )
        if not player.is_connected:
            await cog.do_connect(
                inter,
                channel=bot.get_channel(voice.id) or voice,
                check_other_bots_in_vc=guild_data["check_other_bots_in_vc"],
                bot=bot,
                me=guild.me,
            )
        return player

    async def _start(self, player: Any, uuid: str, *, starter_id: int, place_node: Optional[str] = None) -> Station:
        """M → R ou R → R, já sob o lock e com as pré-condições conferidas. Descarta fila, autoplay e repetir (G1) e abre o fluxo."""
        try:
            station = await self.hub.directory.fresh_for_play(uuid)
        except DirectoryError:
            raise Refused("radio.owner.unavailable") from None  # o diretório não respondeu: não é culpa da estação
        if station is None:
            raise Refused("radio.live.not_found")
        previous = player.radio_mode
        self._bump(player)
        await self._drop_vote(player)  # uma transição real anula o voto aberto e fecha a mensagem dele (m2, MI1)
        if previous is not None:
            self._cancel_tasks(previous)
            await self._revoke(previous)
        discarded = len(player.queue)
        player.queue.clear()
        player.queue_autoplay.clear()
        player.loop = False
        player.station = None  # M/S → R: a estação desliga (R e S são exclusivos)
        mode = RadioMode(
            uuid=station.uuid,
            name=station.name,
            country=station.country,
            starter_id=starter_id,
            started_at=self.wall(),
            favicon=station.favicon,
            codec=station.codec,
            bitrate=station.bitrate,
            place_node=place_node,
        )
        player.radio_mode = mode
        player.radio_switched_at = self.now()
        _changed(player)
        try:
            await self._to_local(
                player
            )  # parar ANTES de trocar de nó: nada da faixa velha é reenviado ao nó local (m1)
            if player.radio_mode is not mode:
                raise Refused("jukebox.radio.player_closed")  # o player foi destruído durante a espera (m5)
            await self._open(player, mode, station)
        except Refused:
            await self._abort(player, mode)
            raise
        except StationFailure:
            await self._abort(player, mode)
            raise Refused("radio.refused.station_down") from None
        except LocalNodeUnavailable:
            await self._abort(player, mode)
            raise Refused("jukebox.radio.no_local_node") from None
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Modo rádio da Jukebox não abriu (%s)", type(error).__name__)
            await self._abort(player, mode)
            raise Refused("radio.owner.unavailable") from None
        logger.info("Modo rádio da Jukebox iniciado (%d música(s) descartada(s) da fila)", discarded)
        return station

    async def _abort(self, player: Any, mode: RadioMode) -> None:
        """A abertura falhou: ocioso se o modo ainda é este; se o player foi destruído no meio (``forget``), só a ficha é revogada (m5)."""
        if player.radio_mode is mode and not player.is_closing:
            await self._to_idle(player, mode)
            return
        self._cancel_tasks(mode)
        await self._revoke(mode)

    @staticmethod
    async def _to_local(player: Any) -> None:
        """Para o que toca e leva o player ao nó LOCAL (o relay só atende o Lavalink deste host, R-8)."""
        if player.current is not None:
            await player.stop()  # o fim (STOPPED) é consumido pelo modo rádio: nada de próxima música
        if player.node is None or player.node.identifier != LOCAL_NODE:
            local_node(player.bot)
            await player.change_node(LOCAL_NODE)

    async def _open(self, player: Any, mode: RadioMode, station: Station) -> None:
        def keep(token: str) -> None:
            mode.token = token

        def on_track(track: Any) -> None:
            mark_live(track)  # a marca vale na própria faixa, mesmo depois de ``radio_mode = None`` (m1)
            mode.track = track

        mode.on_air = False
        track = await open_stream(
            self.hub,
            player.bot,
            player,
            station,
            on_title=lambda title: self._on_title(player, mode, title),
            author=self._author(player, mode),
            volume=player.volume,
            keep_token=keep,
            release=lambda: self._revoke(mode),
            still_wanted=lambda: player.radio_mode is mode,
            flow_wait=FLOW_WAIT_S,
            on_track=on_track,
        )
        mode.track = track
        mode.on_air_since = self.now()
        mode.on_air, mode.retry_at = True, None
        _changed(player)

    def _author(self, player: Any, mode: RadioMode) -> str:
        return mode.title or str(self._tr(player)("radio.live.status_author"))

    def _on_title(self, player: Any, mode: RadioMode, title: str) -> None:
        """O título do fluxo chega do relay já limpo (R-11, ``clean_title``): guardado SEM escape e sem link nem menção (MJ1)."""
        if player.radio_mode is not mode:
            return
        mode.title = stored_text(title, ICY_TITLE_LIMIT, markdown=False) if title else ""
        _changed(player)
        track = mode.track
        if track is None or track.author == self._author(player, mode):
            return
        track.author = self._author(player, mode)
        if mode.status_task is None or mode.status_task.done():
            mode.status_task = self._spawn(self._refresh_status(player))

    @staticmethod
    async def _refresh_status(player: Any) -> None:
        try:
            await player.update_stage_topic(reconnect=False)
        except Exception as error:  # noqa: BLE001 - o status do canal é conveniência: só o tipo vai ao log (R-12)
            logger.warning("Status do canal do modo rádio não atualizado (%s)", type(error).__name__)

    # ------------------------------------------------------------------ sair (R → M e R → ocioso)

    @staticmethod
    def _bump(player: Any) -> None:
        player.radio_epoch += 1

    @staticmethod
    def _cancel_tasks(mode: RadioMode) -> None:
        for task in (mode.retry_task, mode.status_task):
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()

    async def _revoke(self, mode: RadioMode) -> None:
        token, mode.token = mode.token, None
        if token is not None:
            with suppress(Exception):
                await self.hub.relay.revoke(token)

    async def _leave(self, player: Any, mode: Optional[RadioMode] = None) -> None:
        """Tira o player de R (sob o lock): ``radio_mode = None``, revoga o token, cancela voto e tarefas. Não toca nada."""
        mode = mode or player.radio_mode
        if mode is None:
            return
        if player.radio_mode is mode:
            player.radio_mode = None
        self._bump(player)
        await self._drop_vote(player)
        self._cancel_tasks(mode)
        await self._revoke(mode)
        _changed(player)

    async def _to_idle(self, player: Any, mode: Optional[RadioMode] = None) -> None:
        """R → ocioso: sai de R e o player fica ocioso, SEM próximo item nem autoplay (§2.2)."""
        from empiremuse.utils.music.models.playback_steps import go_idle

        await self._leave(player, mode)
        player.queue.clear()
        await go_idle(player)

    async def release(self, player: Any, mode: RadioMode) -> bool:
        """O player saiu de R por conta própria (a fila acabou, ⏭, ou uma música entrou no lugar do ao vivo). ``mode`` = o modo que o player
        viu: sob o lock, só sai se ele ainda é o de agora (rev. 3.3, achado M1), para um ``process_next`` atrasado nunca derrubar a rádio que
        acabou de abrir. ``True`` se saiu."""
        async with player.radio_lock:
            if player.radio_mode is not mode:
                return False
            await self._leave(player, mode)
            return True

    async def leave_preview(self, member: Any, player: Any) -> Optional[Decision]:
        """A PRÉVIA do pedido de música em R, só para o passo de confirmação escolher o texto: ``None`` fora de R, ``DIRECT`` (DJ, ou um voto que
        passaria no ato) ou ``VOTE``; ``Refused`` sem confirmação (modo restrito, voto aberto, autor em espera). Sem lock e sem efeito: a porta de
        verdade é ``leave_for_music``, refeita no clique."""
        if player.radio_mode is None:
            return None
        decision = gate(await self._actor(member, player), self.state_of(player), Target.LEAVE)
        if decision.decision is Decision.DENY:
            raise Refused(decision.reason)
        if decision.decision is Decision.VOTE:
            self._may_open_vote(player, member)
            if self._needed(player) > 1:
                return Decision.VOTE
        return Decision.DIRECT

    async def live_preview(self, member: Any, player: Any) -> bool:
        """O aviso ``warn.vote`` do passo "Estação escolhida": ``True`` se o clique abriria uma votação que não passa no ato. Só prévia."""
        if player is None or not self.state_of(player).busy:
            return False
        decision = gate(await self._actor(member, player), self.state_of(player), Target.LIVE)
        return decision.decision is Decision.VOTE and self._needed(player) > 1

    async def leave_for_music(self, inter: Any, player: Any) -> None:
        """O ponto único de interceptação do pedido de música em R (§2.2): DJ sai na hora; membro abre o voto (plano B, Q3); modo restrito recusa.

        Volta normalmente quando o pedido pode seguir; ``Refused`` quando não pode (o voto foi aberto, ou a recusa)."""
        member = player.guild.get_member(inter.author.id) or inter.author
        async with player.radio_lock:
            # antes do ``radio_mode``: ``forget`` zera o modo de um player destruído na espera do lock (Crivo S-2)
            if player.is_closing:
                raise Refused("jukebox.radio.player_closed")
            if player.radio_mode is None:
                return
            # o clique pode vir até 45 s depois do pedido (passo de confirmação): quem pediu ainda está na call do player (Sentinela N1)
            self._requester_channel(member, player)
            decision = gate(await self._actor(member, player), self.state_of(player), Target.LEAVE)
            if decision.decision is Decision.DENY:
                raise Refused(decision.reason)
            if decision.decision is Decision.VOTE:
                self._may_open_vote(player, member)
                vote = self._new_vote(player, member, Target.LEAVE, None, player.radio_mode.name)
                if not self._passes(player, vote):
                    key, fields = await self._post_vote(player, vote)
                    raise Refused(key, **fields)
            await self._leave(player)
            player.last_track = None  # o fim (STOPPED) do ao vivo não leva a faixa ao histórico (m1)
            with suppress(Exception):
                await player.stop()  # o pedido segue e toca no lugar do ao vivo

    async def forget(self, player: Any) -> None:
        """O player foi destruído (``stop``, call vazia, saída do bot): revoga o token e cancela voto e tentativas, sem tarefa solta."""
        mode, player.radio_mode = player.radio_mode, None
        player.station = None
        self._bump(player)
        await self._drop_vote(player)
        if mode is not None:
            self._cancel_tasks(mode)
            await self._revoke(mode)

    # ------------------------------------------------------------------ visão (§1.5.1)

    def view(self, player: Any) -> Optional[RadioView]:
        """A cópia imutável do modo rádio para as telas e as skins: ``None`` fora de R. Síncrona, sem lock e sem I/O (um passo só)."""
        mode = getattr(player, "radio_mode", None)
        if mode is None:
            return None
        vote = getattr(player, "radio_vote", None)
        vote_view = (
            VoteView(vote.target, len(self._valid(player, vote)), self._needed(player), vote.expires_at)
            if vote is not None
            else None
        )
        return RadioView(
            uuid=mode.uuid,
            name=mode.name,
            title=mode.title,
            country=mode.country,
            place_node=mode.place_node,
            favicon=mode.favicon,
            codec=mode.codec,
            bitrate=mode.bitrate,
            since=mode.started_at,
            on_air=mode.on_air,
            attempt=mode.attempt if not mode.on_air else 0,
            retry_at=mode.retry_at,
            vote=vote_view,
        )

    # ------------------------------------------------------------------ votação (§2.3)

    async def _drop_vote(self, player: Any) -> None:
        """Uma transição ou a destruição anula o voto aberto: cancela o prazo e fecha a mensagem ("A votação acabou. Nada mudou."), sem botão."""
        from empiremuse.utils.music import jukebox_ui

        vote = self._cancel_vote(player)
        if vote is not None:
            await self._close_vote_message(vote, jukebox_ui.vote_expired(self._tr(player)))

    def _may_open_vote(self, player: Any, member: Any) -> None:
        """Pode abrir um voto agora? Um por player, o cooldown do autor depois de um expirado [m5], e o autor tem de poder votar (S6)."""
        if player.radio_vote is not None:
            raise Refused("jukebox.radio.vote.running")
        if (wait := cooldown_wait(player.radio_vote_expired.get(member.id), self.now())) > 0:
            raise Refused("jukebox.radio.vote.cooldown", when=f"<t:{int(self.wall() + wait)}:R>")  # [m5]
        if not voter_ok(member, player):
            raise Refused("jukebox.radio.vote.not_eligible")

    def _cancel_vote(self, player: Any) -> Optional[RadioVote]:
        vote, player.radio_vote = player.radio_vote, None
        if (
            vote is not None
            and vote.task is not None
            and not vote.task.done()
            and vote.task is not asyncio.current_task()
        ):
            vote.task.cancel()
        return vote

    def _new_vote(
        self,
        player: Any,
        member: Any,
        target: Target,
        uuid: Optional[str],
        station_name: str,
        *,
        place_node: Optional[str] = None,
    ) -> RadioVote:
        """Um voto novo (ainda não guardado), já sob o lock. O pedido conta como o 1º voto."""
        self._may_open_vote(player, member)
        return RadioVote(
            nonce=secrets.token_hex(4),
            author_id=member.id,
            target=target,
            uuid=uuid,
            station_name=station_name,
            epoch=player.radio_epoch,
            expires_at=self.wall() + VOTE_TTL_S,
            voters={member.id},
            place_node=place_node,
        )

    async def _post_vote(self, player: Any, vote: RadioVote) -> Reply:
        """Guarda o voto, envia a mensagem no canal do player (menção só do autor) e arma o prazo de ``VOTE_TTL_S``."""
        from empiremuse.utils.music import jukebox_ui

        channel = player.text_channel
        if channel is None:
            raise Refused("radio.owner.unavailable")
        player.radio_vote = vote
        try:
            vote.message = await channel.send(
                **self._vote_payload(player, vote),
                allowed_mentions=disnake.AllowedMentions(
                    users=[disnake.Object(vote.author_id)], roles=False, everyone=False
                ),
            )
        except Exception as error:  # noqa: BLE001 - sem a mensagem não há votação; só o tipo vai ao log
            player.radio_vote = None
            logger.warning("Votação do modo rádio não enviada (%s)", type(error).__name__)
            raise Refused("radio.owner.unavailable") from None
        vote.task = self._spawn(self._expire_later(player, vote.nonce))
        _changed(player)
        return "jukebox.radio.vote.opened", {"channel": f"<#{channel.id}>", "seconds": VOTE_TTL_S}

    def _vote_payload(self, player: Any, vote: RadioVote) -> dict[str, Any]:
        from empiremuse.utils.music import jukebox_ui

        return jukebox_ui.vote_payload(
            self._tr(player),
            nonce=vote.nonce,
            author_id=vote.author_id,
            target=vote.target,
            station_name=vote.station_name,
            votes=len(self._valid(player, vote)),
            needed=self._needed(player),
            expires_at=vote.expires_at,
        )

    def _valid(self, player: Any, vote: RadioVote) -> set[int]:
        """Só contam os votos de quem AINDA passa em ``voter_ok`` (saiu da call ou ensurdeceu: deixa de contar)."""
        members = {m.id for m in eligible_humans(player)}
        return vote.voters & members

    def _needed(self, player: Any) -> int:
        from empiremuse.rules.radio_mode import quorum

        return quorum(len(eligible_humans(player)), self.vote_cap())

    def _passes(self, player: Any, vote: RadioVote) -> bool:
        return vote_passes(len(self._valid(player, vote)), len(eligible_humans(player)), self.vote_cap())

    async def vote_click(self, inter: Any, nonce: str) -> Reply:
        """O clique em "✋ Votar": o player vem da call de quem clicou (``resolve_player``), o ``nonce`` só descarta voto velho."""
        from empiremuse.utils.music.checks import resolve_player

        resolved = resolve_player(self.pool, inter.guild_id, inter.author)
        if resolved is None:
            return "jukebox.radio.vote.over", {}
        _, player = resolved
        member = player.guild.get_member(inter.author.id) or inter.author
        async with player.radio_lock:
            vote = player.radio_vote
            if vote is None or not secrets.compare_digest(vote.nonce, str(nonce)):
                return "jukebox.radio.vote.over", {}
            if vote.epoch != player.radio_epoch:
                await self._drop_vote(player)  # anulado, não expirado: sem o cooldown do autor (Crivo S1)
                return "jukebox.radio.vote.over", {}
            if not voter_ok(member, player):
                return "jukebox.radio.vote.not_eligible", {}
            if member.id in vote.voters:
                return "jukebox.radio.vote.already", {}
            vote.voters.add(member.id)  # DJ ou admin que vota conta como 1 voto comum
            if self._passes(player, vote):
                await self._pass(player)
                return "jukebox.radio.vote.counted", {}
            with suppress(Exception):
                await vote.message.edit(
                    **self._vote_payload(player, vote), allowed_mentions=disnake.AllowedMentions.none()
                )
            _changed(player)
            return "jukebox.radio.vote.counted", {}

    async def _pass(self, player: Any) -> None:
        """Passou (sob o lock): lê e limpa o voto de uma vez e executa o alvo como DJ, refazendo TODAS as pré-condições (§2.3)."""
        from empiremuse.utils.music import jukebox_ui

        vote = self._cancel_vote(player)
        if vote is None:
            return
        tr = self._tr(player)
        author = player.guild.get_member(vote.author_id)
        mention = getattr(author, "mention", f"<@{vote.author_id}>")
        try:
            if vote.epoch != player.radio_epoch:
                raise Refused("jukebox.radio.vote.expired")
            channel_id = self._player_channel_id(player)
            if vote.target is Target.LIVE and vote.uuid is not None:
                await self._entry_checks(player.guild.id, channel_id, player.bot)
                station = await self._start(player, vote.uuid, starter_id=vote.author_id, place_node=vote.place_node)
                text = jukebox_ui.vote_passed_live(tr, station.name)
            elif vote.target is Target.STATION and vote.uuid is not None:
                # tudo refeito: premium, 24 h na call, a estação relida no servidor e o pool (J2b)
                await self._station_checks(player.guild.id, channel_id)
                found = await self._station_of(player.guild.id, vote.uuid)
                loaded = await self._station_pool(player, found)
                name = self.station_name(tr, found)
                if await self._start_station(player, found, loaded, name=name, starter_id=vote.author_id):
                    self._spawn(player.process_next())  # fora do lock: o sorteio da 1ª faixa não segura o voto
                text = jukebox_ui.vote_passed_station(tr, name)
            else:
                await self._to_idle(player)
                text = jukebox_ui.vote_passed_leave(tr, mention)
        except Refused as refused:
            text = jukebox_ui.vote_failed(tr, tr(refused.key, **refused.fields))
        except Exception as error:  # noqa: BLE001 - banco ou diretório fora (S1): a mensagem fecha mesmo assim; só o tipo vai ao log
            logger.warning("Votação do modo rádio passou, mas o efeito falhou (%s)", type(error).__name__)
            text = jukebox_ui.vote_failed(tr, tr("radio.owner.unavailable"))
        await self._close_vote_message(vote, text, mention_id=vote.author_id if vote.target is Target.LEAVE else None)

    async def _expire(self, player: Any, vote: RadioVote) -> None:
        from empiremuse.utils.music import jukebox_ui

        if player.radio_vote is vote:
            self._cancel_vote(player)
        player.radio_vote_expired[vote.author_id] = self.now()
        await self._close_vote_message(vote, jukebox_ui.vote_expired(self._tr(player)))
        _changed(player)

    async def _expire_later(self, player: Any, nonce: str) -> None:
        await self.sleep(VOTE_TTL_S)
        async with player.radio_lock:
            vote = player.radio_vote
            if vote is None or vote.nonce != nonce:
                return
            if vote.epoch == player.radio_epoch and self._passes(player, vote):
                await self._pass(player)  # recalculado no prazo: quem saiu da call pode ter baixado o quórum
                return
            await self._expire(player, vote)

    @staticmethod
    async def _close_vote_message(vote: RadioVote, text: str, *, mention_id: Optional[int] = None) -> None:
        if vote.message is None:
            return
        mentions = (
            disnake.AllowedMentions(users=[disnake.Object(mention_id)], roles=False, everyone=False)
            if mention_id is not None
            else disnake.AllowedMentions.none()
        )
        with suppress(Exception):
            await vote.message.edit(content=text, embed=None, components=[], allowed_mentions=mentions)

    # ------------------------------------------------------------------ queda (§2.4)

    def on_stream_lost(self, player: Any, mode: Optional[RadioMode] = None) -> None:
        """O fluxo caiu (fim, exceção ou travada do Lavalink em R, ou o player saiu do nó local): o recuo roda numa tarefa, sob o lock.

        ``mode`` = o modo que quem chama viu (rev. 3.3). A faixa sai do modo na hora: até a nova abertura o player não age sobre ele
        (``_pn_next_track`` não faz nada) e evento nenhum da faixa velha conta como queda."""
        current = player.radio_mode
        if current is None or (mode is not None and current is not mode):
            return
        current.track, current.on_air = None, False
        _changed(player)  # "reconectando" aparece já, mesmo com uma volta em curso (Alicerce N-s3)
        if current.retry_task is not None and not current.retry_task.done():
            return  # já há uma volta em curso (exceção e fim do mesmo fluxo)
        current.retry_task = self._spawn(self._recover(player, current, player.radio_epoch))

    async def _recover(self, player: Any, mode: RadioMode, epoch: int) -> None:
        relay = self.hub.relay
        async with player.radio_lock:
            if player.radio_mode is not mode or player.radio_epoch != epoch:
                return
            if mode.token is not None and relay.reached(mode.token) and not relay.limited(mode.token):
                mode.fault = True  # o relay recusar por política não é culpa da estação
            await self._revoke(mode)
        while True:
            retry = next_retry(mode.attempt, mode.on_air_since, self.now())
            mode.attempt = retry.attempt
            if retry.delay is None:
                await self._exhausted(player, mode, epoch)
                return
            mode.on_air_since = None
            mode.retry_at = self.wall() + retry.delay
            _changed(player)
            await self.sleep(retry.delay)
            async with player.radio_lock:
                if player.radio_mode is not mode or player.radio_epoch != epoch or player.is_closing:
                    return
                if not await self._reopen(player, mode):
                    continue
                return

    async def resume_live(self, player: Any) -> None:
        """Retomar em R toca o ao vivo DE AGORA (A6): um fluxo novo pelo relay. Falhou: segue o recuo da queda (2, 4 e 8 s)."""
        async with player.radio_lock:
            mode = player.radio_mode
            if mode is None:
                return
            mode.track, mode.on_air = None, False
            _changed(player)
            await self._revoke(mode)
            if await self._reopen(player, mode):
                return
            epoch = player.radio_epoch
        if mode.retry_task is None or mode.retry_task.done():
            mode.retry_task = self._spawn(self._recover(player, mode, epoch))

    async def _reopen(self, player: Any, mode: RadioMode) -> bool:
        """Uma nova tentativa (sob o lock): premium de novo (Q4), estação relida (``fresh_for_play``) e ficha nova do relay."""
        try:
            if await self.hub.premium.fresh(player.guild.id) is None:
                mode.attempt = len(LIVE_RETRY_S)  # sem premium a Jukebox para já, sem contar na saúde
                return False
            station = await self.hub.directory.fresh_for_play(mode.uuid)
            if station is None:
                mode.fault = True
                return False
            await self._to_local(player)  # de volta ao nó LOCAL, sem reenviar nada (Minor 2)
            await self._open(player, mode, station)
            return True
        except StationFailure:
            mode.fault = True
            return False
        except Exception as error:  # noqa: BLE001 - diretório, Lavalink ou Discord: não é culpa da estação; só o tipo vai ao log
            logger.warning("Nova tentativa do modo rádio falhou (%s)", type(error).__name__)
            return False

    async def _exhausted(self, player: Any, mode: RadioMode, epoch: int) -> None:
        from empiremuse.utils.music import jukebox_ui

        async with player.radio_lock:
            if player.radio_mode is not mode or player.radio_epoch != epoch:
                return
            if mode.fault:
                with suppress(Exception):
                    await self.hub.health.record_exhausted(mode.uuid)  # Q5: uma vez, ao esgotar
            await self._to_idle(player, mode)
        channel = player.text_channel
        if channel is None:
            return
        member = player.guild.get_member(mode.starter_id)
        mention = getattr(member, "mention", f"<@{mode.starter_id}>")
        with suppress(Exception):
            await channel.send(
                jukebox_ui.dropped_text(self._tr(player), mode.name, mention),
                allowed_mentions=disnake.AllowedMentions(
                    users=[disnake.Object(mode.starter_id)], roles=False, everyone=False
                ),
            )
        logger.info("Modo rádio da Jukebox parou depois de %d tentativas", mode.attempt)

    # ------------------------------------------------------------------ estrela do player (§4.2)

    async def toggle_favorite(self, inter: Any, player: Any) -> Reply:
        """A estrela do player em R vai para as MINHAS favoritas ao vivo (o uuid vem do player, o nome é relido pelo diretório, s6)."""
        from empiremuse.utils.radio.custom_favorites import Toggled
        from empiremuse.utils.radio.favorites import FavoriteNotFound
        from empiremuse.utils.radio.premium import RadioUnavailable
        from empiremuse.utils.radio.user_favorites import USER_LIVE_FAVORITES_MAX

        mode = player.radio_mode
        if mode is None:
            return "radio.live.not_found", {}
        try:
            result = await self.hub.user_favorites.toggle_station(inter.author.id, mode.uuid)
        except FavoriteNotFound:
            return "radio.live.not_found", {}
        except RadioUnavailable:
            return "radio.owner.unavailable", {}
        if result is Toggled.FULL:
            return "radio.favorites.mine.full", {"max": USER_LIVE_FAVORITES_MAX}
        station = stored_text(mode.name, 80)
        return ("radio.favorites.mine.added" if result is Toggled.ADDED else "radio.favorites.mine.removed"), {
            "station": station
        }
