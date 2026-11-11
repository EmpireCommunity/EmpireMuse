"""Apoio dos testes da API externa do painel (B1): configuração válida, relógio e dublês sem rede."""

from datetime import UTC, datetime

SERVICE_KEY = "chave-de-servico-0123456789-abcdefghij"
PREVIOUS_KEY = "chave-anterior-0123456789-abcdefghijklm"
SIGNING_SECRET = "chave-de-assinatura-0123456789-abcdefg"
OAUTH_SECRET = "oauth-segredo-0123456789-abcdefghijklm"
COOKIE_SECRET = "cookie-segredo-0123456789-abcdefghijklm"
PANEL_URL = "https://painel.example.com"
WEB_PORT = 6000
API_PORT = 6100
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp()


def external_config(**overrides) -> dict:
    """Configuração externa válida: bind em loopback (painel e bot no mesmo host, sem contêiner)."""
    config = {
        "EXTERNAL_PANEL_URL": PANEL_URL,
        "EXTERNAL_PANEL_API_SECRET": SERVICE_KEY,
        "EXTERNAL_PANEL_SIGNING_SECRET": SIGNING_SECRET,
        "EXTERNAL_PANEL_API_PORT": str(API_PORT),
        "EXTERNAL_PANEL_API_BIND": "127.0.0.1",
        "PANEL_OAUTH_CLIENT_SECRET": OAUTH_SECRET,
    }
    config.update(overrides)
    return config
