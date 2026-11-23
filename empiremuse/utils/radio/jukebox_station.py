"""A estação da Jukebox (T-434 J2b, §2.6, A5): uma estação do servidor ou um gênero pronto no lugar do autoplay.

Estado S, sobreposto a M: a fila das pedidas toca primeiro e, com ela vazia, ``_pn_next_track`` pede aqui a próxima faixa (``next_station_track``)
**antes** do autoplay. A faixa é um ``PartialTrack(autoplay=True)`` resolvido pelo caminho normal de música (``player.resolve_track``); nada do
relay nem do cache de faixas resolvidas da 24 h. R e S são exclusivos.

Este mixin é parte do controlador (``JukeboxRadio`` o herda): o dono de ``player.station`` continua sendo um só, e toda transição roda sob
``player.radio_lock`` com a porta (``rules.radio_mode.gate``, alvo ``STATION``) refeita lá dentro. O que chega de fora é só a ref da estação
(``g:<gênero>`` ou o id de 24 hex), relida no servidor por ``StationStore.get(guild, ref)``: id de outro servidor dá ``None`` (11.9).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from empiremuse.rules.playlists import SCOPE_RADIO
from empiremuse.rules.radio_mode import Decision, PlayerState, Target, gate, switch_wait
from empiremuse.rules.radio_shuffle import Track
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
from empiremuse.utils.radio.lastfm_feed import LastFmMissing, LastFmUnavailable

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


@dataclass
class ActiveStation:
    """A estação que a Jukebox toca. Só o controlador lê isto; a tela lê ``station_view``."""

    ref: str  # ``g:<gênero>`` ou o id de 24 hex da estação do servidor
    name: str  # o nome mostrado (gênero já traduzido), guardado SEM escape: o destino usa ``external_text``
    kind: str
    starter_id: int
    started_at: float  # relógio de parede
    session: CustomSession


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
        from empiremuse.utils.radio.jukebox_radio import Refused

        if channel_id is None:
            raise Refused("player.you_must_join_a_voice_2")
        if await self.hub.premium.fresh(guild_id) is None:
            raise Refused("radio.refused.no_premium")
        if self.hub.manager.running_in(guild_id, channel_id):
            raise Refused("jukebox.radio.has_24h")

    async def _station_of(self: JukeboxRadio, guild_id: int, ref: object) -> CustomStation:
        """A estação relida no servidor pelo id do pedido (nunca do componente); outro servidor, apagada ou ``g:`` desconhecido: recusa."""
        from empiremuse.utils.radio.jukebox_radio import Refused

        station = await self.hub.stations.get(guild_id, ref) if valid_station_id(ref) else None
        if station is None:
            raise Refused("jukebox.station.not_found")
        return station

    async def _station_pool(self: JukeboxRadio, player: Any, station: CustomStation) -> Loaded:
        """O pool da estação (``load_pool``, §2.6). Recusas de hoje: sem faixas, sem ``LASTFM_KEY``, Last.fm fora."""
        from empiremuse.utils.radio.jukebox_radio import Refused

        async def playlist_tracks(names: Sequence[str]) -> list[Track]:
            return await self._playlist_tracks(player, names)

        try:
            return await load_pool(self.hub, station, playlist_tracks, random.Random())
        except EmptyStation:
            raise Refused("jukebox.station.empty") from None
        except LastFmMissing:
            raise Refused("radio.station.ui.lastfm_missing") from None
        except LastFmUnavailable:
            raise Refused("radio.owner.unavailable") from None

    async def _playlist_tracks(self: JukeboxRadio, player: Any, names: Sequence[str]) -> list[Track]:
        """As faixas das playlists da rádio do servidor (a mesma leitura da rádio personalizada: a URL exata, sem busca)."""
        bot, guild = player.bot, player.guild
        music = bot.get_cog("Music")
        if music is None:
            return []
        shim = SimpleNamespace(guild=guild, guild_id=guild.id, author=guild.me, bot=bot, channel=player.text_channel)
        found: list[Track] = []
        for name in names:
            playlist = await self.pool.servers.playlists.find(guild.id, name, scope=SCOPE_RADIO)
            if playlist is None:
                continue
            result, _ = await music.get_tracks(
                playlist.url, shim, guild.me, source=False, node=getattr(player, "node", None), bot=bot
            )
            for track in list(getattr(result, "tracks", result) or []):
                author, title = str(getattr(track, "author", "") or ""), str(getattr(track, "title", "") or "")
                if author.strip() and title.strip():
                    found.append(Track(author, title))
        return found

    # ------------------------------------------------------------------ ligar (M/R → S) e trocar (S → S)

    async def request_station(self: JukeboxRadio, inter: Any, ref: object) -> tuple[str, dict[str, Any]]:
        """Escolher uma estação em "Rádios do Servidor" (A5): o texto de volta. ``Refused`` traz a recusa."""
        from empiremuse.utils.music.checks import has_radio_player, is_radio_only, pick_request_bot
        from empiremuse.utils.radio.jukebox_radio import Refused

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
        name = self.station_name(player.tr, station)
        loaded = await self._station_pool(
            player, station
        )  # antes do lock: o Last.fm pode demorar; nada muda até o efeito
        async with player.radio_lock:
            if player.is_closing:
                raise Refused("jukebox.radio.player_closed")
            channel_id = self._requester_channel(member, player)
            if switch_wait(player.radio_switched_at, self.now()) > 0:
                raise Refused("jukebox.radio.switch_wait")  # [s1]
            decision = gate(await self._actor(member, player), self.station_state(player, member.id), Target.STATION)
            if decision.decision is Decision.DENY:
                raise Refused(decision.reason)
            if decision.decision is Decision.VOTE:
                vote = self._new_vote(player, member, Target.STATION, station.id, name)
                if not self._passes(player, vote):
                    return await self._post_vote(player, vote)
            await self._station_checks(guild_id, channel_id)  # dentro do lock: nada do que valia antes vale agora
            now = await self._start_station(player, station, loaded, name=name, starter_id=member.id)
        if now:
            await (
                player.process_next()
            )  # fora do lock: o sorteio e a resolução da 1ª faixa não seguram votos nem trocas
        return ("jukebox.station.started_now" if now else "jukebox.station.started_next"), {"station": name}

    async def _start_station(
        self: JukeboxRadio, player: Any, station: CustomStation, loaded: Loaded, *, name: str, starter_id: int
    ) -> bool:
        """M → S, R → S ou S → S, já sob o lock. Descarta a fila e as sugestões (G1, só no clique que passou). ``True`` = tocar já (o player
        estava ocioso ou em R); senão a estação começa na próxima faixa (a atual termina)."""
        self._bump(player)
        await self._drop_vote(player)
        from_radio = player.radio_mode is not None
        if from_radio:
            await self._leave(player)  # R → S: a rádio acaba agora (revoga a ficha)
        discarded = len(player.queue)
        player.queue.clear()
        player.queue_autoplay.clear()
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

    async def next_station_track(self: JukeboxRadio, player: Any, active: ActiveStation) -> Any:
        """A faixa seguinte da estação, já resolvida, ou ``None`` (a estação acabou ou mudou; o player segue para o autoplay normal ou o
        ocioso). Sem o lock: roda dentro do ``process_next`` que uma transição sob o lock pode ter chamado."""
        from empiremuse.utils.music.models import PartialTrack
        from empiremuse.utils.music.models.player_autoqueue import resolve_error

        if player.station is not active:
            return None
        try:
            if await self.hub.premium.get(player.guild.id) is None:  # Q4: a cada faixa, com o cache de 30 s
                await self._drop_station(player, active)
                return None
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
            _, partial = await active.session.next_playable(resolve)
        except StreakLimit:
            await self._drop_station(player, active, ended=True)  # 5 seguidas sem tocar encerram a estação (§2.6)
            return None
        active.session.streak += 1  # zera quando a faixa começa de fato (``station_track_started``)
        return partial if player.station is active else None

    def station_track_started(self: JukeboxRadio, player: Any) -> None:
        """Uma faixa da estação começou a tocar: a sequência de falhas zera (resolveu E tocou)."""
        active = getattr(player, "station", None)
        if active is not None and is_station_track(getattr(player, "current", None), active.ref):
            active.session.streak = 0

    # ------------------------------------------------------------------ desligar (S → M)

    async def _drop_station(self: JukeboxRadio, player: Any, active: ActiveStation, *, ended: bool = False) -> None:
        """Tira a estação sem o lock (atribuições síncronas; o gancho roda dentro de um ``process_next``). ``ended`` avisa no canal."""
        if player.station is not active:
            return
        player.station = None
        self._bump(player)
        await self._drop_vote(player)
        self._changed(player)
        if ended and player.text_channel is not None:
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
            if active is None:
                return None
            await self._drop_station(player, active)
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
