"""As fontes das skins de imagem: arquivos conferidos por SHA-256, fontes variáveis por eixo e reserva por caractere.

- **Arquivos:** só os de ``assets/fonts/`` listados em ``MANIFEST`` (origem, commit e hash no ``assets/fonts/README.md``).
  Cada arquivo é conferido UMA vez, na primeira carga; hash diferente ou arquivo fora da lista = ``FontIntegrityError``
  (nunca uma fonte não conferida). Nada é baixado em runtime.
- **Pesos:** uma ``FreeTypeFont`` por ``(arquivo, peso, tamanho)``, com a variação aplicada uma vez, por NOME de eixo
  (``Weight``; os outros eixos ficam no padrão da fonte), e nunca mudada depois. O peso é limitado ao intervalo do eixo.
- **Reserva por caractere:** a fonte da skin, depois a Noto Sans (latim estendido, grego, cirílico). Caractere sem glifo
  nas duas sai do texto (``lossy``: o título real vai também no autor do embed). No máximo ``RUNS_MAX`` trechos por
  texto; o que passa vira ``…``.
- **Sem ``raqm`` presumido:** o motor de layout vem de ``features.check("raqm")``, conferido uma vez. Sem ele, texto da
  direita para a esquerda não é desenhado (``in_message``: a imagem escreve ``jbx.title_in_text``). Se nada sobra para
  desenhar, idem.
- **Uso:** dentro da vaga de render (``slot.py``), uma thread por vez; o cache não é protegido para threads concorrentes.
"""

from __future__ import annotations

import hashlib
import struct
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple, Optional

from PIL import ImageDraw, ImageFont, features

FONTS_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"

UNBOUNDED = "Unbounded[wght].ttf"
FIGTREE = "Figtree[wght].ttf"
NOTO = "NotoSans[wdth,wght].ttf"
FALLBACK = NOTO

# SHA-256 dos arquivos de ``google/fonts`` no commit registrado em ``assets/fonts/README.md``.
MANIFEST: dict[str, str] = {
    UNBOUNDED: "323b511be380c8d474ef030686b71aedde501f8d9cd46da558b7c40454372c3f",
    FIGTREE: "26ad3db9b31ff7dde67a91ff515d022d2f495cd506590699cf264f0bfe6fb714",
    NOTO: "bfb7bb691513f12e734dc346c03a03f784912432d7e3fa8e56efcf906fe86b3d",
}

RUNS_MAX = 32  # trechos (troca de fonte) por texto (SK-7)
ELLIPSIS = "…"
WEIGHT_AXIS = b"Weight"
_RTL = frozenset({"R", "AL", "RLE", "RLO", "RLI"})
_NEUTRAL = frozenset({"Z", "P", "M"})  # categorias Unicode que não puxam troca de fonte


class FontIntegrityError(RuntimeError):
    """Arquivo de fonte fora do ``MANIFEST`` ou com hash diferente: a fonte não é usada."""


class Run(NamedTuple):
    text: str
    font: ImageFont.FreeTypeFont


class TextRuns(NamedTuple):
    runs: tuple[Run, ...]
    lossy: bool  # algum caractere saiu (sem glifo ou além do teto): o título real vai também no embed
    in_message: bool  # nada a desenhar ou RTL sem ``raqm``: a imagem escreve ``jbx.title_in_text``


# ---------------------------------------------------------------------- cmap (só de arquivos já conferidos)


def _tables(data: bytes) -> dict[bytes, tuple[int, int]]:
    count = struct.unpack_from(">H", data, 4)[0]
    out = {}
    for i in range(count):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        out[tag] = (offset, length)
    return out


def _format4(data: bytes, at: int) -> set[int]:
    segments = struct.unpack_from(">H", data, at + 6)[0] // 2
    ends_at = at + 14
    starts_at = ends_at + 2 * segments + 2
    deltas_at = starts_at + 2 * segments
    ranges_at = deltas_at + 2 * segments
    points: set[int] = set()
    for s in range(segments):
        end = struct.unpack_from(">H", data, ends_at + 2 * s)[0]
        start = struct.unpack_from(">H", data, starts_at + 2 * s)[0]
        delta = struct.unpack_from(">h", data, deltas_at + 2 * s)[0]
        range_offset = struct.unpack_from(">H", data, ranges_at + 2 * s)[0]
        for code in range(start, min(end, 0xFFFE) + 1):
            if range_offset == 0:
                glyph = (code + delta) & 0xFFFF
            else:
                glyph = struct.unpack_from(">H", data, ranges_at + 2 * s + range_offset + 2 * (code - start))[0]
                glyph = (glyph + delta) & 0xFFFF if glyph else 0
            if glyph:
                points.add(code)
    return points


def _format12(data: bytes, at: int) -> set[int]:
    groups = struct.unpack_from(">I", data, at + 12)[0]
    points: set[int] = set()
    for g in range(groups):
        start, end, glyph = struct.unpack_from(">III", data, at + 16 + 12 * g)
        points.update(range(start if glyph else start + 1, end + 1))
    return points


def cmap_points(data: bytes) -> frozenset[int]:
    """Os códigos Unicode com glifo (diferente do ``.notdef``) no ``cmap`` da fonte: formato 12, senão formato 4."""
    offset, _ = _tables(data)[b"cmap"]
    count = struct.unpack_from(">H", data, offset + 2)[0]
    subtables: dict[int, int] = {}
    for i in range(count):
        platform, encoding, sub = struct.unpack_from(">HHI", data, offset + 4 + 8 * i)
        if (platform, encoding) in ((3, 10), (0, 4), (0, 6), (3, 1), (0, 3)):
            fmt = struct.unpack_from(">H", data, offset + sub)[0]
            subtables.setdefault(fmt, offset + sub)
    if 12 in subtables:
        return frozenset(_format12(data, subtables[12]))
    if 4 in subtables:
        return frozenset(_format4(data, subtables[4]))
    raise FontIntegrityError("cmap")


# ---------------------------------------------------------------------- o livro de fontes


class FontBook:
    def __init__(
        self,
        directory: Path = FONTS_DIR,
        manifest: Optional[dict[str, str]] = None,
        *,
        raqm: Optional[bool] = None,
    ) -> None:
        self._dir = directory
        self._manifest = dict(MANIFEST if manifest is None else manifest)
        self.raqm = features.check("raqm") if raqm is None else raqm  # uma vez, nunca presumido
        self.layout = ImageFont.Layout.RAQM if self.raqm else ImageFont.Layout.BASIC
        self._points: dict[str, frozenset[int]] = {}
        self._fonts: dict[tuple[str, int, int], ImageFont.FreeTypeFont] = {}

    def _verified(self, name: str) -> frozenset[int]:
        if (points := self._points.get(name)) is not None:
            return points
        expected = self._manifest.get(name)
        if expected is None:
            raise FontIntegrityError(f"fonte fora da lista: {name}")
        data = (self._dir / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise FontIntegrityError(f"hash diferente do registrado: {name}")
        points = self._points[name] = cmap_points(data)
        return points

    def font(self, name: str, weight: int, size: int) -> ImageFont.FreeTypeFont:
        """A fonte ``name`` no ``weight`` (limitado ao eixo) e ``size`` px, em cache; o arquivo é conferido antes."""
        key = (name, int(weight), int(size))
        if (cached := self._fonts.get(key)) is not None:
            return cached
        self._verified(name)
        font = ImageFont.truetype(str(self._dir / name), key[2], layout_engine=self.layout)
        axes = font.get_variation_axes()
        vector = []
        for axis in axes:
            if axis["name"] == WEIGHT_AXIS:
                vector.append(min(max(key[1], int(axis["minimum"])), int(axis["maximum"])))
            else:
                vector.append(int(axis["default"]))
        if axes:
            font.set_variation_by_axes(vector)  # pela ordem dos eixos DESTA fonte, montada pelo nome
        self._fonts[key] = font
        return font

    def has_glyph(self, name: str, char: str) -> bool:
        return ord(char) in self._verified(name)

    def runs(self, text: str, name: str, weight: int, size: int) -> TextRuns:
        """O texto em trechos de uma fonte só: a da skin, senão a Noto Sans; sem glifo nas duas, o caractere sai."""
        if not self.raqm and any(unicodedata.bidirectional(c) in _RTL for c in text):
            return TextRuns((), lossy=True, in_message=True)
        chain = (name,) if name == FALLBACK else (name, FALLBACK)
        pieces: list[tuple[str, list[str]]] = []
        lossy = False
        for char in text:
            current = pieces[-1][0] if pieces else None
            if current is not None and unicodedata.category(char)[0] in _NEUTRAL and self.has_glyph(current, char):
                owner: Optional[str] = current  # espaço, pontuação e marca combinante ficam no trecho atual
            else:
                owner = next((n for n in chain if self.has_glyph(n, char)), None)
            if owner is None:
                lossy = True
                continue
            if pieces and pieces[-1][0] == owner:
                pieces[-1][1].append(char)
                continue
            if len(pieces) == RUNS_MAX:
                lossy = True
                _close_with_ellipsis(pieces, self, chain)
                break
            pieces.append((owner, [char]))
        runs = tuple(Run("".join(chars), self.font(owner, weight, size)) for owner, chars in pieces)
        drawable = any(run.text.strip() for run in runs)
        return TextRuns(runs, lossy=lossy, in_message=not drawable and bool(text.strip()))


def _close_with_ellipsis(pieces: list[tuple[str, list[str]]], book: FontBook, chain: Iterable[str]) -> None:
    """Fecha no teto: o ``…`` entra no último trecho se a fonte dele o tem; senão troca o último trecho pelo ``…``."""
    owner = pieces[-1][0]
    if book.has_glyph(owner, ELLIPSIS):
        pieces[-1][1].append(ELLIPSIS)
        return
    fallback = next((n for n in chain if book.has_glyph(n, ELLIPSIS)), None)
    if fallback is not None:
        pieces[-1] = (fallback, [ELLIPSIS])


def text_length(runs: TextRuns) -> float:
    return sum(run.font.getlength(run.text) for run in runs.runs)


def draw_runs(draw: ImageDraw.ImageDraw, xy: tuple[float, float], runs: TextRuns, fill: object) -> None:
    """Desenha os trechos lado a lado na mesma linha de base (``xy`` = início da linha de base, âncora ``ls``)."""
    x, y = xy
    for run in runs.runs:
        draw.text((x, y), run.text, font=run.font, fill=fill, anchor="ls")  # type: ignore[arg-type]
        x += run.font.getlength(run.text)
