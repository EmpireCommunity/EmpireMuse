# -*- coding: utf-8 -*-
"""``python -m empiremuse.i18n.check [--root DIR]``: o validador da subida em todos os locales (item 21).

Sai com código diferente de zero se houver arquivo recusado (fatal) ou entrada descartada em ``en-US`` ou ``pt-BR``, se eles ficarem abaixo
de 100% ou se o código usar uma chave (``tr("…")``/``localized("…")`` com texto literal) que não existe no ``en-US``. Nos OUTROS locales (as
traduções da comunidade, que chegam pelo Crowdin) nada disso é erro: arquivo recusado, entrada descartada e cobertura abaixo do mínimo viram AVISO
(saída 0), e valor VAZIO (chave ainda não traduzida; o Crowdin pode exportá-la assim) é ignorado em silêncio. O bot já descarta a entrada e usa o
``en-US`` em execução, e um idioma só liga com a cobertura mínima.
A conferência código ↔ catálogo só faz sentido contra o catálogo REAL: com ``--root`` ela só roda se ``--code`` apontar o código a varrer.
Roda o MESMO validador do ``BotPool``; o CI não é fronteira (I-8), então a saída leva só chaves e motivos, nunca texto de catálogo.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from empiremuse.i18n import loader
from empiremuse.rules import i18n as rules

PACKAGE_DIR = Path(__file__).resolve().parent.parent
_CALLEES = frozenset({"tr", "localized"})


def used_keys(package_dir: Path = PACKAGE_DIR) -> dict[str, list[str]]:
    """As chaves literais passadas a ``tr(...)``/``localized(...)`` no código, com os arquivos que as usam."""
    found: dict[str, list[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except OSError, SyntaxError, UnicodeDecodeError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            first = node.args[0]
            if name in _CALLEES and isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, []).append(str(path.relative_to(package_dir)))
    return found


def missing_keys(used: Iterable[str], reference: dict[str, str]) -> list[str]:
    """As chaves usadas que não existem no ``en-US`` (uma chave de plural existe se houver o ``_other``)."""
    return sorted(key for key in used if key not in reference and f"{key}_other" not in reference)


def report(loaded: dict[str, loader.LocaleData]) -> tuple[list[str], list[str]]:
    """``(erros, avisos)`` dos catálogos. ``en-US`` e ``pt-BR``: arquivo recusado, ausente e entrada descartada são erro. Os outros locales: aviso,
    e valor vazio (``rules.EMPTY``, chave ainda não traduzida) nem aviso é."""
    problems: list[str] = []
    warnings: list[str] = []
    for name, data in sorted(loaded.items()):
        strict = name in rules.REQUIRED_LOCALES
        found = problems if strict else warnings
        found.extend(f"{name}: arquivo {namespace} recusado ({reason})" for namespace, reason in data.fatal)
        found.extend(
            f"{name}: arquivo {namespace} ausente ou recusado"
            for namespace in data.missing
            if namespace in loader.REQUIRED_FILES and strict
        )
        found.extend(
            f"{name}: {namespace}.{rules.loggable_key(key)} descartada ({reason})"
            for namespace, key, reason in data.dropped
            if strict or reason != rules.EMPTY
        )
    return problems, warnings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m empiremuse.i18n.check", description=__doc__)
    parser.add_argument("--root", type=Path, default=None, help="pasta com os locales (padrão: empiremuse/locales)")
    parser.add_argument(
        "--code",
        type=Path,
        default=None,
        help="pasta do código varrida atrás de chaves usadas (padrão: o pacote, e só sem --root)",
    )
    args = parser.parse_args(argv)

    loaded = loader.load_catalogs(args.root)
    problems, warnings = report(loaded)
    for name in rules.REQUIRED_LOCALES:
        data = loaded.get(name)
        if data is None:
            problems.append(f"{name}: catálogo ausente")
        elif data.coverage < 1.0:
            problems.append(f"{name}: cobertura abaixo de 100%")
    for name, data in sorted(loaded.items()):
        sys.stdout.write(
            f"{name}: cobertura {data.coverage * 100:.1f}% ({len(data.entries)} chaves, {len(data.dropped)} descartes)\n"
        )
        if name not in rules.REQUIRED_LOCALES and data.usable and data.coverage < rules.MIN_COVERAGE:
            warnings.append(f"{name}: cobertura abaixo de {rules.MIN_COVERAGE * 100:.0f}% (idioma desligado no bot)")
    reference = loaded.get(rules.REFERENCE_LOCALE)
    code_dir = args.code if args.code is not None else PACKAGE_DIR if args.root is None else None
    if reference is not None and code_dir is not None:
        for key in missing_keys(used_keys(code_dir), reference.entries):
            problems.append(f"chave usada no código e ausente no {rules.REFERENCE_LOCALE}: {key}")
    for warning in warnings:
        sys.stderr.write(f"AVISO {warning}\n")
    for problem in problems:
        sys.stderr.write(f"ERRO {problem}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
