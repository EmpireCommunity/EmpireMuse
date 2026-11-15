"""Valida um ``site-data.json`` contra ``docs/api/site-data.schema.json`` e confere o ``bot_version``.

Usado no CI de PR (job ``site-data-image``), para uma falha determinística do exportador aparecer ANTES da tag (no release uma falha
depois de a imagem ser publicada obriga a rodar de novo o job; ver ``docs/api/site-data.md``). Uso:
``python scripts/validate_site_data.py site-data.json --bot-version 0.0.0-rc.0``. Sai com 1 e diz só o caminho do campo que falhou.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "api" / "site-data.schema.json"
MAX_REPORTED = 5


def problems(data: object, expected_version: str | None) -> list[str]:
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    found = [
        f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.validator}"
        for error in validator.iter_errors(data)
    ]
    if isinstance(data, dict) and expected_version is not None and data.get("bot_version") != expected_version:
        found.append(f"/bot_version: diferente de {expected_version}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Valida o site-data.json contra o schema publicado.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--bot-version", default=None)
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.file.read_text(encoding="utf-8"))
    except OSError, ValueError:
        sys.stderr.write(f"erro: {args.file} não é um JSON legível\n")
        return 1
    found = problems(data, args.bot_version)
    if found:
        sys.stderr.write("site-data.json inválido:\n" + "\n".join(found[:MAX_REPORTED]) + "\n")
        return 1
    sys.stdout.write(f"ok: {args.file} bate com o schema\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
