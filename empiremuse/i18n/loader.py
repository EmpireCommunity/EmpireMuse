# -*- coding: utf-8 -*-
"""Leitura dos catálogos em disco (item 21, I-9): só ``locales/<locale>/{commands,bot,panel}.json``, sem seguir link simbólico.

Quem escolhe o caminho é o código (``Path(__file__).parent.parent / "locales"``; o ``--root`` existe só na CLI de checagem). O nome do
diretório precisa ser IGUAL a um valor de ``disnake.Locale`` e só os três nomes de arquivo entram. Cada arquivo abre com ``O_NOFOLLOW`` e
``O_NONBLOCK`` (uma FIFO não trava a subida), só vale se for arquivo regular, o ``fstat`` confere o tamanho antes de ler e a leitura
para em ``limite + 1``. A validação é de ``rules/i18n``; aqui não se loga texto de catálogo.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import disnake

from empiremuse.rules import i18n as rules

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "locales"
LOCALE_NAMES = frozenset(locale.value for locale in disnake.Locale)


class I18nError(RuntimeError):
    """A release traz um catálogo obrigatório quebrado (``en-US`` ou ``pt-BR``): a subida falha."""


@dataclass
class LocaleData:
    """Um locale carregado: as entradas aceitas (todos os arquivos juntos), os descartes e o motivo de ter sido desligado."""

    locale: str
    entries: dict[str, str] = field(default_factory=dict)
    dropped: list[tuple[str, str, str]] = field(default_factory=list)  # (arquivo, chave, motivo)
    fatal: list[tuple[str, str]] = field(default_factory=list)  # (arquivo, motivo)
    coverage: float = 0.0
    categories: tuple[str, ...] = ("one", "other")

    @property
    def usable(self) -> bool:
        return not self.fatal


def read_catalog(path: Path) -> bytes | None:
    """Os bytes do arquivo, ou ``None`` se ele não existe ou não é aceito (link simbólico, FIFO, outro tipo). Maior que o limite → ``limite + 1`` bytes."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > rules.MAX_FILE_BYTES:
            return bytes(rules.MAX_FILE_BYTES + 1)
        chunks: list[bytes] = []
        remaining = rules.MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def locale_directories(root: Path) -> list[str]:
    """Os nomes de ``root/*`` que são diretório de verdade (não link) e iguais a um ``disnake.Locale``; o resto é ignorado."""
    try:
        with os.scandir(root) as entries:
            return sorted(
                entry.name for entry in entries if entry.name in LOCALE_NAMES and entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        return []


def plural_categories(locale: str) -> tuple[str, ...]:
    """As categorias de plural CLDR do locale (``other`` sempre). Só um valor do allowlist chega ao Babel (I-10)."""
    if locale not in LOCALE_NAMES:
        return ("one", "other")
    try:
        from babel import Locale

        tags = {str(tag) for tag in Locale.parse(locale, sep="-").plural_form.tags}
    except Exception:  # noqa: BLE001 - sem Babel ou locale desconhecido: o plural simples
        return ("one", "other")
    return tuple(category for category in rules.PLURAL_CATEGORIES if category in tags or category == "other")


def _load_locale(root: Path, locale: str, reference: Mapping[str, Mapping[str, str]]) -> LocaleData:
    data = LocaleData(locale, categories=plural_categories(locale))
    for namespace in rules.NAMESPACES:
        raw = read_catalog(root / locale / f"{namespace}.json")
        if raw is None:
            continue
        report = rules.validate_catalog(locale, namespace, raw, reference[namespace])
        if report.fatal:
            data.fatal.append((namespace, report.fatal))
            continue
        data.dropped.extend((namespace, key, reason) for key, reason in report.dropped)
        data.entries.update(report.entries)
    return data


def load_catalogs(root: Path | None = None) -> dict[str, LocaleData]:
    """Carrega e valida todos os locales de ``root``; o ``en-US`` (referência) primeiro. Nunca levanta; o fatal fica em ``LocaleData``.

    A cobertura de cada locale é a do conjunto dos três arquivos contra o ``en-US``.
    """
    base = Path(root) if root is not None else DEFAULT_ROOT
    names = locale_directories(base)
    result: dict[str, LocaleData] = {}
    reference_entries: dict[str, dict[str, str]] = {}
    reference = _load_locale_reference(base, reference_entries) if rules.REFERENCE_LOCALE in names else None
    if reference is not None:
        result[rules.REFERENCE_LOCALE] = reference
    for locale in names:
        if locale == rules.REFERENCE_LOCALE:
            continue
        data = _load_locale(base, locale, reference_entries)
        if reference is not None and data.usable:
            data.coverage = rules.coverage(data.entries, reference.entries, data.categories)
        result[locale] = data
    if reference is not None:
        reference.coverage = 1.0 if reference.usable else 0.0
    return result


def _load_locale_reference(root: Path, per_namespace: dict[str, dict[str, str]]) -> LocaleData:
    """O ``en-US``: validado sem referência; as entradas de cada arquivo ficam em ``per_namespace`` para os outros locales."""
    data = LocaleData(rules.REFERENCE_LOCALE, categories=plural_categories(rules.REFERENCE_LOCALE))
    for namespace in rules.NAMESPACES:
        raw = read_catalog(root / rules.REFERENCE_LOCALE / f"{namespace}.json")
        per_namespace[namespace] = {}
        if raw is None:
            continue
        report = rules.validate_catalog(rules.REFERENCE_LOCALE, namespace, raw, None)
        if report.fatal:
            data.fatal.append((namespace, report.fatal))
            continue
        data.dropped.extend((namespace, key, reason) for key, reason in report.dropped)
        per_namespace[namespace] = dict(report.entries)
        data.entries.update(report.entries)
    return data


def enabled_locales(loaded: Mapping[str, LocaleData], minimum: float = rules.MIN_COVERAGE) -> frozenset[str]:
    """``{en-US, pt-BR}`` mais os que não têm fatal e chegam à cobertura mínima."""
    ligados = set(rules.REQUIRED_LOCALES)
    ligados.update(name for name, data in loaded.items() if data.usable and data.coverage >= minimum)
    return frozenset(ligados)


def log_report(loaded: Mapping[str, LocaleData], enabled: Collection[str]) -> None:
    """Uma linha por locale com contagens (nunca texto): ligado ou desligado, cobertura, descartes, fatal."""
    for name, data in sorted(loaded.items()):
        state = "ligado" if name in enabled else "desligado"
        logger.info(
            "Idioma %s %s (cobertura %.0f%%, %d descartes)", name, state, data.coverage * 100, len(data.dropped)
        )
        for namespace, reason in data.fatal:
            logger.warning("Idioma %s: arquivo %s recusado (%s)", name, namespace, reason)
        for namespace, key, reason in data.dropped:
            logger.warning("Idioma %s: chave %s.%s descartada (%s)", name, namespace, key, reason)


def required_errors(loaded: Mapping[str, LocaleData]) -> list[str]:
    """Os problemas que impedem a subida: ``en-US``/``pt-BR`` ausente, com fatal ou descarte (release quebrada, I-7)."""
    problems: list[str] = []
    for name in rules.REQUIRED_LOCALES:
        data = loaded.get(name)
        if data is None:
            problems.append(f"{name}: catálogo ausente")
            continue
        problems.extend(f"{name}: {namespace} recusado ({reason})" for namespace, reason in data.fatal)
        problems.extend(f"{name}: {namespace}.{key} descartada ({reason})" for namespace, key, reason in data.dropped)
    return problems
