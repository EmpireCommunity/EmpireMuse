"""A guarda dos nomes antigos (contrato-nomes-radios, I5): o bot tem só duas funções, Jukebox e Radios.

Varre o texto de ``empiremuse/`` (menos ``panel/``, que é do painel e tem a guarda dele) e dos catálogos ``bot``, ``commands`` e ``player``. Os termos antigos não
voltam: "24/7" (modo), "nonstop"/"interrupto", "song request" e "canal de pedidos" como nome da função, "Jukebox estrita" e o modo estrito. Entradas antigas do CHANGELOG
são histórico e ficam fora do alcance; a exceção da lista abaixo é a permitida pelo contrato (o alias ``247`` e a migração).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "empiremuse"
SKIP_DIRS = {"panel", "__pycache__", "wavelink"}
ALLOWED = {
    "utils/migrations/radios_release.py",  # lê as chaves antigas para migrar
    "utils/radio/prefix_commands.py",  # o alias de prefixo 247 do grupo radio
}
LOCALES = ("bot.json", "commands.json", "player.json")

TERMS = {
    "24/7": re.compile(r"24\s*/\s*7"),
    "nonstop": re.compile(r"non[-_ ]?stop", re.IGNORECASE),
    "interrupto": re.compile(r"\binterrupto\b|\bmodo interrupt", re.IGNORECASE),
    "song request": re.compile(r"song[-_ ]?request|songrequest", re.IGNORECASE),
    "canal de pedidos": re.compile(r"canal de pedidos|canal de pedir|request[_ ]channel", re.IGNORECASE),
    "estrita": re.compile(
        r"jukebox estrit|modo[ _]estrit|request_channel_strict|strict_action|StrictAction|\bstrict[_ ]mode\b",
        re.IGNORECASE,
    ),
}
# A chave de ambiente é API pública do deploy (um ``.env`` existente continua valendo): o nome dela não muda nesta release.
IGNORED_LINES = (re.compile(r"ENABLE_SONGREQUEST_MENTION"),)


def sources():
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in SKIP_DIRS or relative.as_posix() in ALLOWED:
            continue
        yield relative.as_posix(), path
    for locale in sorted((ROOT / "locales").iterdir()):
        for name in LOCALES:
            if (locale / name).is_file():
                yield f"locales/{locale.name}/{name}", locale / name


def offenders():
    found = []
    for label, path in sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(pattern.search(line) for pattern in IGNORED_LINES):
                continue
            for term, pattern in TERMS.items():
                if pattern.search(line):
                    found.append(f"{label}:{number} [{term}] {line.strip()[:90]}")
    return found


def test_the_old_names_do_not_come_back():
    assert not offenders(), "\n".join(offenders())


@pytest.mark.parametrize(
    "text", ["restrict", "restrictmode", "restrito", "resolve(strict=True)", "InterruptedException", "24 horas"]
)
def test_the_guard_has_no_false_positives(text):
    assert not any(pattern.search(text) for pattern in TERMS.values())


@pytest.mark.parametrize(
    "text", ["Rádio 24/7", "nonstop", "song_request", "canal de pedidos", "request_channel_strict", "modo estrito"]
)
def test_the_guard_catches_the_old_names(text):
    assert any(pattern.search(text) for pattern in TERMS.values())
