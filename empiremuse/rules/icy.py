"""Leitor ICY, puro e sobre bytes (item 8, R3, R-9): separa o áudio dos metadados ``StreamTitle`` e limita tudo que vem da estação.

A estação é controlada por terceiros (corpo, cabeçalhos, ``StreamTitle``): aqui nada confia nela.

- ``icy-metaint`` de 1 a 65536; fora disso (ou ausente) não há parse e o fluxo passa sem tocar nos bytes.
- O bloco de metadados tem até 4080 bytes (255 × 16); a busca do título é por ``find``, sem regex, então não há backtracking.
- Decodifica em UTF-8 e, se falhar, em latin-1; tira o NUL; depois a função única do texto externo (R-11) corta e neutraliza.
- Os metadados são REMOVIDOS do fluxo: o que sai para o Lavalink é só áudio.
- O título sai no máximo a cada 15 s e nunca repetido (``TitleThrottle``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from empiremuse.rules.text import external_text

MIN_METAINT = 256  # abaixo disto o laço do relay rodaria por byte (CPU no laço de eventos): a estação é recusada
MAX_METAINT = 65536
BLOCK_UNIT = 16
MAX_BLOCK = 255 * BLOCK_UNIT  # 4080 bytes
TITLE_LIMIT = 120
TITLE_INTERVAL_S = 15.0
_TAG = b"StreamTitle='"


def check_metaint(raw: object) -> int | None:
    """O valor do cabeçalho ``icy-metaint`` como inteiro de 256 a 65536, ou ``None`` (ausente, 0, negativo, enorme ou lixo)."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.isascii() or not text.isdecimal() or len(text) > 6:
        return None
    value = int(text)
    return value if MIN_METAINT <= value <= MAX_METAINT else None


def metaint_too_small(raw: object) -> bool:
    """O cabeçalho traz um ``icy-metaint`` de 1 a 255? (0 é "sem metadados" e vale; lixo cai em ``check_metaint`` e vira "sem parse".)"""
    text = raw.decode("ascii", "ignore") if isinstance(raw, bytes) else raw
    if not isinstance(text, str):
        return False
    text = text.strip()
    return text.isascii() and text.isdecimal() and len(text) <= 6 and 0 < int(text) < MIN_METAINT


def decode_block(block: bytes) -> str:
    """Os bytes do bloco como texto: UTF-8 e, se não for, latin-1; sem NUL (o preenchimento do bloco)."""
    block = block.replace(b"\x00", b"")
    try:
        return block.decode("utf-8")
    except UnicodeDecodeError:
        return block.decode("latin-1")


def parse_title(block: bytes) -> str | None:
    """O ``StreamTitle`` de um bloco de metadados (texto cru, ainda sem a R-11), ou ``None`` se não há título."""
    if len(block) > MAX_BLOCK:
        block = block[:MAX_BLOCK]
    start = block.find(_TAG)
    if start < 0:
        return None
    start += len(_TAG)
    end = block.find(b"';", start)
    raw = block[start:] if end < 0 else block[start:end]
    text = decode_block(raw).strip().rstrip("';").strip()
    return text or None


def clean_title(raw: str | None, limit: int = TITLE_LIMIT) -> str | None:
    """O título como pode ser PUBLICADO (R-11: sem menção, sem link, sem controle, no limite); ``None`` se não sobrar nada."""
    if raw is None:
        return None
    text = external_text(raw, limit)
    return text or None


@dataclass
class IcyStripper:
    """Separa áudio e metadados de um fluxo ICY, em pedaços de qualquer tamanho.

    ``feed(chunk)`` devolve ``(áudio, títulos)``: o áudio sem os blocos de metadados e os títulos novos achados neste pedaço (texto cru).
    Sem ``metaint`` válido o fluxo passa inteiro.
    """

    metaint: int | None
    _until_meta: int = field(init=False)
    _meta_left: int = field(init=False, default=0)
    _meta_buffer: bytearray = field(init=False, default_factory=bytearray)
    _reading_meta: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._until_meta = self.metaint or 0

    def feed(self, chunk: bytes) -> tuple[bytes, list[str]]:
        if not self.metaint:
            return chunk, []
        audio = bytearray()
        titles: list[str] = []
        view = memoryview(chunk)
        position = 0
        while position < len(view):
            if self._reading_meta:
                if self._meta_left == -1:  # ainda falta o byte de tamanho
                    self._meta_left = view[position] * BLOCK_UNIT
                    position += 1
                    if self._meta_left == 0:
                        self._finish_block(titles)
                    continue
                take = min(self._meta_left, len(view) - position)
                self._meta_buffer += view[position : position + take]
                self._meta_left -= take
                position += take
                if self._meta_left == 0:
                    self._finish_block(titles)
                continue
            take = min(self._until_meta, len(view) - position)
            audio += view[position : position + take]
            self._until_meta -= take
            position += take
            if self._until_meta == 0:
                self._reading_meta = True
                self._meta_left = -1
        return bytes(audio), titles

    def _finish_block(self, titles: list[str]) -> None:
        if self._meta_buffer and (title := parse_title(bytes(self._meta_buffer))):
            titles.append(title)
        self._meta_buffer.clear()
        self._reading_meta = False
        self._meta_left = 0
        self._until_meta = self.metaint or 0


@dataclass
class TitleThrottle:
    """No máximo um título a cada ``interval`` s e nunca o mesmo duas vezes seguidas (o último fica guardado até a janela abrir)."""

    interval: float = TITLE_INTERVAL_S
    _last_published: str | None = field(default=None, init=False)
    _last_at: float | None = field(default=None, init=False)
    _pending: str | None = field(default=None, init=False)

    def offer(self, title: str | None, now: float) -> str | None:
        """Chega um título novo. Devolve o que pode ser publicado AGORA, ou ``None``."""
        if title is not None and title != self._last_published:
            self._pending = title
        return self.poll(now)

    def poll(self, now: float) -> str | None:
        pending = self._pending
        if pending is None or pending == self._last_published:
            self._pending = None
            return None
        if self._last_at is not None and now - self._last_at < self.interval:
            return None
        self._pending, self._last_published, self._last_at = None, pending, now
        return pending
