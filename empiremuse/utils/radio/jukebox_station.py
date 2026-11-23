"""A estação da Jukebox (T-434 J2b, §2.6, A5): uma estação do servidor ou um gênero pronto no lugar do autoplay.

Estado S, sobreposto a M: a fila das pedidas toca primeiro e, com ela vazia, ``_pn_next_track`` pede aqui a próxima faixa (``next_station_track``)
**antes** do autoplay. A faixa é um ``PartialTrack(autoplay=True)`` resolvido pelo caminho normal de música (``player.resolve_track``); nada do
relay nem do cache de faixas resolvidas da 24 h. R e S são exclusivos.

Este mixin é parte do controlador (``JukeboxRadio`` o herda): o dono de ``player.station`` continua sendo um só, e toda transição roda sob
``player.radio_lock`` com a porta (``rules.radio_mode.gate``, alvo ``STATION``) refeita lá dentro. Nenhuma carga de pool (Last.fm, playlists) roda com
o lock preso: ela vem depois da porta, fora do lock, e o efeito volta a ele com tudo refeito. O que chega de fora é só a ref da estação
(``g:<gênero>`` ou o id de 24 hex), relida no servidor por ``StationStore.get(guild, ref)``: id de outro servidor dá ``None`` (11.9).
"""

from __future__ import annotations

import logging
import random
from enum import Enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from empiremuse.rules.radio_mode import Decision, PlayerState, Target, gate, switch_wait
from empiremuse.rules.radio_shuffle import Track, track_key
from empiremuse.rules.radio_station import is_genre, valid_station_id
from empiremuse.rules.text import external_text
from empiremuse.utils.radio.custom_source import (
    FAIL_STREAK_MAX,
    CustomSession,
    EmptyStation,
    Loaded,
    StreakLimit,
    TransientResolve,
    load_pool,
)
from empiremuse.utils.radio.jukebox_errors import Refused, Reply
from empiremuse.utils.radio.lastfm_feed import LastFmMissing, LastFmUnavailable
from empiremuse.utils.radio.playlist_tracks import PlaylistsUnavailable, radio_playlist_tracks

if TYPE_CHECKING:
    from empiremuse.rules.radio_station import CustomStation
    from empiremuse.utils.radio.jukebox_radio import JukeboxRadio

logger = logging.getLogger(__name__)

STATION_MARK = "jukebox_station"  # em ``info["extra"]``: a ref da estação que sorteou a faixa
SOURCE_NAME = "last.fm"  # a faixa por metadados: o nó a resolve pelos provedores de busca, como o autoplay
NAME_LIMIT = 80


class _NoNegativeCache:
    """A estação da Jukebox NÃO usa o cache negativo global de 24 h (§2.6, isso é da 24 h): faixa ruim só sai do pool desta estação."""

    async def failed(self, track: Track) -> bool:
        return False

    async def mark(self, track: Track) -> None:
        return None


class StationNext(Enum):
    """O resultado do gancho da estação em ``_pn_next_track``."""

    TRACK = "track"  # a faixa seguinte, já resolvida
    ENDED = "ended"  # a estação acabou (S → M): o player segue para o autoplay ou o ocioso
    SUPERSEDED = (
        "superseded"  # outra transição tomou o player (R abrindo, outra estação, player fechando): nada segue daqui
    )


@dataclass
class ActiveStation:
    """A estação que a Jukebox toca. Só o controlador lê isto; a tela lê ``station_view``."""

    ref: str  # ``g:<gênero>`` ou o id de 24 hex da estação do servidor
    name: str  # o nome mostrado (gênero já traduzido), guardado SEM escape: o destino usa ``external_text``
    kind: str
    starter_id: int
    started_at: float  # relógio de parede
    session: CustomSession
    # a faixa sorteada que ainda não provou que toca: (do pool, o PartialTrack)
    pending: Optional[tuple[Track, Any]] = None
    # o encoded que o player de fato mandou ao Lavalink para ela (no caminho alternativo, o de outra plataforma)
    pending_sent: Optional[str] = None
    pick_dropped: bool = False  # a pendente já saiu do pool (uma falha; a nova tentativa ainda pode tocá-la)


class StationView(NamedTuple):
    """A estação como a tela a mostra (§1.5.1, J2b): sem pool, histórico nem quem ligou."""

    ref: str
    name: str
    kind: str
    since: float
    playing: bool  # a faixa atual veio da estação (e não de um pedido)


def is_station_track(track: Any, ref: Optional[str] = None) -> bool:
    info = getattr(track, "info", None)
    extra = info.get("extra") if isinstance(info, dict) else None
    sent = extra.get(STATION_MARK) if isinstance(extra, dict) else None
    return isinstance(sent, str) and (ref is None or sent == ref)


def _requester(track: Any) -> Any:
    try:
        return track.requester
    except AttributeError, KeyError, TypeError:
        return None


class JukeboxStationMixin:
    """A parte S do controlador (``JukeboxRadio``)."""

    # ------------------------------------------------------------------ leitura

    def station_view(self: JukeboxRadio, player: Any) -> Optional[StationView]:
        active = getattr(player, "station", None)
        if active is None:
            return None
        return StationView(
            ref=active.ref,
            name=active.name,
            kind=active.kind,
            since=active.started_at,
            playing=is_station_track(getattr(player, "current", None), active.ref),
        )

    @staticmethod
    def station_state(player: Any, author_id: int) -> PlayerState:
        """O estado para a porta da estação: "fila só com músicas do autor" não pede voto (A5, Q5)."""
        queue = list(player.queue)
        return PlayerState(
            playing=player.current is not None,
            queued=bool(queue),
            radio=player.radio_mode is not None,
            station=getattr(player, "station", None) is not None,
            foreign_queue=any(_requester(t) != author_id for t in queue),
        )

    @staticmethod
    def station_name(tr: Any, station: CustomStation) -> str:
        """O nome mostrado, guardado sem escape: o gênero pronto traduzido no idioma do servidor, a do servidor pelo nome validado."""
        name = tr(f"radio.genre.{station.source.get('genre')}") if is_genre(station.id) else station.name
        return external_text(name, NAME_LIMIT, markdown=False)

    async def station_preview(self: JukeboxRadio, member: Any, player: Any) -> bool:
        """O aviso ``warn.vote`` antes de "Tocar estação": ``True`` se o clique abriria uma votação que não passa no ato. Só prévia."""
        decision = gate(await self._actor(member, player), self.station_state(player, member.id), Target.STATION)
        return decision.decision is Decision.VOTE and self._needed(player) > 1

    # ------------------------------------------------------------------ pré-condições

    async def _station_checks(self: JukeboxRadio, guild_id: int, channel_id: Optional[int]) -> None:
        """Premium (P1) e nenhuma rádio 24 h na call (P12), refeitos em cada efeito. A estação não precisa do nó local."""
        if channel_id is None:
            raise Refused("player.you_must_join_a_voice_2")
        if await self.hub.premium.fresh(guild_id) is None:
            raise Refused("radio.refused.no_premium")
        if self.hub.manager.running_in(guild_id, channel_id):
            raise Refused("jukebox.radio.has_24h")

    async def _station_of(self: JukeboxRadio, guild_id: int, ref: object) -> CustomStation:
        """A estação relida no servidor pelo id do pedido (nunca do componente); outro servidor, apagada ou ``g:`` desconhecido: recusa."""
        station = await self.hub.stations.get(guild_id, ref) if valid_station_id(ref) else None
        if station is None:
            raise Refused("jukebox.station.not_found")
        return station

    async def _station_pool(self: JukeboxRadio, player: Any, station: CustomStation) -> Loaded:
        """O pool da estação (``load_pool``, §2.6). Recusas de hoje: sem faixas, sem ``LASTFM_KEY``, Last.fm fora."""

        async def playlist_tracks(names: Sequence[str]) -> list[Track]:
            return await self._playlist_tracks(player, names)

        try:
            return await load_pool(self.hub, station, playlist_tracks, random.Random())
        except EmptyStation:
            raise Refused("jukebox.station.empty") from None
        except LastFmMissing:
            raise Refused("radio.station.ui.lastfm_missing") from None
        except LastFmUnavailable, PlaylistsUnavailable:
            raise Refused("radio.owner.unavailable") from None

    async def _playlist_tracks(self: JukeboxRadio, player: Any, names: Sequence[str]) -> list[Track]:
        """As faixas das playlists da rádio do servidor (a mesma leitura da rádio personalizada: a URL exata, sem busca)."""
        node = getattr(player, "node", None)
        return await radio_playlist_tracks(self.pool, player.bot, player.guild, player.text_channel, node, names)

    # ------------------------------------------------------------------ ligar (M/R → S) e trocar (S → S)

    async def request_station(self: JukeboxRadio, inter: Any, ref: object) -> Reply:
        """Escolher uma estação em "Rádios do Servidor" (A5): o texto de volta. ``Refused`` traz a recusa.

        A porta vem ANTES do pool: um pedido recusado, ou que só abre votação, não gasta Last.fm nem carga de playlist (a lição S2 da J2a). O pool é
        carregado fora do lock e, de volta a ele, tudo é refeito: a estação relida, a porta e as pré-condições."""
        from empiremuse.utils.music.checks import has_radio_player, is_radio_only, pick_request_bot

        if not valid_station_id(ref):
            raise Refused("jukebox.station.not_found")
        guild_id, member = inter.guild_id, inter.author
        voice = getattr(getattr(member, "voice", None), "channel", None)
        if voice is None:
            raise Refused("player.you_must_join_a_voice_2")
        bot = pick_request_bot(self.pool, guild_id, member)
        if bot is None or is_radio_only(bot) or has_radio_player(bot, guild_id):
            raise Refused("player.all_music_bots_are_in")  # nunca o só-rádio nem o da 24 h
        await self._station_checks(guild_id, voice.id)
        station = await self._station_of(guild_id, ref)
        player = bot.music.players.get(guild_id)
        if player is None:
            player = await self._create_player(inter, bot, voice, local=False)
        guild = bot.get_guild(guild_id)
        member = (guild.get_member(member.id) if guild is not None else None) or member
        async with player.radio_lock:
            opened, _ = await self._station_decision(player, member, station.id, self.station_name(player.tr, station))
            if opened is not None:
                return opened
        try:
            # fora do lock: o Last.fm pode demorar; só com a porta liberando
            loaded = await self._station_pool(player, station)
        except Exception:
            # a carga já foi gasta: a recusa conta como troca para a espera curta [s1], e outro clique logo em seguida não carrega de novo.
            # Fora do lock de propósito: esta escrita só pode adiantar o carimbo de uma troca concorrente (estender a espera curta), nunca abrir
            # uma troca que a espera barraria
            player.radio_switched_at = self.now()
            raise
        async with player.radio_lock:
            # relida no efeito: apagada durante a carga não começa; editada (outra fonte) também não, porque o pool é da fonte aceita
            self._same_source(station, await self._station_of(guild_id, station.id))
            name = self.station_name(player.tr, station)
            # dentro do lock: nada do que valia antes vale agora
            await self._station_checks(guild_id, self._requester_channel(member, player))
            # a porta por último: entre ela e o descarte da fila não há espera (``_start_station`` incrementa o epoch e descarta a fila
            # antes do primeiro ``await``)
            opened, _ = await self._station_decision(player, member, station.id, name)
            if opened is not None:
                return opened
            now = await self._start_station(player, station, loaded, name=name, starter_id=member.id)
        if now:
            await (
                player.process_next()
            )  # fora do lock: o sorteio e a resolução da 1ª faixa não seguram votos nem trocas
        return ("jukebox.station.started_now" if now else "jukebox.station.started_next"), {"station": name}

    @staticmethod
    def _same_source(accepted: CustomStation, current: CustomStation) -> None:
        """O pool foi carregado da estação aceita; se ela foi editada (tipo ou fonte) durante a carga, o pedido não segue com faixas de outra."""
        if (current.kind, current.source) != (accepted.kind, accepted.source):
            raise Refused("jukebox.station.not_found")

    async def _station_decision(
        self: JukeboxRadio, player: Any, member: Any, station_id: str, name: str
    ) -> tuple[Optional[Reply], Optional[int]]:
        """A porta da estação, sob o lock: recusa (``Refused``), voto aberto (a resposta) ou ``None`` (segue para o efeito). Sem efeito nenhum além
        de abrir o voto; o voto que passa no ato (sozinho na call) segue para o efeito como DJ."""
        if player.is_closing:
            raise Refused("jukebox.radio.player_closed")
        channel_id = self._requester_channel(member, player)
        if switch_wait(player.radio_switched_at, self.now()) > 0:
            raise Refused("jukebox.radio.switch_wait")  # [s1]
        decision = gate(await self._actor(member, player), self.station_state(player, member.id), Target.STATION)
        if decision.decision is Decision.DENY:
            raise Refused(decision.reason)
        if decision.decision is Decision.VOTE:
            vote = self._new_vote(player, member, Target.STATION, station_id, name)
            if not self._passes(player, vote):
                return await self._post_vote(player, vote), channel_id
        return None, channel_id

    async def _pass_station(self: JukeboxRadio, player: Any, vote: Any, channel_id: Optional[int]) -> None:
        """O voto de estação passou (sob o lock, chamado por ``_pass``): as pré-condições baratas agora; o pool e o efeito numa tarefa à parte,
        fora do lock, que volta a ele com a cerca de epoch e fecha a mensagem do voto."""
        await self._station_checks(player.guild.id, channel_id)
        station = await self._station_of(player.guild.id, vote.uuid)
        origin = getattr(player, "station", None)  # S → M não mexe no epoch: a estação de antes também é conferida
        self._spawn(self._finish_station_vote(player, vote, station, player.radio_epoch, origin))

    async def _finish_station_vote(
        self: JukeboxRadio, player: Any, vote: Any, station: CustomStation, epoch: int, origin: Any = None
    ) -> None:
        from empiremuse.utils.music import jukebox_ui

        tr = self._tr(player)
        now = False
        await self._freeze_vote_button(player, vote)
        try:
            loaded = await self._station_pool(player, station)
            async with player.radio_lock:
                if player.is_closing:
                    raise Refused("jukebox.radio.player_closed")
                if player.radio_epoch != epoch or getattr(player, "station", None) is not origin:
                    # outra transição (inclusive S → M) veio durante a carga
                    raise Refused("jukebox.radio.vote.expired")
                await self._station_checks(player.guild.id, self._player_channel_id(player))
                self._same_source(station, await self._station_of(player.guild.id, station.id))
                name = self.station_name(tr, station)
                now = await self._start_station(player, station, loaded, name=name, starter_id=vote.author_id)
            text = jukebox_ui.vote_passed_station(tr, name, now=now)
        except Refused as refused:
            text = jukebox_ui.vote_failed(tr, tr(refused.key, **refused.fields))
        except Exception as error:  # noqa: BLE001 - banco ou Last.fm fora: a mensagem fecha mesmo assim; só o tipo vai ao log
            logger.warning("Votação de estação passou, mas o efeito falhou (%s)", type(error).__name__)
            text = jukebox_ui.vote_failed(tr, tr("radio.owner.unavailable"))
        await self._close_vote_message(vote, text)
        if now:
            await player.process_next()

    async def _freeze_vote_button(self: JukeboxRadio, player: Any, vote: Any) -> None:
        """O voto passou e a estação carrega: o botão "Votar" fica desativado até a mensagem fechar (nenhum clique responde "acabou")."""
        import disnake

        if vote.message is None:
            return
        try:
            payload = self._vote_payload(player, vote)
            for component in payload["components"]:
                component.disabled = True
            await vote.message.edit(**payload, allowed_mentions=disnake.AllowedMentions.none())
        except Exception as error:  # noqa: BLE001 - conveniência; a mensagem fecha no fim de qualquer jeito
            logger.warning("Botão da votação não desativado (%s)", type(error).__name__)

    async def _start_station(
        self: JukeboxRadio, player: Any, station: CustomStation, loaded: Loaded, *, name: str, starter_id: int
    ) -> bool:
        """M → S, R → S ou S → S, já sob o lock. Descarta a fila e as sugestões (G1, só no clique que passou). ``True`` = tocar já (o player
        estava ocioso ou em R); senão a estação começa na próxima faixa (a atual termina)."""
        self._bump(player)
        # o descarte logo depois do epoch, antes de qualquer ``await``: o que a porta viu é o que sai; um pedido que entrar durante as
        # esperas abaixo fica na fila e toca antes da estação, como o que chega durante o sorteio
        discarded = len(player.queue)
        player.queue.clear()
        player.queue_autoplay.clear()
        await self._drop_vote(player)
        from_radio = player.radio_mode is not None
        if from_radio:
            await self._leave(player)  # R → S: a rádio acaba agora (revoga a ficha)
        if player.is_closing:
            # destruído durante os awaits: ``forget`` já rodou e não veria a estação nova
            raise Refused("jukebox.radio.player_closed")
        player.station = ActiveStation(
            ref=station.id,
            name=name,
            kind=station.kind,
            starter_id=starter_id,
            started_at=self.wall(),
            session=CustomSession(loaded.pool, _NoNegativeCache(), random.Random()),  # type: ignore[arg-type]
        )
        player.radio_switched_at = self.now()
        self._changed(player)
        logger.info("Estação da Jukebox ligada (%d música(s) descartada(s) da fila)", discarded)
        if from_radio:
            player.last_track = None  # o fim do ao vivo não leva a faixa ao histórico (m1)
            await player.stop()
            return True
        return player.current is None

    # ------------------------------------------------------------------ a próxima faixa (gancho do autoplay, §0.3)

    async def next_station_track(self: JukeboxRadio, player: Any, active: ActiveStation) -> tuple[StationNext, Any]:
        """A faixa seguinte da estação, já resolvida (``TRACK``); ``ENDED`` quando a estação acabou (S → M: o player segue para o autoplay ou o
        ocioso); ``SUPERSEDED`` quando outra transição tomou o player no meio (R abrindo, outra estação, player fechando): aí nada segue daqui e
        quem manda é essa transição, com a cerca de epoch da J2a. A resolução roda sem o lock; a saída de S toma o lock."""
        from empiremuse.utils.music.models import PartialTrack
        from empiremuse.utils.music.models.player_autoqueue import resolve_error

        if player.station is not active:
            return self._after_station(player), None
        try:
            if await self.hub.premium.get(player.guild.id) is None:  # Q4: a cada faixa, com o cache de 30 s
                await self._end_from_hook(player, active)
                return self._after_station(player), None
        except Exception as error:  # noqa: BLE001 - sem como conferir, segue a faixa; só o tipo vai ao log
            logger.warning("Premium da estação da Jukebox não lido (%s)", type(error).__name__)

        async def resolve(track: Track) -> Any:
            partial = PartialTrack(
                title=track.title,
                author=track.artist,
                source_name=SOURCE_NAME,
                requester=player.bot.user.id,
                autoplay=True,
            )
            partial.info.setdefault("extra", {})[STATION_MARK] = active.ref
            try:
                await player.resolve_track(partial)
            except Exception as error:  # noqa: BLE001 - a faixa que não resolve é só a próxima; só o tipo vai ao log
                logger.warning("Faixa da estação da Jukebox não resolvida (%s)", type(error).__name__)
                raise TransientResolve from None
            if partial.id:
                return partial
            if resolve_error(partial):
                raise TransientResolve
            return None

        try:
            pick, partial = await active.session.next_playable(resolve)
        except StreakLimit:
            await self._end_from_hook(player, active, ended=True)  # 5 seguidas sem tocar encerram a estação (§2.6)
            return self._after_station(player), None
        if player.station is not active:
            return self._after_station(player), None
        active.session.streak += (
            1  # só o progresso real zera (``station_track_ended``); uma falha de carga tira a faixa do pool
        )
        active.pending, active.pending_sent, active.pick_dropped = (pick, partial), None, False
        return StationNext.TRACK, partial

    @staticmethod
    def _after_station(player: Any) -> StationNext:
        """A estação não está mais lá: acabou (S → M, segue o de sempre) ou outra transição tomou o player (nada segue daqui)."""
        if player.is_closing or player.radio_mode is not None or getattr(player, "station", None) is not None:
            return StationNext.SUPERSEDED
        return StationNext.ENDED

    def station_track_ended(self: JukeboxRadio, player: Any, ended: Any, reason: object) -> None:
        """O fim de uma faixa, identificada pelo evento (``ended``: o ``encoded`` que o Lavalink mandou, ou a própria faixa), nunca por
        ``last_track`` (no ``REPLACED`` ele já é a faixa nova). Só conta se for a faixa da estação que ainda não provou que toca:
        ``LOAD_FAILED`` é falha (o ``TrackStart`` sai antes de o stream abrir); qualquer outro fim é progresso real e zera a sequência."""
        active = self._pending_station(player, ended)
        if active is None:
            return
        if str(reason).upper() == "LOAD_FAILED":
            self._drop_pick(active)
            return
        active.session.streak = 0
        active.pending, active.pending_sent, active.pick_dropped = None, None, False

    def station_track_failed(self: JukeboxRadio, player: Any, track: Any) -> None:
        """A faixa da estação não carregou ou travou (exceção do Lavalink, ``LOAD_FAILED``, ``TrackStuck``): sai do pool desta estação,
        para a estação não girar para sempre. Já contou na sequência quando foi sorteada."""
        active = self._pending_station(player, track)
        if active is not None:
            self._drop_pick(active)

    def station_track_unused(self: JukeboxRadio, player: Any, track: Any) -> None:
        """Um pedido entrou na fila enquanto a faixa da estação era sorteada: ela não toca (o pedido vem antes) e não conta na sequência."""
        active = getattr(player, "station", None)
        if active is not None and active.pending is not None and active.pending[1] is track:
            active.pending, active.pending_sent, active.pick_dropped = None, None, False
            active.session.streak = max(0, active.session.streak - 1)

    def station_track_sent(self: JukeboxRadio, player: Any, track: Any, encoded: object) -> None:
        """A faixa pendente da estação foi mandada ao Lavalink com ``encoded``: no caminho alternativo (outra plataforma no lugar da
        resolvida) ele difere do ``id`` dela, e é esse que volta nos eventos."""
        active = getattr(player, "station", None)
        if (
            active is not None
            and active.pending is not None
            and active.pending[1] is track
            and isinstance(encoded, str)
        ):
            active.pending_sent = encoded

    @staticmethod
    def _pending_station(player: Any, ended: Any) -> Optional[ActiveStation]:
        active = getattr(player, "station", None)
        if active is None or active.pending is None:
            return None
        partial = active.pending[1]
        if isinstance(ended, str):
            same = ended in (getattr(partial, "id", None), active.pending_sent)
        else:
            same = ended is partial
        return active if same else None

    @staticmethod
    def _drop_pick(active: ActiveStation) -> None:
        """A faixa pendente falhou: sai do pool desta estação (uma vez). A pendência continua: se uma nova tentativa do player a tocar até o
        fim, a sequência zera do mesmo jeito; o próximo sorteio troca a pendência."""
        if active.pending is None or active.pick_dropped:
            return
        active.pick_dropped = True
        pick = active.pending[0]
        session = active.session
        session.pool = [t for t in session.pool if track_key(t) != track_key(pick)] or session.pool

    # ------------------------------------------------------------------ desligar (S → M)

    async def _end_from_hook(self: JukeboxRadio, player: Any, active: ActiveStation, *, ended: bool = False) -> None:
        """S → M a partir do gancho (premium perdido, 5 falhas): sob o lock, como toda transição [m3]; o aviso sai fora dele."""
        async with player.radio_lock:
            if player.is_closing or not await self._drop_station(player, active):
                return
        if ended:
            await self._station_ended_notice(player, active)

    async def _drop_station(self: JukeboxRadio, player: Any, active: ActiveStation) -> bool:
        """Tira a estação, já sob o lock. S → M NÃO incrementa o epoch: nada é descartado, e um pedido de música em curso segue (T-J2B-FIX).
        O voto aberto cai junto, com a mensagem fechada (sob o lock: nunca fica um botão de voto órfão)."""
        if player.station is not active:
            return False
        player.station = None
        await self._drop_vote(player)
        self._changed(player)
        return True

    async def _station_ended_notice(self: JukeboxRadio, player: Any, active: ActiveStation) -> None:
        if player.text_channel is None:
            return
        import disnake

        from empiremuse.utils.music import jukebox_ui

        try:
            await player.text_channel.send(
                jukebox_ui.station_ended(player.tr, active.name, FAIL_STREAK_MAX),
                allowed_mentions=disnake.AllowedMentions.none(),
            )
        except Exception as error:  # noqa: BLE001 - o aviso é conveniência; só o tipo vai ao log
            logger.warning("Aviso de estação encerrada não enviado (%s)", type(error).__name__)

    async def end_station(self: JukeboxRadio, player: Any) -> Optional[str]:
        """O botão de autoplay com a estação ligada a desliga (S → M, permissão do autoplay de hoje). O nome desligado, ou ``None``."""
        async with player.radio_lock:
            active = getattr(player, "station", None)
            if active is None or player.is_closing or not await self._drop_station(player, active):
                return None
            return active.name

    # ------------------------------------------------------------------ estrela do player (§4.2)

    async def toggle_station_favorite(self: JukeboxRadio, inter: Any, player: Any) -> tuple[str, dict[str, Any]]:
        """A estrela do player com a estação tocando: ``custom_favorites`` do membro neste servidor, como no painel da 24 h (com ``g:``)."""
        from empiremuse.utils.radio.custom_favorites import MAX_CUSTOM_FAVORITES
        from empiremuse.utils.radio.premium import RadioUnavailable

        active = getattr(player, "station", None)
        if active is None:
            return "jukebox.station.not_found", {}
        station = await self.hub.stations.get(player.guild.id, active.ref)  # relida: apagada no meio não entra
        if station is None:
            return "jukebox.station.not_found", {}
        try:
            result = await self.hub.custom_favorites.toggle(player.guild.id, inter.author.id, station.id)
        except RadioUnavailable:
            return "radio.owner.unavailable", {}
        return f"radio.panel.custom.favorite_{result.value}", {
            "station": external_text(active.name, NAME_LIMIT),
            "max": MAX_CUSTOM_FAVORITES,
        }
