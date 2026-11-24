"""Dublês e fixtures realistas das sessões, filas e skins em JSON, sem Discord, rede nem ``.env``.

* Faixas montadas pelas classes REAIS (``LavalinkTrack``, ``PartialTrack``, ``LavalinkPlaylist``), com os mesmos campos
  que os provedores (Deezer, Spotify, YouTube) preenchem em ``extra`` e em ``pluginInfo``.
* Letras no formato do plugin de letras do Lavalink (linhas sincronizadas e texto), com texto neutro.
* Um jogador mínimo para o ``PlayerSession.save_info`` REAL e um Mongo em memória que copia os documentos (como o BSON).
"""

import copy
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace

from empiremuse.cogs.player_resume import PlayerSession
from empiremuse.utils.client import BotPool
from empiremuse.utils.music.models import LavalinkPlaylist, LavalinkTrack, PartialTrack

SERVIDOR = 1100000000000000001
BOT_ID = 1100000000000000002
CANAL_DE_VOZ = 1100000000000000003
CANAL_DE_TEXTO = 1100000000000000004
MENSAGEM = 1100000000000000005
DONO = 1100000000000000006
DJ_A, DJ_B = 1100000000000000007, 1100000000000000008
INSTANTE = datetime(2026, 10, 19, 14, 3, 22, 123456, tzinfo=timezone.utc)


# ============================================================================ letras e pluginInfo realistas


def letras_sincronizadas(linhas: int = 40) -> dict:
    """Resposta do plugin de letras com linhas sincronizadas (``lines``) e a capa em vários tamanhos."""
    return {
        "type": "timed",
        "source": "YouTube Music",
        "track": {
            "title": "Música de Teste",
            "author": "Artista de Teste",
            "album": "Álbum de Teste",
            "albumArt": [
                {"url": f"https://exemplo.invalid/capa{tamanho}.jpg", "height": tamanho, "width": tamanho}
                for tamanho in (60, 120, 544)
            ],
        },
        "lines": [
            {"line": f"Linha {n} da letra", "range": {"start": n * 3000, "end": n * 3000 + 2900}} for n in range(linhas)
        ],
    }


def plugin_info_de_faixa() -> dict:
    """``pluginInfo`` de faixa do LavaSrc (Spotify, Deezer, Apple Music): dicionário plano."""
    return {
        "albumName": "Álbum de Teste",
        "albumUrl": "https://exemplo.invalid/album/1",
        "artistUrl": "https://exemplo.invalid/artista/1",
        "artistArtworkUrl": "https://exemplo.invalid/artista/1.jpg",
        "previewUrl": "https://exemplo.invalid/previa/1.mp3",
        "isPreview": False,
    }


# ============================================================================ faixas pelas classes reais


def faixa_youtube(n: int = 0, *, requester: int = DONO, letras: dict | None = None, **extras) -> LavalinkTrack:
    info = {
        "identifier": f"yt{n:09d}",
        "isSeekable": True,
        "author": f"Canal {n}",
        "length": 180000 + n,
        "isStream": False,
        "position": 0,
        "title": f"Música {n}",
        "uri": f"https://www.youtube.com/watch?v=yt{n:09d}",
        "sourceName": "youtube",
        "artworkUrl": None,
        "isrc": None,
    }
    faixa = LavalinkTrack(id_=f"QAAAcodificado{n}", info=info, requester=requester, pluginInfo={})
    if letras is not None:
        faixa.info["extra"]["lyrics"] = letras
    faixa.info["extra"].update(extras)
    return faixa


def faixa_deezer(n: int = 0, *, requester: int = DONO) -> LavalinkTrack:
    info = {
        "title": f"Faixa Deezer {n}",
        "author": "Artista",
        "length": 200000,
        "identifier": str(1000 + n),
        "isStream": False,
        "uri": f"https://www.deezer.com/track/{1000 + n}",
        "sourceName": "deezer",
        "position": 0,
        "artworkUrl": "https://exemplo.invalid/capa.jpg",
        "isrc": "BRABC2600001",
    }
    faixa = LavalinkTrack(id_=f"QAAAdeezer{n}", info=info, requester=requester, pluginInfo=plugin_info_de_faixa())
    faixa.info["extra"].update(
        {
            "authors": ["Artista", "Convidada"],
            "authors_md": "[`Artista`](https://www.deezer.com/artist/1), [`Convidada`](https://www.deezer.com/artist/2)",
            "artist_id": 1,
            "album": {"name": "Álbum de Teste", "url": "https://exemplo.invalid/album/1"},
        }
    )
    return faixa


def faixa_parcial(n: int = 0, *, requester: int = DONO) -> PartialTrack:
    return PartialTrack(
        uri=f"https://open.spotify.com/track/parcial{n}",
        title=f"Faixa parcial {n}",
        author="Artista",
        duration=150000,
        requester=requester,
        source_name="spotify",
    )


def faixa_com_playlist(n: int = 0) -> LavalinkTrack:
    """Faixa de uma playlist: o ``save_info`` grava ``playlist = {name, url}``."""
    playlist = LavalinkPlaylist(
        {"loadType": "PLAYLIST_LOADED", "playlistInfo": {"name": "Minha Playlist", "selectedTrack": -1}, "tracks": []},
        url="https://exemplo.invalid/playlist/1",
    )
    faixa = faixa_youtube(n)
    faixa.playlist = playlist
    return faixa


# ============================================================================ jogador, cog e bancos de mentira


def jogador_falso(*, atual=None, fila=(), tocadas=(), autoplay=(), falhas=(), **campos) -> SimpleNamespace:
    """Só o que ``PlayerSession.save_info`` lê do ``LavalinkPlayer``; ``campos`` sobrescreve qualquer atributo."""
    voz = SimpleNamespace(id=CANAL_DE_VOZ)
    servidor = SimpleNamespace(id=SERVIDOR, me=SimpleNamespace(voice=SimpleNamespace(channel=voz)))
    jogador = SimpleNamespace(
        guild=servidor,
        is_closing=False,
        message=SimpleNamespace(id=MENSAGEM),
        text_channel=SimpleNamespace(id=CANAL_DE_TEXTO),
        current=atual,
        queue=deque(fila),
        played=deque(tocadas),
        queue_autoplay=deque(autoplay),
        failed_tracks=list(falhas),
        volume=80,
        nightcore=True,
        position=45123.5,
        dj={DJ_A, DJ_B},
        player_creator=DONO,
        static=False,
        paused=True,
        auto_pause=False,
        keep_connected=True,
        loop="queue",
        autoplay=True,
        stage_title_event=False,
        stage_title_template="{track.title}",
        skin="default",
        skin_static="default",
        uptime=1234567,
        restrict_mode=False,
        mini_queue_enabled=True,
        listen_along_invite="https://exemplo.invalid/convite",
        prefix_info="!!",
        _voice_state={
            "sessionId": "sessao-de-voz",
            "event": {"token": "token-de-voz", "endpoint": "voz.exemplo.invalid"},
        },
        lastfm_artists=["Artista"],
        start_timestamp=1760882602.5,
        _last_channel_id=CANAL_DE_VOZ,
        extra_info={},
        live_lyrics_enabled=False,
        current_encoded="QAAAcodificado0",
        command_log_list=deque(
            [{"text": f"Comando {n}", "emoji": "▶️", "timestamp": 1760882600.0 + n} for n in range(3)], maxlen=10
        ),
        custom_skin_data={},
        custom_skin_static_data={},
    )
    for nome, valor in campos.items():
        setattr(jogador, nome, valor)
    jogador.bot = SimpleNamespace(music=SimpleNamespace(players={SERVIDOR: jogador}))
    return jogador


class MongoEmMemoria:
    """O que ``PlayerSession`` pede ao ``MongoDatabase``; copia os documentos como o BSON faria."""

    def __init__(self):
        self.documentos: dict[tuple[str, str], dict[str, dict]] = {}
        self.apagados: list[tuple[str, str]] = []

    def colecao(self, db_name, collection) -> dict:
        return self.documentos.setdefault((str(db_name), collection), {})

    async def update_data(self, id_, data, *, db_name, collection, default_model=None):
        documento = copy.deepcopy(data)
        documento["_id"] = str(id_)
        self.colecao(db_name, collection)[str(id_)] = documento

    async def query_data(self, db_name, collection, filter=None, limit=100):
        return [copy.deepcopy(d) for d in self.colecao(db_name, collection).values()]

    async def iter_data(self, db_name, collection, filter=None):
        """Cursor (gerador assíncrono) sobre uma cópia de cada documento, com filtro de igualdade de campos."""
        for chave in list(self.colecao(db_name, collection)):
            documento = self.colecao(db_name, collection).get(chave)
            if documento is not None and all(documento.get(k) == v for k, v in (filter or {}).items()):
                yield copy.deepcopy(documento)

    async def delete_data(self, id_, db_name, collection):
        self.apagados.append((str(id_), collection))
        self.colecao(db_name, collection).pop(str(id_), None)


def cog_de_sessoes(*, mongo: MongoEmMemoria | None = None, **config) -> PlayerSession:
    """``PlayerSession`` real, sem ``__init__`` (que dispara a tarefa de retomada) e com o bot mínimo."""
    configuracao = {
        "PLAYER_SESSIONS_MONGODB": mongo is not None,
        "MONGO": "mongodb://configurado" if mongo is not None else "",
        "PLAYER_INFO_BACKUP_INTERVAL": 45,
        "PLAYER_INFO_BACKUP_INTERVAL_MONGO": 300,
        "EXTRA_HINTS": "",
        **config,
    }
    cog = PlayerSession.__new__(PlayerSession)
    cog.bot = SimpleNamespace(
        user=SimpleNamespace(id=BOT_ID),
        config=configuracao,
        pool=SimpleNamespace(
            mongo_database=mongo,
            default_skin="muse_text",  # a retomada recua para o padrão quando a skin gravada é o "default" antigo (Q1)
            default_static_skin="muse_text",
            process_track_cls=lambda *args, **kwargs: BotPool.process_track_cls(None, *args, **kwargs),
        ),
    )
    return cog


# ============================================================================ profundidade de um valor JSON
