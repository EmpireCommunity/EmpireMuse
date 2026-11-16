"""Utilitários compartilhados dos testes do painel web (Etapa 1 — Fundação).

Contrato: BRIEFING-PAINEL-E1.md. Os módulos do painel são importados de forma lazy
para que cada teste falhe individualmente (RED) enquanto a implementação não existe.
"""

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = ROOT / "empiremuse" / "panel"
VENDOR_DIR = PANEL_DIR / "static" / "vendor"
TEMPLATES_DIR = PANEL_DIR / "templates"

OAUTH_SECRET = "oauth-SEGREDO-0123456789"
COOKIE_SECRET = "cookie-SEGREDO-0123456789abcdefghij"  # 35 caracteres

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' https://cdn.discordapp.com data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
EXPECTED_SECURITY_HEADERS = {
    "Content-Security-Policy": EXPECTED_CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
XSRF_META_RE = re.compile(r'<meta\s+name="xsrf-token"\s+content="([^"]+)"')


def local_config(**overrides) -> dict:
    """Configuração válida para desenvolvimento em http://localhost."""
    config = {
        "PANEL_ENABLED": True,
        "PANEL_BASE_URL": "http://localhost:8080",
        "PANEL_OAUTH_CLIENT_ID": "",
        "PANEL_OAUTH_CLIENT_SECRET": OAUTH_SECRET,
        "PANEL_COOKIE_SECRET": COOKIE_SECRET,
        "PANEL_SESSION_TTL_HOURS": 12,
        "PANEL_SESSION_IDLE_MINUTES": 60,
        "PANEL_TRUST_PROXY": False,
        "PANEL_INSECURE_COOKIES": True,
    }
    config.update(overrides)
    return config


def https_config(**overrides) -> dict:
    """Configuração válida para produção (HTTPS, cookies seguros)."""
    return local_config(
        **{"PANEL_BASE_URL": "https://painel.example.com", "PANEL_INSECURE_COOKIES": False, **overrides}
    )


def require(module_name: str):
    """Importa um módulo do projeto; falha o teste (não a coleta) com mensagem clara se não existir."""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        pytest.fail(f"{module_name} ainda não existe ou não importa (RED esperado): {exc}", pytrace=False)


def load_settings(config: dict):
    return require("empiremuse.panel.settings").load_panel_settings(config)


def fake_pool():
    """Dublê mínimo de BotPool: o painel da Etapa 1 não deve tocar no pool."""
    return SimpleNamespace(bots=[], database=None)
