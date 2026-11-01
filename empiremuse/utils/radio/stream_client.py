"""O cliente HTTP/1.0 da saída do relay (item 8, R3, R-8): abre UM fluxo de uma estação sem confiar em nada dela.

Por que um cliente próprio: servidores Shoutcast antigos respondem ``ICY 200 OK`` e o ``aiohttp`` recusa essa linha de status (testado). Em
HTTP/1.0 a resposta nunca vem em ``chunked``, o ``Connection: close`` encerra limpo e dá para fixar tudo:

- a URL passa por ``rules.radio_stations.check_stream_url`` (http/https, sem credencial, porta 80/443/8000-8999);
- o nome é resolvido AQUI e TODO endereço tem de ser público (``is_public_address``; cobre IPv4 dentro de IPv6 e NAT64); a conexão vai
  direto no IP conferido, com o SNI e o certificado conferidos no NOME do host (nunca ``ssl=False``, nunca IP com SNI inventado);
- prazos: conexão 5 s, cabeçalhos 10 s; cabeçalhos até 16 KiB e 100 linhas; sem cookie, sem proxy, ``Accept-Encoding: identity``;
- nunca segue redirecionamento: devolve a resposta e quem chama (o relay) decide, conferindo cada salto de novo.

Erros viram ``StreamRefused`` com um código estável; a URL nunca vai na exceção nem no log (R-12: só o host).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from empiremuse.rules.playlists import is_public_address
from empiremuse.rules.radio_stations import check_stream_url, safe_station_url_for_log

logger = logging.getLogger(__name__)

USER_AGENT = "EmpireMuse/radio"
CONNECT_TIMEOUT_S = 5.0
HEADER_TIMEOUT_S = 10.0
RESOLVE_TIMEOUT_S = 5.0
MAX_ADDRESSES = 3
MAX_HEADER_BYTES = 16 * 1024
MAX_HEADER_LINES = 100
DEFAULT_PORTS = {"http": 80, "https": 443}

Resolve = Callable[[str, int], Awaitable[Sequence[str]]]
PublicCheck = Callable[[str], bool]

URL_REFUSED = "url"
ADDRESS_REFUSED = "address"
CONNECT_FAILED = "connect"
TLS_FAILED = "tls"
TIMEOUT = "timeout"
BAD_RESPONSE = "response"


class StreamRefused(Exception):
    """A estação foi recusada ou falhou; ``reason`` é um dos códigos acima (nunca o texto da estação nem a URL)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class StreamResponse:
    status: int
    headers: dict[str, str]  # nomes em minúsculas; o primeiro valor de cada cabeçalho vale
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    host: str

    async def close(self) -> None:
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except Exception:  # noqa: BLE001 - fechar nunca falha para quem chama
            pass


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _split(url: str) -> tuple[str, str, int, str]:
    """``(scheme, host, port, caminho com query)`` de uma URL já aprovada por ``check_stream_url``."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or DEFAULT_PORTS[parts.scheme]
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, path


def _host_header(host: str, port: int, scheme: str) -> str:
    shown = f"[{host}]" if ":" in host else host
    return shown if port == DEFAULT_PORTS[scheme] else f"{shown}:{port}"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _addresses(host: str, port: int, resolve: Resolve, public: PublicCheck) -> list[str]:
    """Os endereços a tentar. Todos têm de ser públicos: um nome que também aponta para a rede interna (rebinding) é recusado inteiro."""
    try:
        found = [host] if _is_ip(host) else list(await asyncio.wait_for(resolve(host, port), RESOLVE_TIMEOUT_S))
    except (
        OSError,
        UnicodeError,
        TimeoutError,
    ) as error:  # NXDOMAIN, rótulo inválido, DNS lento: recusa, nunca estoura para quem chama
        raise StreamRefused(ADDRESS_REFUSED) from error
    if not found or not all(public(address) for address in found):
        raise StreamRefused(ADDRESS_REFUSED)
    return found[:MAX_ADDRESSES]  # TODOS foram conferidos; só se tenta alguns (cada um custa 5 s de conexão)


def _parse_head(raw: bytes) -> tuple[int, dict[str, str]]:
    lines = raw.decode("latin-1").split("\r\n")
    if len(lines) > MAX_HEADER_LINES:
        raise StreamRefused(BAD_RESPONSE)
    status_line = lines[0].split(" ", 2)
    if len(status_line) < 2 or status_line[0] not in ("HTTP/1.0", "HTTP/1.1", "ICY") or not status_line[1].isdecimal():
        raise StreamRefused(BAD_RESPONSE)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            continue
        headers.setdefault(name.strip().lower(), value.strip())
    return int(status_line[1]), headers


def _request(host: str, port: int, scheme: str, path: str, *, want_icy: bool) -> bytes:
    lines = [
        f"GET {path} HTTP/1.0",
        f"Host: {_host_header(host, port, scheme)}",
        f"User-Agent: {USER_AGENT}",
        "Accept: */*",
        "Accept-Encoding: identity",
    ]
    if want_icy:
        lines.append("Icy-MetaData: 1")
    lines += ["Connection: close", "", ""]
    return "\r\n".join(lines).encode("ascii")


async def open_stream(
    url: object,
    *,
    resolve: Resolve = system_resolve,
    public: PublicCheck = is_public_address,
    ssl_context: ssl.SSLContext | None = None,
    connect_timeout: float = CONNECT_TIMEOUT_S,
    header_timeout: float = HEADER_TIMEOUT_S,
    want_icy: bool = True,
) -> StreamResponse:
    """Abre o fluxo e devolve a resposta (status, cabeçalhos, leitor) com o corpo ainda por ler. ``StreamRefused`` em qualquer falha."""
    checked = check_stream_url(url)
    if checked is None:
        raise StreamRefused(URL_REFUSED)
    scheme, host, port, path = _split(checked)
    addresses = await _addresses(host, port, resolve, public)
    context = (ssl_context or ssl.create_default_context()) if scheme == "https" else None
    last_error = CONNECT_FAILED
    for address in addresses:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    address, port, ssl=context, server_hostname=host if context else None, limit=MAX_HEADER_BYTES * 2
                ),
                connect_timeout,
            )
        except TimeoutError:
            last_error = TIMEOUT
            continue
        except ssl.SSLError:
            last_error = TLS_FAILED
            continue
        except OSError:
            last_error = CONNECT_FAILED
            continue
        try:
            writer.write(_request(host, port, scheme, path, want_icy=want_icy))
            await asyncio.wait_for(writer.drain(), header_timeout)
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), header_timeout)
            if len(head) > MAX_HEADER_BYTES:
                raise StreamRefused(BAD_RESPONSE)
            status, headers = _parse_head(head[:-4])
            if headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise StreamRefused(BAD_RESPONSE)
            return StreamResponse(status, headers, reader, writer, host)
        except StreamRefused:
            writer.close()
            raise
        except TimeoutError:
            writer.close()
            last_error = TIMEOUT
        except Exception:  # noqa: BLE001 - leitura truncada, cabeçalho enorme, reset: fecha e tenta o próximo endereço
            writer.close()
            last_error = BAD_RESPONSE
        except BaseException:  # cancelamento (prazo, revogação): o socket da estação não pode ficar aberto
            writer.close()
            raise
    logger.warning("Estação não abriu [%s] (%s)", safe_station_url_for_log(checked), last_error)
    raise StreamRefused(last_error)


def redirect_target(base_url: str, response: StreamResponse) -> str | None:
    """O destino de um redirecionamento (301/302/303/307/308), resolvido contra a URL atual; ``None`` se não é redirecionamento ou o destino
    não passa em ``check_stream_url``. Quem segue confere o salto de novo em ``open_stream`` (endereço público, porta, credencial)."""
    if response.status not in (301, 302, 303, 307, 308):
        return None
    location = response.headers.get("location")
    if not location or len(location) > 2048:
        return None
    return check_stream_url(urljoin(base_url, location))
