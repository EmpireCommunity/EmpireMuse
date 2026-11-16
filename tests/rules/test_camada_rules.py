"""``empiremuse/rules/`` só usa a biblioteca padrão e o próprio pacote (docs/architecture.md §7), inclusive em import dentro de função."""

import ast
import pathlib
import sys

import pytest

RULES = pathlib.Path(__file__).resolve().parents[2] / "empiremuse" / "rules"


RULES_PACKAGE = "empiremuse.rules"


def _module_of(path: pathlib.Path) -> str:
    """O nome do módulo de um arquivo de ``rules`` (``__init__.py`` é o próprio pacote)."""
    parts = path.relative_to(RULES).with_suffix("").parts
    return ".".join((RULES_PACKAGE, *(parts[:-1] if parts[-1] == "__init__" else parts)))


def _targets(node: ast.ImportFrom, package: list[str]) -> list[str]:
    """Os módulos de um ``from ... import``; o relativo (``level`` > 0) é resolvido contra o pacote do arquivo, como o Python faz."""
    if node.level == 0:
        return [node.module or ""]
    base = package[: len(package) - (node.level - 1)] if node.level - 1 <= len(package) else []
    if node.module:
        return [".".join([*base, node.module])]
    return [".".join([*base, alias.name]) for alias in node.names]  # ``from .. import x``: ``x`` pode ser um módulo


def _is_allowed(name: str) -> bool:
    return (
        name.split(".")[0] in sys.stdlib_module_names or name == RULES_PACKAGE or name.startswith(RULES_PACKAGE + ".")
    )


def _outside_imports_of(source: str, module: str, *, is_package: bool = False) -> list[str]:
    parts = module.split(".")
    package = parts if is_package or module == RULES_PACKAGE else parts[:-1]
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = _targets(node, package)
        else:
            continue
        found += [n for n in names if not _is_allowed(n)]
    return found


def _outside_imports(path: pathlib.Path) -> list[str]:
    return _outside_imports_of(
        path.read_text(encoding="utf-8"), _module_of(path), is_package=path.name == "__init__.py"
    )


@pytest.mark.regression
def test_rules_imports_only_the_standard_library_and_itself():
    """CodeRabbit #200: ``radio_geo.guild_country`` importava o Babel dentro da função; um teste só do nível de módulo não via."""
    offenders = {
        str(p.relative_to(RULES)): names for p in sorted(RULES.rglob("*.py")) if (names := _outside_imports(p))
    }

    assert offenders == {}


@pytest.mark.regression
@pytest.mark.parametrize(
    ("module", "source", "outside"),
    [
        ("empiremuse.rules.text", "from ..utils import x", ["empiremuse.utils"]),
        ("empiremuse.rules.text", "from .. import utils", ["empiremuse.utils"]),
        ("empiremuse.rules.text", "from ... import x", ["x"]),
        ("empiremuse.rules.text", "from .usage import escape_markdown\nfrom . import radio", []),
        ("empiremuse.rules", "from .text import external_text", []),
        ("empiremuse.rules", "from . import text", []),
        ("empiremuse.rules.text", "import empiremuse.rules_extra", ["empiremuse.rules_extra"]),
    ],
)
def test_relative_imports_are_resolved_against_the_package_of_the_file(module, source, outside):
    """CodeRabbit PR 257: ``from ..utils import x`` saía da camada sem o teste ver (o import relativo era pulado)."""
    assert _outside_imports_of(source, module) == outside
