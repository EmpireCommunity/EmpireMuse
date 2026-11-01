# -*- coding: utf-8 -*-
"""``pool.i18n``: os catálogos carregados, o resolvedor de idioma do servidor, o ``Translator`` de cada locale e o provider do disnake.

Um objeto só por processo (``get_i18n()``): os comandos são declarados na importação dos módulos, antes de existir o ``BotPool``, e já
precisam do texto base (``localized``). O ``BotPool.setup`` chama ``load()`` (estrito: ``en-US``/``pt-BR`` quebrado faz a subida
falhar) antes dos bots. O provider devolve só descrições de comando, nomes de opção e nomes de choice dos locales ligados, nunca nome de
comando nem o ``value`` de uma choice (I-6).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import disnake
from disnake.i18n import LocalizationProtocol

from empiremuse.i18n import loader
from empiremuse.i18n.loader import I18nError, LocaleData
from empiremuse.rules import i18n as rules

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

PROVIDER_KINDS = ("description", "choice", "name")


class CatalogProvider(LocalizationProtocol):
    """``LocalizationProtocol`` do disnake sobre os catálogos: ``get(key)`` -> ``{locale: texto}`` dos locales ligados (sem o ``en-US``)."""

    def __init__(self, i18n: I18n) -> None:
        self._i18n = i18n

    def get(self, key: str) -> dict[str, str] | None:
        kind = rules.kind_of(key)
        if (
            kind not in PROVIDER_KINDS
            or not key.startswith("cmd.")
            or (kind == "name" and not rules.is_option_name(key))
        ):
            return None
        found: dict[str, str] = {}
        for locale in sorted(self._i18n.enabled):
            if locale == rules.REFERENCE_LOCALE:
                continue
            text = self._i18n.command_text(locale, key)
            if text is not None:
                found[locale] = text
        return found or None

    def load(self, path: Any) -> None:  # as fontes são só as do pool
        raise NotImplementedError

    def reload(self) -> None:
        return None


class I18n:
    """Os catálogos e o que se faz com eles. ``load()`` é idempotente; o resto só lê o que já foi carregado."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else loader.DEFAULT_ROOT
        self.pool: BotPool | None = None
        self.provider = CatalogProvider(self)
        self._loaded: dict[str, LocaleData] = {}
        self._enabled: frozenset[str] = frozenset(rules.REQUIRED_LOCALES)
        self._translators: dict[str, rules.Translator] = {}
        self._warned: set[tuple[str, str]] = set()
        self._groups: dict[
            str, str
        ] = {}  # chave de descrição de comando -> grupo da legenda (registrado na declaração)
        self._is_loaded = False

    # ------------------------------------------------------------------ carga

    def load(self, *, force: bool = False) -> dict[str, LocaleData]:
        """Lê e valida ``locales/``. ``en-US``/``pt-BR`` quebrado levanta ``I18nError``; outro locale inválido só fica desligado."""
        if self._is_loaded and not force:
            return self._loaded
        loaded = loader.load_catalogs(self.root)
        problems = loader.required_errors(loaded)
        if problems:
            for problem in problems:
                logger.error("Catálogo obrigatório inválido: %s", problem)
            raise I18nError("catálogo obrigatório inválido")
        self._loaded = loaded
        self._enabled = loader.enabled_locales(loaded)
        self._translators = {}
        self._is_loaded = True
        loader.log_report(loaded, self._enabled)
        return loaded

    def _ensure(self) -> None:
        if not self._is_loaded:
            self.load()

    @property
    def enabled(self) -> frozenset[str]:
        self._ensure()
        return self._enabled

    def entries(self, locale: str) -> Mapping[str, str]:
        self._ensure()
        data = self._loaded.get(locale)
        return data.entries if data is not None and locale in self._enabled else {}

    @property
    def reference(self) -> Mapping[str, str]:
        self._ensure()
        return self._loaded[rules.REFERENCE_LOCALE].entries

    # ------------------------------------------------------------------ tradutor e resolução

    def _on_missing(self, locale: str, key: str) -> None:
        marker = (locale, key)
        if marker not in self._warned:
            self._warned.add(marker)
            logger.warning("Texto sem tradução [%s] %s", locale, key)

    def for_locale(self, locale: str | None = None) -> rules.Translator:
        """O ``Translator`` do locale (um locale desligado ou ``None`` usa o idioma de reserva)."""
        self._ensure()
        name = locale if locale in self._enabled else rules.FALLBACK_LOCALE
        translator = self._translators.get(name)
        if translator is None:
            translator = rules.Translator(
                name,
                self.entries(name),
                self.reference,
                plural=self._plural(name),
                on_missing=self._on_missing,
            )
            self._translators[name] = translator
        return translator

    def _plural(self, locale: str):
        categories = self._loaded[locale].categories if locale in self._loaded else ("one", "other")
        if categories == ("one", "other"):
            return None
        try:
            from babel import Locale

            rule = Locale.parse(locale, sep="-").plural_form
            return lambda count: str(rule(count))
        except Exception:  # noqa: BLE001 - Babel ausente: plural simples
            return None

    def resolve(self, saved: str | None, preferred: str | None) -> str:
        """O locale que vale: ``saved`` ligado → ``preferred`` ligado → irmão → reserva (``saved`` desligado continua gravado, só é pulado)."""
        return rules.resolve_locale(_locale_value(saved), _locale_value(preferred), self.enabled, rules.FALLBACK_LOCALE)

    async def language_of(self, guild_id: int) -> str | None:
        """``settings.language`` do servidor (``None`` = automático); qualquer falha do store também é ``None``."""
        pool = self.pool
        if pool is None:
            return None
        try:
            value = (await pool.servers.settings(guild_id)).get("language")
        except Exception:  # noqa: BLE001 - sem o store o servidor segue o idioma do Discord
            return None
        return value if isinstance(value, str) else None

    async def for_guild(self, guild: Any | None) -> rules.Translator:
        """O tradutor do servidor (cache do ``ServerStore``); DM ou servidor fora do pool usam o ``preferred_locale``."""
        preferred = _locale_value(getattr(guild, "preferred_locale", None))
        saved = await self.language_of(guild.id) if guild is not None else None
        return self.for_locale(self.resolve(saved, preferred))

    async def apply_language(self, guild_id: int) -> None:
        """Depois de ``settings.language`` mudar: recalcula o ``player.tr`` do servidor em TODOS os bots e redesenha o controller.

        Nunca levanta (a configuração já foi gravada); um player com falha não impede os demais. Quem chama limita a taxa (10 s por servidor).
        """
        pool = self.pool
        if pool is None:
            return
        for bot in pool.get_all_bots():
            try:
                guild = bot.get_guild(guild_id)
                player = bot.music.players.get(guild_id)
                if guild is None or player is None:
                    continue
                player.tr = await self.for_guild(guild)
                await player.invoke_np(force=True)
            except Exception as error:  # noqa: BLE001 - enfeite sobre uma configuração já gravada
                logger.warning("Idioma do player não aplicado [%s] (%s)", guild_id, type(error).__name__)

    # ------------------------------------------------------------------ nomes e comandos

    def display_name(self, locale: str) -> str:
        """O nome nativo do idioma. Só um valor do allowlist ``disnake.Locale`` vai ao Babel (I-10); o resto volta como veio."""
        if locale not in loader.LOCALE_NAMES:
            return locale
        try:
            from babel import Locale

            babel_locale = Locale.parse(locale, sep="-")
            name = babel_locale.get_display_name(babel_locale)
        except Exception:  # noqa: BLE001
            return locale
        return name[:1].upper() + name[1:] if name else locale

    def command_text(self, locale: str, key: str) -> str | None:
        """O texto de comando do locale (descrição com a legenda do grupo montada no idioma); ``None`` se faltar."""
        text = self.entries(locale).get(key)
        if text is None:
            return None
        group = rules.command_group(key)
        if group is None:
            return text
        label = self.entries(locale).get(f"cmd.{group}._label") or self.reference.get(f"cmd.{group}._label", "")
        return rules.description_prefix(group, label) + text

    def localized(self, key: str) -> disnake.Localized:
        """``Localized`` com o texto base em ``en-US`` e a chave: o provider completa os outros idiomas.

        A descrição de um comando leva a legenda do grupo (``COMMAND_GROUPS``) montada no idioma.
        """
        base = self.command_text(rules.REFERENCE_LOCALE, key)
        if base is None:
            raise KeyError(key)
        return disnake.Localized(base, key=key)


def _locale_value(locale: Any) -> str | None:
    value = getattr(locale, "value", locale)
    return value if isinstance(value, str) else None


_INSTANCE: I18n | None = None


def get_i18n() -> I18n:
    """O ``I18n`` único do processo (o ``BotPool`` o usa como ``pool.i18n``)."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = I18n()
    return _INSTANCE


def localized(key: str) -> disnake.Localized:
    """Para declarar um comando: ``description=localized("cmd.play.description")``."""
    return get_i18n().localized(key)
