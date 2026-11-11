"""``ExternalPanelSettings``: a configuração do modo externo, validada e fail-closed (contrato T-456 §2 e §6).

Qualquer erro levanta ``ExternalPanelConfigError`` (a mensagem nunca traz valores de segredos); quem chama decide que nada
sobe, nem a API nem o painel genérico, e o bot segue tocando.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Mapping
from urllib.parse import urlsplit

from empiremuse.panel.settings import PanelConfigError, _check_transport, _parse_base_url

logger = logging.getLogger(__name__)

URL_KEY = "EXTERNAL_PANEL_URL"
SECRET_KEY = "EXTERNAL_PANEL_API_SECRET"
PREVIOUS_SECRET_KEY = "EXTERNAL_PANEL_API_SECRET_PREVIOUS"
PREVIOUS_UNTIL_KEY = "EXTERNAL_PANEL_API_PREVIOUS_UNTIL"
SIGNING_SECRET_KEY = "EXTERNAL_PANEL_SIGNING_SECRET"
PORT_KEY = "EXTERNAL_PANEL_API_PORT"
BIND_KEY = "EXTERNAL_PANEL_API_BIND"
CIDRS_KEY = "EXTERNAL_PANEL_API_CIDRS"
EXTERNAL_KEYS = (
    URL_KEY,
    SECRET_KEY,
    PREVIOUS_SECRET_KEY,
    PREVIOUS_UNTIL_KEY,
    SIGNING_SECRET_KEY,
    PORT_KEY,
    BIND_KEY,
    CIDRS_KEY,
)

SECRET_MIN_LENGTH = 32
PREVIOUS_MAX_DAYS_AHEAD = 7
CALLBACK_PATH = "/auth/callback"
MAX_PORT = 65535

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class ExternalPanelConfigError(ValueError):
    """Configuração inválida do modo externo. A mensagem nunca contém valores de segredos."""


@dataclass(frozen=True)
class ExternalPanelSettings:
    base_url: str
    service_key: str = field(repr=False)
    signing_secret: str = field(repr=False)
    oauth_client_id: str
    oauth_client_secret: str = field(repr=False)
    bind: str
    port: int
    cidrs: tuple[IPNetwork, ...]
    previous_key: str = field(default="", repr=False)
    previous_expires_at: float | None = None

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}{CALLBACK_PATH}"  # ``base_url`` pode levar prefixo de caminho (``…/panel``)


def _text(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    return "" if value is None or isinstance(value, bool) else str(value).strip()


def external_panel_requested(config: Mapping[str, Any]) -> bool:
    """Se alguma variável ``EXTERNAL_PANEL_*`` foi preenchida (então o modo externo vale e a configuração tem de estar certa)."""
    return any(_text(config, key) for key in EXTERNAL_KEYS)


PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+", re.ASCII)
DOT_SEGMENTS = frozenset({".", ".."})


def _normalized_prefix(path: str) -> str:
    """O prefixo de caminho do painel (``/panel``, ``/a/b``) sem a barra final; vazio é válido (``/`` também).

    Cada segmento: só ``A-Za-z0-9._~-``, nunca vazio, ``.`` nem ``..``; sem ``%`` (nada de caminho codificado). Falha fechado.
    """
    prefix = path.removesuffix("/")
    if not prefix:
        return ""
    segments = prefix.split("/")
    if segments[0] != "" or any(not PATH_SEGMENT.fullmatch(s) or s in DOT_SEGMENTS for s in segments[1:]):
        raise ExternalPanelConfigError(
            f"{URL_KEY} inválida: o caminho só aceita segmentos de A-Z a-z 0-9 . _ ~ - (sem vazio, . ou ..)."
        )
    return prefix


def _parse_url(config: Mapping[str, Any]) -> str:
    raw = _text(config, URL_KEY)
    if not raw:
        raise ExternalPanelConfigError(f"{URL_KEY} é obrigatória no modo externo.")
    if "\\" in raw or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw) or "@" in raw:
        raise ExternalPanelConfigError(
            f"{URL_KEY} inválida: sem espaços, controles, barra invertida ou usuário e senha."
        )
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - valida a porta
    except ValueError:
        raise ExternalPanelConfigError(f"{URL_KEY} inválida: informe uma URL absoluta https válida.") from None
    if parts.query or parts.fragment or "?" in raw or "#" in raw:
        raise ExternalPanelConfigError(f"{URL_KEY} inválida: não deve conter query nem fragmento.")
    prefix = _normalized_prefix(parts.path)
    try:
        # o genérico segue recusando qualquer caminho: aqui ele só valida esquema, host e porta da origem
        origin, host = _parse_base_url(f"{parts.scheme}://{parts.netloc}")
        _check_transport(origin, host, bool(config.get("PANEL_INSECURE_COOKIES", False)))
    except PanelConfigError as error:
        raise ExternalPanelConfigError(str(error).replace("PANEL_BASE_URL", URL_KEY)) from None
    return origin + prefix


def _parse_port(config: Mapping[str, Any], web_port: int) -> int:
    raw = _text(config, PORT_KEY)
    if not raw:
        raise ExternalPanelConfigError(f"{PORT_KEY} é obrigatória (sem padrão).")
    try:
        port = int(raw)
    except ValueError:
        raise ExternalPanelConfigError(f"{PORT_KEY} deve ser um número de porta.") from None
    if not 1 <= port <= MAX_PORT:
        raise ExternalPanelConfigError(f"{PORT_KEY} deve estar entre 1 e {MAX_PORT}.")
    if port == web_port:
        raise ExternalPanelConfigError(f"{PORT_KEY} não pode ser a porta pública do bot (PORT).")
    return port


def _parse_bind(config: Mapping[str, Any]) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    raw = _text(config, BIND_KEY)
    if not raw:
        raise ExternalPanelConfigError(f"{BIND_KEY} é obrigatório (sem padrão).")
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        raise ExternalPanelConfigError(
            f"{BIND_KEY} deve ser um endereço IP (o IP fixo do bot na rede dedicada)."
        ) from None
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id:
        raise ExternalPanelConfigError(f"{BIND_KEY} não aceita escopo de interface.")
    return address


def _parse_cidrs(config: Mapping[str, Any]) -> tuple[IPNetwork, ...]:
    entries = [item.strip() for item in _text(config, CIDRS_KEY).split(",") if item.strip()]
    networks = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=True)
        except ValueError:
            raise ExternalPanelConfigError(
                f"{CIDRS_KEY} tem uma entrada inválida (use sub-redes como 172.20.0.0/24)."
            ) from None
        if network.prefixlen == 0:
            raise ExternalPanelConfigError(f"{CIDRS_KEY} não aceita a rede inteira (/0): informe a sub-rede dedicada.")
        networks.append(network)
    return tuple(networks)


def _parse_previous(config: Mapping[str, Any], now: float) -> tuple[str, float | None]:
    """A chave anterior e o instante em que ela deixa de valer; sem data válida e futura ela é ignorada (aviso alto)."""
    previous = _text(config, PREVIOUS_SECRET_KEY)
    if not previous:
        return "", None
    try:
        until = date.fromisoformat(_text(config, PREVIOUS_UNTIL_KEY))
    except ValueError:
        until = None
    today = datetime.fromtimestamp(now, UTC).date()
    if until is None or until < today or until > today + timedelta(days=PREVIOUS_MAX_DAYS_AHEAD):
        logger.warning(
            "%s ignorada: informe %s (AAAA-MM-DD, hoje ou até %d dias à frente) para a rotação valer.",
            PREVIOUS_SECRET_KEY,
            PREVIOUS_UNTIL_KEY,
            PREVIOUS_MAX_DAYS_AHEAD,
        )
        return "", None
    expires = datetime.combine(until + timedelta(days=1), datetime.min.time(), UTC).timestamp()
    logger.warning("%s em uso até %s (UTC): conclua a rotação e remova-a.", PREVIOUS_SECRET_KEY, until.isoformat())
    return previous, expires


def _check_secrets(secrets_by_name: Mapping[str, str]) -> None:
    for name, value in secrets_by_name.items():
        if name in (SECRET_KEY, PREVIOUS_SECRET_KEY, SIGNING_SECRET_KEY) and value and len(value) < SECRET_MIN_LENGTH:
            raise ExternalPanelConfigError(f"{name} deve ter no mínimo {SECRET_MIN_LENGTH} caracteres.")
    present = [(name, value) for name, value in secrets_by_name.items() if value]
    for index, (name, value) in enumerate(present):
        for other_name, other_value in present[index + 1 :]:
            if value == other_value:
                raise ExternalPanelConfigError(f"{name} e {other_name} não podem ser iguais.")


def load_external_panel_settings(
    config: Mapping[str, Any], *, web_port: int, now: float
) -> ExternalPanelSettings | None:
    """``None`` com o modo externo desligado (nenhuma ``EXTERNAL_PANEL_*``); senão a configuração validada ou o erro."""
    if not external_panel_requested(config):
        return None

    url = _parse_url(config)
    service_key = _text(config, SECRET_KEY)
    signing_secret = _text(config, SIGNING_SECRET_KEY)
    oauth_secret = _text(config, "PANEL_OAUTH_CLIENT_SECRET")
    for name, value in ((SECRET_KEY, service_key), (SIGNING_SECRET_KEY, signing_secret)):
        if not value:
            raise ExternalPanelConfigError(f"{name} é obrigatória no modo externo.")
    if not oauth_secret:
        raise ExternalPanelConfigError("PANEL_OAUTH_CLIENT_SECRET é obrigatório no modo externo.")
    port = _parse_port(config, web_port)
    bind = _parse_bind(config)
    cidrs = _parse_cidrs(config)
    if not bind.is_loopback and not cidrs:
        raise ExternalPanelConfigError(
            f"{CIDRS_KEY} é obrigatório quando {BIND_KEY} não é loopback (a sub-rede da rede dedicada do painel)."
        )
    previous_key, previous_expires_at = _parse_previous(config, now)
    _check_secrets(
        {
            SECRET_KEY: service_key,
            PREVIOUS_SECRET_KEY: previous_key,
            SIGNING_SECRET_KEY: signing_secret,
            "PANEL_OAUTH_CLIENT_SECRET": oauth_secret,
            "PANEL_COOKIE_SECRET": _text(config, "PANEL_COOKIE_SECRET"),
            "RPC_SERVER_SECRET": _text(config, "RPC_SERVER_SECRET"),  # sem piso de tamanho: só a unicidade vale
        }
    )
    return ExternalPanelSettings(
        base_url=url,
        service_key=service_key,
        signing_secret=signing_secret,
        oauth_client_id=_text(config, "PANEL_OAUTH_CLIENT_ID"),
        oauth_client_secret=oauth_secret,
        bind=str(bind),
        port=port,
        cidrs=cidrs,
        previous_key=previous_key,
        previous_expires_at=previous_expires_at,
    )
