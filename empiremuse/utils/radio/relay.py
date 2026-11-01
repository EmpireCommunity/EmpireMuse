"""O relay local das rádios ao vivo (item 8, R3, R-8 e R-9): o Lavalink só toca ``http://127.0.0.1:<porta>/r/<token>``.

O Lavalink nunca recebe URL, playlist nem cabeçalho de estação: o lavaplayer segue o ``Location`` de um redirecionamento e cada entrada de PLS/M3U
como referência nova, e transforma ``icy-name`` em autor (SSRF e texto sem filtro). O relay fica no meio e faz tudo isso por ele:

- **Entrada:** escuta só em ``127.0.0.1``; teto de 64 sockets; o token tem 128 bits e é comparado em tempo constante; token desconhecido fecha
  SEM ler o corpo; aceita UM consumidor por vez enquanto durar o play; só a linha de pedido é lida (2 KiB, 5 s).
- **Saída para a estação:** ``stream_client.open_stream`` (IP público fixado, SNI no nome, ``Icy-MetaData: 1``); semáforo global de 4 aberturas;
  redirecionamento seguido pelo relay, até 3 saltos, cada um conferido de novo; URL que é playlist (``.pls``/``.m3u``) é resolvida AQUI (16 KiB,
  10 entradas, cada uma pelas mesmas regras) para o Lavalink nunca receber playlist.
- **Antes do primeiro byte:** ``Content-Type`` de áudio e assinatura dos primeiros bytes de acordo com o codec; qualquer outra coisa encerra.
- **Cabeçalhos:** o relay manda ao Lavalink só os seus, fixos, sem nenhum ``icy-*``; os metadados saem do fluxo (``IcyStripper``).
- **Fluxo:** até 1,5 × o bitrate (pela contrapressão do TCP: lê menos), 15 s parado encerra.
- **Pausa:** ``revoke`` fecha a saída e invalida o token; a retomada pede um token novo.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from empiremuse.rules.icy import IcyStripper, TitleThrottle, check_metaint, clean_title
from empiremuse.rules.playlists import is_public_address
from empiremuse.rules.radio_stations import (
    PLAYLIST_MAX_BYTES,
    content_type_ok,
    looks_like_playlist,
    parse_playlist,
    safe_station_url_for_log,
    signature_ok,
)
from empiremuse.utils.radio.stream_client import (
    PublicCheck,
    Resolve,
    StreamRefused,
    StreamResponse,
    open_stream,
    redirect_target,
    system_resolve,
)

logger = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
MAX_SOCKETS = 64
OPEN_CONCURRENCY = 4
MAX_REDIRECTS = 3
REQUEST_LINE_LIMIT = 2048
REQUEST_TIMEOUT_S = 5.0
IDLE_TIMEOUT_S = 15.0
DRAIN_TIMEOUT_S = 30.0  # consumidor que não lê e não fecha
DEADLINE_S = 20.0  # abrir uma estação inteira (DNS, saltos, playlist, primeiros bytes) dentro do semáforo
PLAYLIST_READ_S = 10.0
CHUNK = 8192
RATE_FACTOR = 1.5
PROBE_BYTES = 16 * 1024  # o health check lê isto pelo mesmo caminho do relay
PROBE_MIN_BYTES = 1024
DEFAULT_BITRATE_KBPS = 320  # sem bitrate informado, o teto é o do maior bitrate aceito
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22}")
_REQUEST = re.compile(r"GET /r/([A-Za-z0-9_-]{22}) HTTP/1\.[01]")


class RateLimiter:
    """Balde de fichas: quanto esperar antes de ler mais bytes da estação para o consumo ficar em até ``RATE_FACTOR`` × o bitrate. O crédito
    acumulado nunca passa da rajada (``burst_s`` segundos), então horas a 1x não compram um despejo depois. Puro: o relógio vem de fora."""

    def __init__(self, bitrate_kbps: int, *, burst_s: float = 2.0) -> None:
        kbps = bitrate_kbps if bitrate_kbps > 0 else DEFAULT_BITRATE_KBPS
        self.rate = kbps * 1000 / 8 * RATE_FACTOR  # bytes por segundo
        self.burst = self.rate * burst_s
        self._tokens = self.burst
        self._at: float | None = None

    def delay_for(self, nbytes: int, now: float) -> float:
        if self._at is not None:
            self._tokens = min(self.burst, self._tokens + max(0.0, now - self._at) * self.rate)
        self._at = now
        self._tokens -= nbytes
        return -self._tokens / self.rate if self._tokens < 0 else 0.0


@dataclass
class RelaySession:
    token: str
    url: str
    codec: str
    bitrate: int
    on_title: Callable[[str], None] | None = None
    busy: bool = False
    refused: bool = False
    hits: int = 0  # quantas vezes o Lavalink chegou ao relay com este token (distingue "a estação falhou" de "o Lavalink nem chegou")
    output: asyncio.StreamWriter | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class Upstream:
    response: StreamResponse
    first: bytes
    content_type: str
    metaint: int | None


class RadioRelay:
    def __init__(
        self,
        *,
        resolve: Resolve = system_resolve,
        public: PublicCheck = is_public_address,
        max_sockets: int = MAX_SOCKETS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
        idle_timeout: float = IDLE_TIMEOUT_S,
    ) -> None:
        self._resolve = resolve
        self._public = public
        self._max_sockets = max_sockets
        self._clock = clock
        self._sleep = sleep
        self._idle = idle_timeout
        self._sessions: dict[str, RelaySession] = {}
        self._open = asyncio.Semaphore(OPEN_CONCURRENCY)
        self._sockets = 0
        self._server: asyncio.AbstractServer | None = None
        self._start_lock = asyncio.Lock()
        self.port = 0

    # ------------------------------------------------------------------ ciclo de vida

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, LOOPBACK, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def ensure_started(self) -> int:
        """Sobe o servidor local uma vez (sob demanda, na primeira rádio ao vivo que toca). Devolve a porta."""
        async with self._start_lock:
            if self._server is None:
                await self.start()
        return self.port

    async def close(self) -> None:
        for token in list(self._sessions):
            await self.revoke(token)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------------ tokens

    def register(self, url: str, *, codec: str, bitrate: int, on_title: Callable[[str], None] | None = None) -> str:
        """Um token novo (128 bits) para tocar ``url``. O Lavalink recebe ``local_url(token)``, nunca a URL da estação."""
        token = secrets.token_urlsafe(16)
        self._sessions[token] = RelaySession(token, url, codec, bitrate, on_title)
        return token

    def local_url(self, token: str) -> str:
        return f"http://{LOOPBACK}:{self.port}/r/{token}"

    async def revoke(self, token: str) -> None:
        """Pausa/parada: invalida o token e fecha a saída que estiver aberta."""
        session = self._sessions.pop(token, None)
        if session is None:
            return
        if session.task is not None:
            session.task.cancel()
        if session.output is not None:
            session.output.close()

    def sessions(self) -> int:
        """Quantas saídas estão ABERTAS agora (o "tocando" do resumo da R1)."""
        return sum(1 for s in self._sessions.values() if s.busy)

    def _find(self, token: str) -> RelaySession | None:
        """Acha a sessão comparando em tempo constante com TODOS os tokens (sem atalho que vaze qual prefixo acertou)."""
        found = None
        for known, session in self._sessions.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                found = session
        return found

    # ------------------------------------------------------------------ entrada

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._sockets >= self._max_sockets:
            writer.close()
            return
        self._sockets += 1
        try:
            await self._serve(reader, writer)
        except Exception as error:  # noqa: BLE001 - nada de uma estação derruba o servidor; só o tipo vai ao log (R-12)
            logger.warning("Relay encerrou uma saída (%s)", type(error).__name__)
        finally:
            self._sockets -= 1
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT_S)
        except TimeoutError, ValueError:
            return
        if len(line) > REQUEST_LINE_LIMIT:
            return
        match = _REQUEST.fullmatch(line.decode("latin-1").rstrip("\r\n"))
        if match is None:
            return  # nada além da linha de pedido é lido; token desconhecido fecha sem ler o corpo
        session = self._find(match.group(1))
        if session is None:
            return
        session.hits += 1
        if session.busy and not await self._take_over(session):
            await self._reply(writer, "409 Conflict")
            return
        session.busy = True
        session.output = writer
        session.task = asyncio.current_task()
        try:
            async with self._open:
                upstream = await self._connect(session)
            await self._pump(session, upstream, writer)
        except StreamRefused as refusal:
            session.refused = True
            logger.warning(
                "Estação recusada pelo relay [%s] (%s)", safe_station_url_for_log(session.url), refusal.reason
            )
            await self._reply(writer, "502 Bad Gateway")
        finally:
            session.busy = False
            session.output = None
            session.task = None

    @staticmethod
    async def _take_over(session: RelaySession) -> bool:
        """O Lavalink abre o endereço duas vezes (carregar a faixa e tocar) e a primeira saída pode ainda estar aberta: o último consumidor vence.
        Continua UM consumidor por vez, porque a saída anterior é fechada e esperada antes de a nova começar."""
        previous, output = session.task, session.output
        if output is not None:
            output.close()
        if previous is not None and previous is not asyncio.current_task():
            previous.cancel()
            await asyncio.wait({previous}, timeout=2)
        return not session.busy

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: str) -> None:
        try:
            writer.write(f"HTTP/1.0 {status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode("ascii"))
            await asyncio.wait_for(writer.drain(), 2)
        except Exception:  # noqa: BLE001 - o consumidor já saiu
            pass

    # ------------------------------------------------------------------ saída para a estação

    async def _connect(self, session: RelaySession) -> Upstream:
        return await self._open_within_deadline(session.url, session.codec)

    async def _open_within_deadline(self, url: str, codec: str) -> Upstream:
        """Abre a estação inteira (saltos, playlist, primeiros bytes) em até ``DEADLINE_S``: estação lenta de propósito não segura as 4 vagas."""
        try:
            async with asyncio.timeout(DEADLINE_S):
                return await self._resolve_url(url, codec, allow_playlist=True)
        except TimeoutError as error:
            raise StreamRefused("timeout") from error

    def touched(self, token: str) -> bool:
        """O Lavalink chegou a pedir este token?"""
        session = self._sessions.get(token)
        return session is not None and session.hits > 0

    def refused(self, token: str) -> bool:
        session = self._sessions.get(token)
        return session is not None and session.refused

    async def _resolve_url(self, url: str, codec: str, *, allow_playlist: bool) -> Upstream:
        """Segue até 3 redirecionamentos (cada salto de novo pelas regras), resolve playlist (uma vez, aqui) e confere tipo e assinatura."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            response = await open_stream(current, resolve=self._resolve, public=self._public)
            try:
                if (target := redirect_target(current, response)) is not None:
                    await response.close()
                    current = target
                    continue
                return await self._accept(response, current, codec, allow_playlist=allow_playlist)
            except BaseException:
                await (
                    response.close()
                )  # inclusive o cancelamento (prazo, revogação): nenhum socket da estação fica aberto
                raise
        raise StreamRefused("redirects")

    async def _accept(self, response: StreamResponse, current: str, codec: str, *, allow_playlist: bool) -> Upstream:
        if response.status != 200:
            raise StreamRefused("status")
        content_type = response.headers.get("content-type", "")
        if looks_like_playlist(current, content_type):
            if not allow_playlist:
                raise StreamRefused("playlist")
            entries = await self._read_playlist(response)
            return await self._first_playable(entries, codec)
        if not content_type_ok(content_type):
            raise StreamRefused("content-type")
        try:
            first = await asyncio.wait_for(response.reader.readexactly(4), self._idle)
        except Exception as error:  # noqa: BLE001 - fechou antes dos 4 bytes ou parou
            raise StreamRefused("empty") from error
        if not signature_ok(codec, first):
            raise StreamRefused("signature")
        return Upstream(
            response,
            first,
            content_type.split(";", 1)[0].strip().lower(),
            check_metaint(response.headers.get("icy-metaint")),
        )

    @staticmethod
    async def _read_playlist(response: StreamResponse) -> list[str]:
        async def collect() -> bytes:
            body = bytearray()
            while (
                len(body) < PLAYLIST_MAX_BYTES
            ):  # read() devolve o que chegou, não o que foi pedido: lê até EOF ou o teto
                chunk = await response.reader.read(PLAYLIST_MAX_BYTES - len(body))
                if not chunk:
                    break
                body += chunk
            return bytes(body)

        try:
            body = await asyncio.wait_for(collect(), PLAYLIST_READ_S)
        except Exception as error:  # noqa: BLE001
            raise StreamRefused("playlist") from error
        finally:
            await response.close()
        entries = parse_playlist(body)
        if not entries:
            raise StreamRefused("playlist")
        return entries

    async def _first_playable(self, entries: list[str], codec: str) -> Upstream:
        last = StreamRefused("playlist")
        for entry in entries:
            try:
                return await self._resolve_url(entry, codec, allow_playlist=False)
            except StreamRefused as refusal:
                last = refusal
        raise last

    async def probe(self, url: str, codec: str) -> bool:
        """O health check (R-10): abre a estação pelo MESMO caminho do relay (R-8) e lê até 16 KiB. ``True`` se chegou áudio. Nunca clica no diretório."""
        try:
            async with self._open:
                upstream = await self._open_within_deadline(url, codec)
        except StreamRefused:
            return False
        got = len(upstream.first)
        try:
            while got < PROBE_BYTES:
                chunk = await asyncio.wait_for(upstream.response.reader.read(CHUNK), self._idle)
                if not chunk:
                    break
                got += len(chunk)
        except TimeoutError, ConnectionError, asyncio.IncompleteReadError:
            pass
        finally:
            await upstream.response.close()
        return got >= PROBE_MIN_BYTES

    # ------------------------------------------------------------------ fluxo

    async def _pump(self, session: RelaySession, upstream: Upstream, writer: asyncio.StreamWriter) -> None:
        stripper = IcyStripper(upstream.metaint)
        limiter = RateLimiter(session.bitrate)
        throttle = TitleThrottle()
        response = upstream.response
        last_audio = self._clock()
        try:
            writer.write(
                f"HTTP/1.0 200 OK\r\nContent-Type: {upstream.content_type}\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n".encode(
                    "ascii"
                )
            )
            pending: bytes | None = upstream.first
            while True:
                if pending is not None:
                    chunk, pending = pending, None
                else:
                    chunk = await asyncio.wait_for(response.reader.read(CHUNK), self._idle)
                    if not chunk:
                        return
                now = self._clock()
                audio, titles = stripper.feed(chunk)
                published = None
                for title in titles:
                    published = throttle.offer(clean_title(title), now) or published  # R-11 e no máximo 1 a cada 15 s
                published = throttle.poll(now) or published
                if published is not None and session.on_title is not None:
                    session.on_title(published)
                if audio:
                    last_audio = now
                    writer.write(audio)
                    await asyncio.wait_for(writer.drain(), DRAIN_TIMEOUT_S)
                elif now - last_audio > self._idle:
                    return  # só metadado há tempo demais: não é um fluxo de áudio
                delay = limiter.delay_for(len(chunk), now)  # o que a ESTAÇÃO mandou conta, não só o áudio que sobrou
                if delay > 0:
                    await self._sleep(delay)  # type: ignore[misc]
        except TimeoutError, ConnectionError, asyncio.IncompleteReadError:
            return
        finally:
            await response.close()
