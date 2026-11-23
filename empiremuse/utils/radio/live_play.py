"""O miolo de tocar uma estação AO VIVO pelo relay (T-434 §0.2): o mesmo para a rádio 24 h (``live_ports``) e para o modo rádio da Jukebox.

O Lavalink nunca recebe a URL da estação: o relay registra um token e o nó ``LOCAL`` toca ``http://127.0.0.1:<porta>/r/<token>`` (R-8). A faixa
criada sai sem o endereço local em ``uri`` e ``identifier`` [m1]; o ``track.id`` (codificado) ainda o carrega e por isso nunca é gravado por quem
chama. O nome da estação vem guardado com escape (``check_station``) e a faixa leva o texto SEM escape e já sem link nem menção (R-11/MJ1).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from empiremuse.rules.radio_stations import NAME_LIMIT as STATION_NAME_LIMIT, Station
from empiremuse.rules.text import stored_text
from empiremuse.utils.radio.live import StationFailure

LOCAL_NODE = "LOCAL"


class LocalNodeUnavailable(RuntimeError):
    """O nó ``LOCAL`` ainda não existe (na subida a rádio tenta antes do Lavalink local registrar): o recuo tenta de novo; o log mostra este tipo, não um ``RuntimeError`` mudo."""


def local_node(bot: Any) -> Any:
    node = bot.music.nodes.get(LOCAL_NODE)
    if node is None:
        raise LocalNodeUnavailable("sem nó local")  # o relay só atende o Lavalink deste host
    return node


def station_title(station: Station) -> str:
    """O título da faixa ao vivo: o nome da estação sem escape de markdown e sem link nem menção (o destino escapa, se renderizar markdown)."""
    return stored_text(station.name, STATION_NAME_LIMIT, markdown=False) or "—"


async def open_stream(
    hub: Any,
    bot: Any,
    player: Any,
    station: Station,
    *,
    on_title: Callable[[str], None],
    author: str,
    volume: int,
    keep_token: Callable[[str], None],
    release: Callable[[], Any],
    still_wanted: Callable[[], bool] = lambda: True,
    flow_wait: float,
    on_track: Callable[[Any], None] | None = None,
) -> Any:
    """Abre ``station`` pelo relay e manda o player tocá-la; devolve a faixa ao vivo. Qualquer falha propaga (quem chama decide a nova tentativa).

    ``keep_token`` guarda o token ANTES da primeira espera (uma revogação no meio o encontra); ``release`` o revoga. ``on_track`` recebe a faixa antes
    de ela entrar na fila (o modo rádio da Jukebox a reconhece no ``process_next``). ``StationFailure`` = a culpa é da estação (o relay chegou a
    abri-la e o fluxo não veio); qualquer outro erro não conta para a saúde dela.
    """
    from empiremuse.utils.music.models import LavalinkPlaylist, LavalinkTrack  # tarde: os modelos importam o cliente

    local_node(bot)  # sem nó local nada é aberto (e isso não é falha da estação)
    relay = hub.relay
    await relay.ensure_started()
    if not still_wanted():  # a estação mudou enquanto o diretório respondia: nada desta tentativa vale
        raise RuntimeError("estação trocada durante a partida")
    token = relay.register(station.url_resolved, codec=station.codec, bitrate=station.bitrate, on_title=on_title)
    keep_token(token)
    try:
        # SÓ o nó local e a URL exata (R-8): o get_tracks genérico trocaria a consulta por "ytsearch:<url>" e, sem resultado, a mandaria a
        # Deezer, Spotify e aos outros nós, vazando o endereço e o token e tocando outra coisa no lugar do fluxo
        found = await local_node(bot).get_tracks(
            relay.local_url(token),
            track_cls=LavalinkTrack,
            playlist_cls=LavalinkPlaylist,
            requester=bot.user.id,
            trusted_local=True,  # só aqui: o filtro de IP do nó recusaria o 127.0.0.1 do relay (que é nosso, com token)
        )
        tracks = list(getattr(found, "tracks", found) or [])
        if not tracks:
            raise RuntimeError("o fluxo não abriu")
        track = tracks[0]
        # o endereço local (com o token) só existe para o Lavalink: nenhuma tela, comando, cache, histórico ou RPC o recebe [m1]
        track.info["uri"] = ""
        track.info["identifier"] = ""
        with suppress(AttributeError):
            track.identifier = ""
        track.info["title"] = track.title = station_title(station)
        track.author = author  # o que está no ar: o título do fluxo quando ele chega, senão "Ao vivo"
        if on_track is not None:
            on_track(track)
        player.queue.clear()
        player.queue.append(track)
        player.volume = volume  # o volume vale para cada fluxo novo (o player começa a faixa com ele)
        relay.expect_flow(token)  # o que o loadtracks leu não conta: só o áudio do play
        await player.process_next()
        if not await relay.wait_flowing(token, flow_wait):
            # só "toca" quando o áudio chegou: fluxo mudo é falha, não estado. Se o relay chegou a abrir a estação, a culpa é dela
            raise StationFailure if relay.reached(token) else RuntimeError("o fluxo não chegou ao Lavalink")
        return track
    except BaseException as error:
        station_fault = (
            relay.reached(token) and not relay.limited(token) and not isinstance(error, asyncio.CancelledError)
        )
        with suppress(Exception):
            await (
                player.stop()
            )  # nada de faixa velha esperando o Lavalink: parada de verdade, quem chama decide a próxima tentativa
        await release()
        if station_fault and not isinstance(error, StationFailure):
            raise StationFailure from error  # o Lavalink chegou ao relay e o fluxo não abriu: a estação falhou
        raise
