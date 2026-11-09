"""Regras puras dos comandos de owner que executam shell: ambiente mínimo e mascaramento da saída.

``build_shell_env`` devolve só as variáveis de uma allowlist explícita, para que o ``.env`` carregado no processo não
fique visível ao comando. ``collect_secret_values`` reúne os valores de segredo conhecidos (configuração e ambiente) e
``mask_secrets`` os troca por um marcador antes de a saída ou o comando seguirem para o Discord ou para o log.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

SHELL_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "GIT_DIR")
SHELL_ENV_ALLOWLIST_WINDOWS = ("SystemRoot", "COMSPEC")

# Abaixo disso o valor vira falso positivo (um "true" ou um "0" no meio de qualquer saída).
MIN_SECRET_LENGTH = 6
MASK = "***"

# Nomes de variável cujo valor é segredo (ver .claude/rules/empiremuse/security.md, seção 1).
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASS|KEY|MONGO|WEBHOOK|COOKIE|CREDENTIAL|AUTH|REFRESH|LAVALINK_NODE|SPOTIFY_CLIENT|LASTFM",
    re.IGNORECASE,
)


# Identificadores públicos por construção (vão em toda URL de autorização do OAuth2). Lista exata e sensível a maiúsculas: o que
# não está aqui continua segredo pelo nome. O valor só fica de fora da máscara se for um snowflake ASCII (ver ``is_public_id``).
_PUBLIC_ID_NAMES = frozenset({"PANEL_OAUTH_CLIENT_ID"})
_SNOWFLAKE = re.compile(r"[0-9]{17,20}", re.ASCII)


def is_secret_name(name: str) -> bool:
    """Se o NOME de uma variável, chave ou parâmetro é de segredo (o valor nunca deve ir a log nem a relatório)."""
    name = str(name)
    return name not in _PUBLIC_ID_NAMES and bool(_SECRET_NAME.search(name))


def is_public_id(name: str, value: str) -> bool:
    """Se ``name`` é um ID público conhecido E ``value`` tem a forma de um snowflake (senão pode ser um segredo colado ali)."""
    return name in _PUBLIC_ID_NAMES and _SNOWFLAKE.fullmatch(value) is not None


def build_shell_env(environ: Mapping[str, str], *, windows: bool = False) -> dict[str, str]:
    """Cópia de ``environ`` só com as variáveis da allowlist (no Windows os nomes não diferenciam maiúsculas)."""
    allowed = SHELL_ENV_ALLOWLIST + (SHELL_ENV_ALLOWLIST_WINDOWS if windows else ())
    if windows:
        wanted = {name.casefold() for name in allowed}
        return {key: value for key, value in environ.items() if key.casefold() in wanted}
    return {name: environ[name] for name in allowed if name in environ}


def collect_secret_values(*sources: Mapping[str, object]) -> frozenset[str]:
    """Valores textuais de segredo (pelo nome da chave) das fontes dadas, sem os vazios nem os curtos demais."""
    values: set[str] = set()
    for source in sources:
        for key, value in source.items():
            if not isinstance(value, str) or len(value) < MIN_SECRET_LENGTH:
                continue
            name = str(key)
            if _SECRET_NAME.search(name) and not is_public_id(name, value):
                values.add(value)
    return frozenset(values)


def mask_secrets(text: str, secrets: Iterable[str]) -> str:
    """Troca cada valor de ``secrets`` em ``text`` por ``MASK``, do mais longo para o mais curto."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, MASK)
    return text


DISCORD_TOKEN_SHAPE = re.compile(r"[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}")


def mask_shell_output(text: str, secrets: Iterable[str]) -> str:
    """Mascara valores exatos e depois tokens Discord, preservando texto comum."""
    return DISCORD_TOKEN_SHAPE.sub(MASK, mask_secrets(text, secrets))
