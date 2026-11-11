"""Configuração validada do painel web (variáveis ``PANEL_*``)."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from empiremuse.rules.client_ip import IPNetwork, parse_trusted_proxy_cidrs

logger = logging.getLogger(__name__)

COOKIE_SECRET_MIN_LENGTH = 32
GENERATED_COOKIE_SECRET_BYTES = 48
SESSION_TTL_HOURS_RANGE = (1, 168)
SESSION_IDLE_MINUTES_RANGE = (5, 1440)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CALLBACK_PATH = "/painel/callback"


class PanelConfigError(ValueError):
    """Configuração inválida do painel. A mensagem nunca contém valores de segredos."""


@dataclass(frozen=True)
class PanelSettings:
    enabled: bool
    base_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = field(default="", repr=False)
    cookie_secret: str = field(default="", repr=False)
    session_ttl_hours: int = 12
    session_idle_minutes: int = 60
    trust_proxy: bool = False
    trusted_proxy_networks: tuple[IPNetwork, ...] = field(default_factory=lambda: parse_trusted_proxy_cidrs(None)[0])
    insecure_cookies: bool = False

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}{CALLBACK_PATH}"

    @property
    def is_https(self) -> bool:
        return self.base_url.startswith("https://")


def _parse_base_url(raw: Any) -> tuple[str, str]:
    """Valida ``PANEL_BASE_URL`` e devolve ``(url_sem_barra_final, host)``."""
    value = str(raw or "").strip()
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - valida a porta (levanta ValueError se inválida)
    except ValueError:
        raise PanelConfigError("PANEL_BASE_URL inválida: informe uma URL absoluta http(s) válida.") from None
    if parts.scheme not in ("http", "https") or not host:
        raise PanelConfigError(
            "PANEL_BASE_URL inválida: use uma URL absoluta http(s) com host (ex.: https://painel.exemplo.com)."
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise PanelConfigError("PANEL_BASE_URL inválida: não deve conter caminho, query ou fragmento.")
    return value.rstrip("/"), host


def _bounded_int(config: Mapping[str, Any], key: str, default: int, bounds: tuple[int, int]) -> int:
    raw = config.get(key, default)
    try:
        number = int(raw)
    except TypeError, ValueError:
        raise PanelConfigError(f"{key} deve ser um número inteiro.") from None
    if isinstance(raw, bool) or not bounds[0] <= number <= bounds[1]:
        raise PanelConfigError(f"{key} deve estar entre {bounds[0]} e {bounds[1]}.")
    return number


def _resolve_cookie_secret(raw: Any) -> str:
    secret = str(raw or "")
    if not secret:
        logger.warning(
            "PANEL_COOKIE_SECRET vazio: gerado um segredo aleatório; as sessões do painel não sobrevivem a reinícios."
        )
        return secrets.token_urlsafe(GENERATED_COOKIE_SECRET_BYTES)
    if len(secret) < COOKIE_SECRET_MIN_LENGTH:
        raise PanelConfigError(f"PANEL_COOKIE_SECRET deve ter no mínimo {COOKIE_SECRET_MIN_LENGTH} caracteres.")
    return secret


def _check_transport(base_url: str, host: str, insecure_cookies: bool) -> None:
    is_local = host in LOCAL_HOSTS
    if insecure_cookies and not is_local:
        raise PanelConfigError(
            "PANEL_INSECURE_COOKIES=true só é permitido com host local (localhost, 127.0.0.1 ou ::1)."
        )
    if not base_url.startswith("https://") and not (insecure_cookies and is_local):
        raise PanelConfigError("PANEL_BASE_URL deve usar https (http só em localhost com PANEL_INSECURE_COOKIES=true).")


def load_session_limits(config: Mapping[str, Any]) -> PanelSettings:
    """Só os prazos de sessão (``PANEL_SESSION_*``), para o modo externo com o painel genérico desligado."""
    return PanelSettings(
        enabled=False,
        session_ttl_hours=_bounded_int(config, "PANEL_SESSION_TTL_HOURS", 12, SESSION_TTL_HOURS_RANGE),
        session_idle_minutes=_bounded_int(config, "PANEL_SESSION_IDLE_MINUTES", 60, SESSION_IDLE_MINUTES_RANGE),
    )


def load_panel_settings(config: Mapping[str, Any]) -> PanelSettings:
    """Monta e valida ``PanelSettings``. Com o painel desligado nada além do tipo é validado."""
    if not config.get("PANEL_ENABLED", False):
        return PanelSettings(enabled=False)

    base_url, host = _parse_base_url(config.get("PANEL_BASE_URL"))
    insecure_cookies = bool(config.get("PANEL_INSECURE_COOKIES", False))
    _check_transport(base_url, host, insecure_cookies)

    oauth_secret = str(config.get("PANEL_OAUTH_CLIENT_SECRET") or "")
    if not oauth_secret:
        raise PanelConfigError("PANEL_OAUTH_CLIENT_SECRET é obrigatório com o painel ativo.")

    networks, cidr_warnings = parse_trusted_proxy_cidrs(config.get("PANEL_TRUSTED_PROXY_CIDRS"))
    for warning in cidr_warnings:
        logger.warning("%s", warning)

    return PanelSettings(
        enabled=True,
        base_url=base_url,
        oauth_client_id=str(config.get("PANEL_OAUTH_CLIENT_ID") or ""),
        oauth_client_secret=oauth_secret,
        cookie_secret=_resolve_cookie_secret(config.get("PANEL_COOKIE_SECRET")),
        session_ttl_hours=_bounded_int(config, "PANEL_SESSION_TTL_HOURS", 12, SESSION_TTL_HOURS_RANGE),
        session_idle_minutes=_bounded_int(config, "PANEL_SESSION_IDLE_MINUTES", 60, SESSION_IDLE_MINUTES_RANGE),
        trust_proxy=bool(config.get("PANEL_TRUST_PROXY", False)),
        trusted_proxy_networks=networks,
        insecure_cookies=insecure_cookies,
    )
