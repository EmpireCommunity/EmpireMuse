"""``empiremuse/rules/`` só usa a biblioteca padrão e o próprio pacote (docs/architecture.md §7), inclusive em import dentro de função."""

import ast
import pathlib
import sys

import pytest

RULES = pathlib.Path(__file__).resolve().parents[2] / "empiremuse" / "rules"


def _outside_imports(path: pathlib.Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        found += [
            n for n in names if n.split(".")[0] not in sys.stdlib_module_names and not n.startswith("empiremuse.rules")
        ]
    return found


@pytest.mark.regression
def test_rules_imports_only_the_standard_library_and_itself():
    """CodeRabbit #200: ``radio_geo.guild_country`` importava o Babel dentro da função; um teste só do nível de módulo não via."""
    offenders = {
        str(p.relative_to(RULES)): names for p in sorted(RULES.rglob("*.py")) if (names := _outside_imports(p))
    }

    assert offenders == {}
