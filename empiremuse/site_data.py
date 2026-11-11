"""Exporta os dados públicos de comandos do bot para o site (``site-data.json``; contrato T-456 §12 B7, ``painel-p1-arquitetura.md`` §5).

Lê **só o que o código já declara**, sem login no Discord e sem rede: os comandos de barra e de prefixo dos cogs (as classes, sem
instanciar nenhum bot) e o catálogo de textos (``cmd.<id>.{name,description,aliases}``), em pt-BR e en-US. Nunca entra comando de dono,
comando oculto, segredo nem id interno: o filtro é :func:`is_public` e a saída passa por um teste que falha se algo vazar.

A saída é determinística (sem data nem ordem solta): o mesmo código gera os mesmos bytes e o mesmo SHA-256. O formato está em
``docs/api/site-data.schema.json`` (JSON Schema 2020-12) e é descrito em ``docs/api/site-data.md``.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import re
from collections.abc import Iterable, Iterator
from typing import Any, Optional

import disnake
from disnake.ext import commands

from empiremuse.cogs.help_cog import catalog_id
from empiremuse.i18n.service import I18n
from empiremuse.rules.i18n import REFERENCE_LOCALE
from empiremuse.version import image_version

SCHEMA_VERSION = 1
LOCALES = ("pt-BR", "en-US")
COG_PACKAGES = ("empiremuse.cogs", "empiremuse.music_cog")
VERSION_PATTERN = re.compile(r"^(dev|[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?)$")
# Os cogs prefixam a descrição de barra com a própria categoria: ``[🔧 Settings] | Texto``.
CATEGORY_PREFIX = re.compile(r"^\[(?P<emoji>\S+) (?P<label>[^\]]+)\] \| (?P<text>.*)$", re.DOTALL)
OWNER_CHECK_MARKERS = (
    "admin_owner_only",
    "is_owner",
    "owner_check",
)  # nome (``__qualname__``) das verificações de dono


class SiteDataError(Exception):
    """A saída não pode ser gerada com segurança (por exemplo, versão inválida)."""


def is_public(command: Any) -> bool:
    """Falso para comando oculto ou com verificação de dono: nunca vai ao site."""
    if getattr(command, "hidden", False):
        return False
    checks = getattr(command, "checks", ())
    return not any(marker in getattr(check, "__qualname__", "") for check in checks for marker in OWNER_CHECK_MARKERS)


def _cog_classes() -> Iterator[type[commands.Cog]]:
    for package in COG_PACKAGES:
        root = importlib.import_module(package)
        for info in sorted(pkgutil.iter_modules(root.__path__), key=lambda item: item.name):
            module = importlib.import_module(f"{package}.{info.name}")
            for _, cls in inspect.getmembers(module, inspect.isclass):  # ``getmembers`` já ordena por nome
                if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                    yield cls


def _permission_names(permissions: Optional[Any]) -> list[str]:
    """Nomes (``manage_guild``…) das permissões ligadas, em ordem alfabética; ``None`` ou vazio = lista vazia."""
    if permissions is None:
        return []
    return sorted(name for name, enabled in disnake.Permissions(permissions.value) if enabled)


def _prefix_permissions(command: commands.Command) -> list[str]:
    """As permissões de servidor que ``has_permissions``/``has_guild_permissions`` exigem (lidas do fecho da verificação)."""
    found: set[str] = set()
    for check in command.checks:
        if "has_guild_permissions" in check.__qualname__ or "has_permissions" in check.__qualname__:
            perms = inspect.getclosurevars(check).nonlocals.get("perms", {})
            found.update(name for name, wanted in perms.items() if wanted)
    return sorted(found)


def _texts(entries: dict[str, dict[str, str]], key: str, fallback: str = "") -> dict[str, str]:
    return {locale: entries[locale].get(key) or fallback for locale in LOCALES}


def _localized(base: str, localizations: Any, locale: str) -> str:
    """O texto no idioma: o en-US é o próprio texto-base do Discord; os demais vêm das localizações (sem elas, o base)."""
    if locale == REFERENCE_LOCALE:
        return base
    data = getattr(localizations, "data", None) or {}
    return data.get(locale) or base


def _by_locale(base: str, localizations: Any) -> dict[str, str]:
    return {loc: _localized(base, localizations, loc) for loc in LOCALES}


def _split_category(descriptions: dict[str, str]) -> tuple[dict[str, str], Optional[dict[str, Any]]]:
    """Tira o ``[emoji rótulo] | `` da descrição: devolve as descrições limpas e a categoria (se todas as línguas a trazem)."""
    matches = {locale: CATEGORY_PREFIX.match(text) for locale, text in descriptions.items()}
    if not all(matches.values()):
        return descriptions, None
    first = matches[LOCALES[0]]
    assert first is not None
    labels = {loc: m.group("label") for loc, m in matches.items() if m}
    return {loc: m.group("text") for loc, m in matches.items() if m}, {"emoji": first.group("emoji"), "label": labels}


# ---------------------------------------------------------------------------- barra


def _option(option: disnake.Option) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": _by_locale(option.name, option.name_localizations),
        "description": _by_locale(option.description, option.description_localizations),
        "type": option.type.name,
        "required": bool(option.required),
    }
    if option.choices:
        entry["choices"] = [
            {"value": choice.value, "name": _by_locale(choice.name, choice.name_localizations)}
            for choice in option.choices
        ]
    return entry


def _slash_node(path: list[str], source: Any) -> dict[str, Any]:
    """Um comando, grupo ou subcomando de barra (``source`` tem ``name``, ``description``, ``options`` e as localizações)."""
    nested = (disnake.OptionType.sub_command, disnake.OptionType.sub_command_group)
    leaves = [o for o in source.options if o.type in nested]
    description, category = _split_category(_by_locale(source.description, source.description_localizations))
    node: dict[str, Any] = {
        "id": ".".join(path),
        "path": path,
        "name": _by_locale(source.name, source.name_localizations),
        "description": description,
        "options": [_option(o) for o in source.options if o.type not in nested],
    }
    if category is not None:
        node["_category"] = category
    if leaves:
        node["subcommands"] = [_slash_node([*path, sub.name], sub) for sub in leaves]
    return node


def slash_commands(provider: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for cls in _cog_classes():
        for command in getattr(cls, "__cog_app_commands__", []):
            if not isinstance(command, commands.InvokableSlashCommand) or not is_public(command):
                continue
            body = command.body
            body.localize(provider)
            node = _slash_node([body.name], body)
            found.append(
                {
                    "id": node.pop("id"),
                    "kind": "slash",
                    **node,
                    "permissions": _permission_names(body.default_member_permissions),
                    "_cog": cls.__cog_name__,  # só para a categoria; o nome do cog (classe Python) nunca sai
                }
            )
    return sorted(found, key=lambda item: item["id"])


# ---------------------------------------------------------------------------- prefixo


def _prefix_node(command: commands.Command, entries: dict[str, dict[str, str]], cog: str) -> Optional[dict[str, Any]]:
    if not is_public(command):
        return None
    base = f"cmd.{catalog_id(command)}"
    node: dict[str, Any] = {
        "id": catalog_id(command),
        "kind": "prefix",
        "path": command.qualified_name.split(" "),
        "name": _texts(entries, f"{base}.name", command.name),
        "description": _texts(entries, f"{base}.description", command.description or ""),
        "aliases": {
            loc: [a.strip() for a in text.split(",") if a.strip()] if text else list(command.aliases)
            for loc, text in _texts(entries, f"{base}.aliases").items()
        },
        "permissions": _prefix_permissions(command),
        "_cog": cog,
    }
    subs = [s for s in (_prefix_node(sub, entries, cog) for sub in getattr(command, "commands", ())) if s is not None]
    if subs:
        node["subcommands"] = sorted(subs, key=lambda item: item["id"])
    return node


def prefix_commands(entries: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    found = []
    for cls in _cog_classes():
        for command in getattr(cls, "__cog_commands__", []):
            if command.parent is None:
                node = _prefix_node(command, entries, cls.__cog_name__)
                if node is not None:
                    found.append(node)
    return sorted(found, key=lambda item: item["id"])


# ---------------------------------------------------------------------------- montagem


def _strip_private(node: dict[str, Any]) -> dict[str, Any]:
    """Tira as chaves de trabalho (``_cog``, ``_category``) do comando e, recursivamente, dos subcomandos."""
    item = {key: value for key, value in node.items() if not key.startswith("_")}
    if "subcommands" in item:
        item["subcommands"] = [_strip_private(sub) for sub in item["subcommands"]]
    return item


def _with_category(nodes: Iterable[dict[str, Any]], labels: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Dá a cada comando a categoria que a barra do mesmo cog declara (o prefixo a herda; sem barra no cog, fica sem categoria)."""
    out = []
    for node in nodes:
        item = _strip_private(node)
        category = labels.get(node["_cog"])
        if category is not None:
            item["category"] = category
        out.append(item)
    return out


def build_site_data(bot_version: Optional[str] = None) -> dict[str, Any]:
    """O dicionário de ``site-data.json``. ``bot_version`` padrão = versão da imagem, ou ``dev`` fora dela."""
    version = bot_version if bot_version is not None else str(image_version() or "dev")
    if not VERSION_PATTERN.fullmatch(version):
        raise SiteDataError("bot_version fora do formato X.Y.Z[-rc.N] ou dev")
    i18n = I18n(only=LOCALES)  # só os catálogos en-US e pt-BR do repositório: nenhum idioma baixado do Crowdin é aberto
    entries = {locale: i18n.entries(locale) for locale in LOCALES}
    slash = slash_commands(i18n.provider)
    labels = {node["_cog"]: node["_category"] for node in slash if "_category" in node}
    return {
        "schema_version": SCHEMA_VERSION,
        "bot_version": version,
        "locales": list(LOCALES),
        "slash_commands": _with_category(slash, labels),
        "prefix_commands": _with_category(prefix_commands(entries), labels),
    }


def render(data: dict[str, Any]) -> str:
    """JSON estável (UTF-8, indentado, uma quebra de linha no fim): o mesmo código dá os mesmos bytes."""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Gera o site-data.json (comandos públicos do bot) sem rede e sem login no Discord."
    )
    parser.add_argument("--bot-version", help="X.Y.Z[-rc.N] ou dev (padrão: a versão da imagem, ou dev)")
    parser.add_argument("--out", default="-", help="arquivo de saída (padrão: a saída padrão)")
    args = parser.parse_args(argv)
    try:
        text = render(build_site_data(args.bot_version))
    except SiteDataError as error:
        sys.stderr.write(f"erro: {error}\n")
        return 2
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
