"""Apoio dos testes de emojis de aplicação (#130): os 64 nomes, imagens sintéticas e um cliente falso da API.

Imagens: só os bytes que o validador lê (assinatura e cabeçalho); sem Pillow. O cliente falso grava cada chamada e mede a concorrência
(``create`` nunca em paralelo). Nenhum teste dorme: o ``sleep`` é injetado.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import disnake

NOMES = (
    "music_queue add_music play play_source playpause stop skip backward arrow_left arrow_right "
    "leftwards_arrow_with_hook help git stagechannel bandcamp youtube youtube_music spotify soundcloud "
    "deezer applemusic twitch jiosaavn tidal lastfm "
    "loved search config gear replay tools pinned bot voice_event community_server plus headset "
    "history microphone user playlists speaker notification fast_backward volume shuffle playlist_add repeat_all "
    "sound_spectrum refresh lock drag like music_notes music_note playlist_play cancel star lyrics comment disc record repeat pause"
).split()
# §12 do contrato: os 12 extras do dono e a reserva Unicode de cada um; o ``headset`` é o único ANIMADO (GIF89a)
EXTRAS = {
    "loved": "❤️",
    "search": "🔍",
    "config": "📝",
    "gear": "⚙️",
    "replay": "🔁",
    "tools": "🧰",
    "pinned": "📌",
    "bot": "🤖",
    "voice_event": "🔊",
    "community_server": "🏘️",
    "plus": "➕",
    "headset": "🎧",
}
ANIMADOS = ("headset",)
MAXIMO_BYTES = 256 * 1024
LADO = 128


def _bloco(tipo: bytes, corpo: bytes) -> bytes:
    return struct.pack(">I", len(corpo)) + tipo + corpo + struct.pack(">I", zlib.crc32(tipo + corpo))


def png(largura: int = LADO, altura: int = LADO, *, extra: int = 0) -> bytes:
    """PNG com cabeçalho IHDR real, IDAT mínimo e IEND; ``extra`` enche com bytes num chunk auxiliar (para passar do teto de tamanho)."""
    cabecalho = struct.pack(">IIBBBBB", largura, altura, 8, 6, 0, 0, 0)
    dados = zlib.compress(bytes(altura * (1 + largura * 4)))
    auxiliar = _bloco(b"tEXt", b"k\x00" + b"x" * extra) if extra else b""
    return b"\x89PNG\r\n\x1a\n" + _bloco(b"IHDR", cabecalho) + auxiliar + _bloco(b"IDAT", dados) + _bloco(b"IEND", b"")


def escrever_emojis(pasta: Path, nomes=NOMES, *, conteudo=None) -> dict[str, bytes]:
    """Cria ``<nome>.png`` válidos (ou ``conteudo`` dado) em ``pasta``; devolve ``{nome: bytes}``."""
    pasta.mkdir(parents=True, exist_ok=True)
    escritos = {}
    for nome in nomes:
        dados = conteudo if conteudo is not None else png()
        (pasta / f"{nome}.png").write_bytes(dados)
        escritos[nome] = dados
    return escritos


def escrever_somas(pasta: Path, *, errado: tuple[str, ...] = ()) -> Path:
    """``SHA256SUMS`` ao lado da pasta de emojis (em ``assets/``), com o prefixo ``emojis/``; ``errado`` lista arquivos com hash trocado."""
    linhas = []
    for arquivo in sorted(pasta.iterdir()):
        if arquivo.is_file() and not arquivo.is_symlink():
            resumo = hashlib.sha256(b"outro" if arquivo.name in errado else arquivo.read_bytes()).hexdigest()
            linhas.append(f"{resumo}  emojis/{arquivo.name}")
    destino = pasta.parent / "SHA256SUMS"
    destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return destino


def resposta(status: int, headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, reason="x", headers=headers or {})


def erro_http(
    status: int, *, codigo: int = 0, retry_after: float | None = None, corpo: str = "", global_: bool = False
):
    """``disnake.HTTPException`` como a API devolve; ``retry_after`` vai no atributo E no corpo, ``global_`` no cabeçalho E no corpo."""
    dados = {"message": corpo or "erro", "code": codigo}
    if retry_after is not None:
        dados["retry_after"] = retry_after
    if global_:
        dados["global"] = True
    cabecalhos = {"X-RateLimit-Global": "true"} if global_ else {}
    erro = disnake.HTTPException(resposta(status, cabecalhos), dados)
    if retry_after is not None:
        erro.retry_after = retry_after
    return erro


def emoji_da_app(nome: str, id_: object, animado: bool = False) -> SimpleNamespace:
    return SimpleNamespace(name=nome, id=id_, animated=animado)


class ClienteFalso:
    """Faz o papel do ``bot`` no sync: ``fetch_application_emojis`` e ``create_application_emoji``; qualquer remoção/edição derruba o teste."""

    def __init__(
        self, existentes=(), falhas: dict[str, list[Exception]] | None = None, fetch_falha: Exception | None = None
    ):
        self.existentes = list(existentes)
        self.falhas = {nome: list(lista) for nome, lista in (falhas or {}).items()}
        self.fetch_falha = fetch_falha
        self.fetches = 0
        self.criacoes: list[tuple[str, bytes]] = []
        self.tentativas: dict[str, int] = {}
        self.em_voo = 0
        self.pico_em_voo = 0
        self.proximo_id = 100000000000000001
        self.app_emojis: dict[str, str] = {}
        self.user = SimpleNamespace(id=7)

    async def fetch_application_emojis(self):
        self.fetches += 1
        if self.fetch_falha is not None:
            raise self.fetch_falha
        return list(self.existentes)

    async def create_application_emoji(self, *, name, image):
        self.em_voo += 1
        self.pico_em_voo = max(self.pico_em_voo, self.em_voo)
        try:
            await asyncio.sleep(0)
            self.tentativas[name] = self.tentativas.get(name, 0) + 1
            if self.falhas.get(name):
                raise self.falhas[name].pop(0)
            dados = bytes(image) if not hasattr(image, "read") else image.read()
            self.criacoes.append((name, dados))
            criado = emoji_da_app(name, self.proximo_id, dados.startswith(b"GIF8"))
            self.proximo_id += 1
            self.existentes.append(criado)
            return criado
        finally:
            self.em_voo -= 1

    def __getattr__(self, nome: str):
        if nome.startswith(("delete_", "edit_", "remove_")):
            raise AssertionError(f"o sync não pode chamar {nome}")
        raise AttributeError(nome)


class SonoFalso:
    def __init__(self) -> None:
        self.duracoes: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.duracoes.append(segundos)
