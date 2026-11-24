"""O player como as skins Clássicas (e as personalizadas) o veem numa call privada (spec skins 2.3, D10).

Numa call privada nenhuma skin mostra a faixa, a capa nem quem pediu. As skins do EmpireMuse já fazem isso sozinhas. As
Clássicas recebem uma visão do player em que a faixa atual e a fila são máscaras:

- **Título:** o texto de faixa oculta (``jbx.hidden_track``).
- **Sem link nem letra:** sem endereço, sem ``ytid`` e sem a recomendação relacionada.
- **Capa:** o avatar do bot no lugar da capa.
- **Artista, álbum e playlist:** fora.
- **Quem pediu:** o próprio bot, como reserva; as Clássicas nem desenham a linha nem o select da fila (``details_shown``).
- **Última ação:** vazia.

O resto passa direto ao player de verdade (duração, volume, botões, contagem da fila), então o visual da skin não muda (D3).
Os campos da máscara são uma lista fechada: um atributo novo lido por uma skin falha no teste em vez de vazar.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
from typing import Any

from empiremuse.rules.skins import JUKEBOX_IDS
from empiremuse.utils.jukebox_panel import player_call_is_private

NO_ARTIST = "—"


def _bot_avatar(player: Any) -> str:
    return player.bot.user.display_avatar.replace(size=256, static_format="png").url


class HiddenTrack:
    """Uma faixa sem nada que a identifique: título oculto, sem link, avatar do bot, o bot no lugar de quem pediu."""

    def __init__(self, track: Any, title: str, thumb: str, bot_id: int) -> None:
        info = getattr(track, "info", None) or {}
        self.title = self.single_title = title
        self.uri = self.search_uri = ""
        self.thumb = thumb
        self.author = self.authors_string = self.authors_md = NO_ARTIST
        self.requester = bot_id
        self.playlist_name = self.playlist_url = self.album_name = self.album_url = None
        self.ytid = self.lyrics = self.stream_title = None
        self.info = {"sourceName": info.get("sourceName", ""), "extra": {}}  # a origem (ícone) fica; o resto sai
        self.duration = track.duration
        self.is_stream = track.is_stream
        self.track_loops = getattr(track, "track_loops", 0)
        self.autoplay = getattr(track, "autoplay", False)
        self.emoji = getattr(track, "emoji", "")
        self.unique_id = getattr(track, "unique_id", None)


class HiddenTracks:
    """A fila com o mesmo tamanho, cada faixa mascarada só quando a skin a lê."""

    def __init__(self, tracks: Any, mask: Any) -> None:
        self._tracks = tracks
        self._mask = mask

    def __len__(self) -> int:
        return len(self._tracks)

    def __bool__(self) -> bool:
        return len(self._tracks) > 0

    def __iter__(self) -> Iterator[HiddenTrack]:
        return (self._mask(track) for track in self._tracks)

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._mask(track) for track in islice(self._tracks, *index.indices(len(self._tracks)))]
        return self._mask(self._tracks[index])


class PrivateCallView:
    """O player numa call privada: faixa, fila e última ação mascaradas; todo o resto é o player de verdade."""

    def __init__(self, player: Any) -> None:
        title, thumb, bot_id = player.tr("jbx.hidden_track"), _bot_avatar(player), player.bot.user.id

        def mask(track: Any) -> HiddenTrack:
            return HiddenTrack(track, title, thumb, bot_id)

        current = player.current
        hidden = {
            "_player": player,
            "current": mask(current) if current is not None else None,
            "queue": HiddenTracks(player.queue, mask),
            "queue_autoplay": HiddenTracks(player.queue_autoplay, mask),
            "command_log": "",
        }
        for name, value in hidden.items():
            object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._player, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._player, name, value)  # setup das skins (``mini_queue_feature`` e afins) vale no player

    def __str__(self) -> str:
        return str(self._player)


def skin_view(player: Any) -> Any:
    """O que a skin atual recebe: o próprio player, ou a visão mascarada numa call privada (Clássicas e personalizadas)."""
    name = player.skin_static if player.static else player.skin
    if name in JUKEBOX_IDS or not player_call_is_private(player):
        return player
    return PrivateCallView(player)


def details_shown(player: Any) -> bool:
    """A Clássica desenha quem pediu e o select da fila? Não numa call privada (como as skins do EmpireMuse)."""
    return not isinstance(player, PrivateCallView)


def closing_log(player: Any) -> str:
    """A última ação para os embeds de encerramento e de ocioso; vazia numa call privada."""
    return "" if player_call_is_private(player) else (player.command_log or "")
