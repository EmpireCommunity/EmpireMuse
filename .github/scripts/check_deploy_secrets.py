"""Falha se uma chave de segredo em deploy/ ou nos *.example* tiver valor literal.

Aceita valor vazio, referência ``${...}``, booleano e número (ex.: ``MONGO_TIMEOUT: "30"``).
"""

import re
import sys
from pathlib import Path

SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASS|KEY|MONGO|WEBHOOK|COOKIE|REFRESH|URI)", re.IGNORECASE)
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z0-9_]+)\s*[:=]\s*(.*?)\s*$")
HARMLESS_VALUE = re.compile(r"""^(["']?)(|\$\{[^}]*\}|true|false|-?\d+)\1$""", re.IGNORECASE)
SKIPPED_SUFFIXES = {".json", ".md"}
# Exemplo de nós Lavalink públicos de terceiros: as senhas já são públicas por desenho.
PUBLIC_EXAMPLES = {"lavalink.ini.example"}


def candidate_files(root: Path) -> list[Path]:
    deploy = [p for p in (root / "deploy").rglob("*") if p.is_file()]
    examples = [p for p in root.rglob("*.example*") if p.is_file() and ".git" not in p.parts]
    return sorted({p for p in deploy + examples if p.suffix not in SKIPPED_SUFFIXES and p.name not in PUBLIC_EXAMPLES})


def offending_lines(path: Path) -> list[str]:
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if not match or not SECRET_KEY.search(match.group(1)):
            continue
        value = re.sub(r"\s+#.*$", "", match.group(2)).strip()
        if not HARMLESS_VALUE.match(value):
            found.append(f"{path}:{number}: {match.group(1)} tem valor literal")
    return found


def main() -> int:
    problems = [msg for path in candidate_files(Path(".")) for msg in offending_lines(path)]
    for msg in problems:
        sys.stderr.write(f"{msg}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
