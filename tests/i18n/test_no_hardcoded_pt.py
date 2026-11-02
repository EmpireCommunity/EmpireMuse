"""Guarda contra texto em português fixo no código (decisão do dono): nos módulos já limpos, todo texto visível ao usuário
vem do catálogo pelo tradutor da guilda. Um literal com português fora de docstring, log, chave de catálogo ou ``custom_id``
é regressão. Para pôr um módulo sob a guarda, acrescente o caminho em ``CLEAN_PATHS`` depois de limpá-lo."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from empiremuse.rules.i18n import KEY_PATTERN

pytestmark = pytest.mark.critical

ROOT = Path(__file__).resolve().parents[2]

# Módulos sem texto fixo em português (um por linha; pastas valem para todos os ``.py`` dentro).
CLEAN_PATHS = (
    "empiremuse/utils/music/interactions",
    "empiremuse/utils/music/skins",
    "empiremuse/music_cog/playback_controls.py",
)

# Acentos ou palavras que só aparecem em português (nada que também seja inglês, como "link", "volume" ou "skin").
PORTUGUESE = re.compile(
    r"[áéíóúâêôãõçÁÉÍÓÚÂÊÔÃÕÇ]"
    r"|\b(você|não|canal|servidor|música|músicas|permissão|erro|precisa|usar|comando|fila|deve|ainda|favorito|favoritos|"
    r"salvo|salva|adicionar|remover|escolha|digite|cole|nome|opção|opções|tocando|pausada|pausado|pular|anterior|"
    r"próxima|próximo|cancelar|confirmar|aguarde|limite|inválido|inválida|apenas|somente|tente|novamente|tocar|mover|"
    r"recarregar|recomendada|misturar|readicionar|voltar|ativar|desativar|editar|criar|exportar|importar|texto|campo|"
    r"rodapé|autor|miniatura|dica|aviso|página|selecione|informe|clique|arquivo|lista|quantidade|sucesso)\b",
    re.IGNORECASE,
)

# Chamadas cujo primeiro argumento é uma chave de catálogo ou um texto que não chega ao usuário.
KEY_CALLS = frozenset({"tr", "localized", "app_emoji", "emoji", "get_cog", "get_slash_command", "getattr", "get"})
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
# Argumentos nomeados que nunca são texto para o usuário.
NON_TEXT_KEYWORDS = frozenset({"custom_id", "filename", "name_", "key", "db_name", "icon", "url", "emoji"})


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _excluded_ids(tree: ast.AST) -> set[int]:
    """Constantes que não são texto para o usuário: chaves de ``tr``/``localized``, mensagens de log e argumentos técnicos."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        is_log = (
            isinstance(func, ast.Attribute)
            and name in LOG_METHODS
            and isinstance(func.value, ast.Name)
            and (func.value.id in ("logger", "log", "logging"))
        )
        if name in KEY_CALLS or is_log:
            for arg in node.args:
                ids.update(id(n) for n in ast.walk(arg) if isinstance(n, ast.Constant))
        for keyword in node.keywords:
            if keyword.arg in NON_TEXT_KEYWORDS:
                ids.update(id(n) for n in ast.walk(keyword.value) if isinstance(n, ast.Constant))
    return ids


def hardcoded_portuguese(path: Path) -> list[str]:
    """``arquivo:linha: trecho`` de cada literal com português que chega ao usuário."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_ids(tree) | _excluded_ids(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in skip:
            continue
        text = node.value
        if len(text) < 3 or KEY_PATTERN.fullmatch(text) or not PORTUGUESE.search(text):
            continue
        snippet = text.replace("\n", "\\n")[:60]
        shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
        found.append(f"{shown}:{node.lineno}: {snippet}")
    return found


def clean_files() -> list[Path]:
    files: list[Path] = []
    for entry in CLEAN_PATHS:
        path = ROOT / entry
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return files


@pytest.mark.parametrize("path", clean_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_portuguese_text_is_hardcoded_in_a_clean_module(path):
    offenders = hardcoded_portuguese(path)
    assert not offenders, "texto em português fixo (use o catálogo pelo tradutor da guilda):\n" + "\n".join(offenders)


def test_the_detector_catches_a_message_and_ignores_keys_logs_and_docstrings(tmp_path):
    sample = tmp_path / "amostra.py"
    sample.write_text(
        '"""Docstring em português não conta."""\n'
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(tr, inter):\n"
        '    logger.warning("Skin não salva: %s", 1)\n'
        '    a = tr("fav.menu.cleared")\n'
        '    b = disnake.ui.Button(label=tr("fav.add.label"), custom_id="fav_adicionar_botão")\n'
        '    raise GenericError("**Você não possui favoritos!**")\n',
        encoding="utf-8",
    )
    offenders = hardcoded_portuguese(sample)
    assert len(offenders) == 1 and offenders[0].endswith("**Você não possui favoritos!**")
