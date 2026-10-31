"""Falha se uma chave de segredo em deploy/ ou nos *.example* tiver valor literal.

Recusa também URL de webhook do Discord em qualquer chave.
Aceita valor vazio, referência ``${NOME}``, ``${NOME:-}`` ou ``${NOME:?mensagem}`` (sem padrão literal).
Booleano e número só valem nas chaves de ajuste listadas em ``NON_SECRET_SETTINGS``.
"""

import json
import re
import sys
from pathlib import Path

SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASS|KEY|MONGO|WEBHOOK|COOKIE|REFRESH|URI)", re.IGNORECASE)
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?[\"']?([A-Za-z0-9_]+)[\"']?\s*[:=]\s*(.*?)\s*$")
EMPTY_OR_REFERENCE = re.compile(r"""^(["']?)(|\$\{[A-Za-z_][A-Za-z0-9_]*(?::-|:\?[^}]*)?\})\1$""")
BOOLEAN_OR_NUMBER = re.compile(r"""^(["']?)(true|false|-?\d+)\1$""", re.IGNORECASE)
# Chaves que casam com SECRET_KEY pelo nome mas são ajustes, não segredos: só nelas booleano e número são aceitos.
NON_SECRET_SETTINGS = frozenset(
    {
        "PLAYER_SESSIONS_MONGODB",
        "WEBHOOKS_INTENT",
        "MONGO_TIMEOUT",
        "PLAYER_INFO_BACKUP_INTERVAL_MONGO",
        "PANEL_INSECURE_COOKIES",
    }
)
WEBHOOK_URL = re.compile(r"discord(?:app)?\.com/api/webhooks/\S", re.IGNORECASE)
SKIPPED_SUFFIXES = {".md"}
# Exemplo de nós Lavalink públicos de terceiros: as senhas já são públicas por desenho.
PUBLIC_EXAMPLES = {"lavalink.ini.example"}


def candidate_files(root: Path) -> list[Path]:
    deploy = [p for p in (root / "deploy").rglob("*") if p.is_file()]
    examples = [p for p in root.rglob("*.example*") if p.is_file() and ".git" not in p.parts]
    return sorted({p for p in deploy + examples if p.suffix not in SKIPPED_SUFFIXES and p.name not in PUBLIC_EXAMPLES})


def is_harmless(name: str, value: str) -> bool:
    if EMPTY_OR_REFERENCE.match(value):
        return True
    return name in NON_SECRET_SETTINGS and bool(BOOLEAN_OR_NUMBER.match(value))


def normalized(line: str) -> str:
    """Tira o marcador de item de lista (``- ``) e, só nele, as aspas que envolvem o item (``- "TOKEN=x"``)."""
    text = line.strip()
    if not text.startswith("- "):
        return text
    text = text[2:].strip()
    if len(text) > 1 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1]
    return text


def offending_lines(path: Path) -> list[str]:
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if WEBHOOK_URL.search(line):
            found.append(f"{path}:{number}: URL de webhook do Discord literal")
            continue
        if line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.match(normalized(line))
        if not match or not SECRET_KEY.search(match.group(1)):
            continue
        value = re.sub(r"\s+#.*$", "", match.group(2)).strip()
        if not is_harmless(match.group(1), value):
            found.append(f"{path}:{number}: {match.group(1)} tem valor literal")
    return found


def offending_egg_variables(path: Path) -> list[str]:
    """Egg do Pterodactyl: cada ``env_variable`` de segredo precisa de ``default_value`` inofensivo."""
    found = []
    for variable in json.loads(path.read_text(encoding="utf-8")).get("variables", []):
        name = variable.get("env_variable", "")
        default = variable.get("default_value")
        if not SECRET_KEY.search(name) or default is None:
            continue
        if not is_harmless(name, str(default).strip()):
            found.append(f"{path}: {name} tem valor literal")
    return found


def main() -> int:
    problems = [
        msg
        for path in candidate_files(Path("."))
        for msg in (offending_egg_variables(path) if path.suffix == ".json" else offending_lines(path))
    ]
    for msg in problems:
        sys.stderr.write(f"{msg}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
