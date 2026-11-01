"""Categoria Geral: prefixo (G-12), status automático do canal de voz (G-13) e idioma do servidor (item 21)."""

from __future__ import annotations

import logging
import unicodedata
from time import monotonic
from typing import Any

from empiremuse.panel.services.base import (
    Args,
    FormContext,
    ParseResult,
    SettingsForm,
    first,
    flag,
    panel_i18n,
    panel_translator,
)
from empiremuse.panel.services.player import active_player
from empiremuse.rules import limits
from empiremuse.rules.errors import PREFIX_LENGTH, STATUS_CHARS, STATUS_TOO_LONG, RuleError
from empiremuse.rules.settings import validate_prefix, validate_voice_status
from empiremuse.rules.voice_status import DEFAULT_VOICE_TEMPLATE, resolve_voice_status
from empiremuse.utils.music.models.player_voice import schedule_permanent_voice_status

logger = logging.getLogger(__name__)

MSG_PREFIX_LENGTH = f"O prefixo pode ter no máximo {limits.PREFIX_MAX_LENGTH} caracteres."
MSG_PREFIX_CHARS = "O prefixo não pode conter espaços nem caracteres de controle."
MSG_VOICE_PLACEHOLDER = "Use pelo menos um placeholder válido, como {track.title}."
MSG_VOICE_LENGTH = f"O texto pode ter no máximo {limits.VOICE_STATUS_MAX_LENGTH} caracteres."
MSG_VOICE_CHARS = "O texto não pode ter caracteres de controle nem de formatação."
LANGUAGE_COOLDOWN_SECONDS = 10  # entre trocas que chegam ao Discord (``apply_language``), por servidor (I-11)


class PrefixForm(SettingsForm):
    key = "prefixo"
    category = "geral"
    scope_kind = "global"
    fields = ("prefix",)
    markers = fields

    def extract(self, doc: dict) -> dict[str, Any]:
        return {"prefix": doc["prefix"]}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["prefix"] = new["prefix"]

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        sent = first(args, "prefix") or ""
        echo = {"prefix": sent}
        if not sent.strip():
            return ParseResult({"prefix": ""}, {}, echo)  # vazio = voltar ao prefixo padrão (como resetprefix)
        prefix = validate_prefix(sent)
        if isinstance(prefix, RuleError):
            return ParseResult(
                {}, {"prefix": MSG_PREFIX_LENGTH if prefix.code == PREFIX_LENGTH else MSG_PREFIX_CHARS}, echo
            )
        return ParseResult({"prefix": prefix}, {}, echo)

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        ctx.pool.guild_prefix_cache[ctx.guild_id] = new["prefix"]


class VoiceStatusForm(SettingsForm):
    key = "status_voz"
    category = "geral"
    scope_kind = "global"
    fields = ("voice_status_permanent", "voice_channel_status")
    markers = fields

    def extract(self, doc: dict) -> dict[str, Any]:
        cfg = resolve_voice_status(doc)
        return {
            "voice_status_permanent": cfg.permanent,
            "voice_channel_status": "" if cfg.template == DEFAULT_VOICE_TEMPLATE else cfg.template,
        }

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["voice_status_permanent"] = new["voice_status_permanent"]
        doc["voice_channel_status"] = new["voice_channel_status"]

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {"placeholders": limits.VOICE_PLACEHOLDERS, "modelo_padrao": DEFAULT_VOICE_TEMPLATE}

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        raw = first(args, "voice_channel_status") or ""
        permanent = flag(args, "voice_status_permanent")
        echo = {"voice_status_permanent": permanent, "voice_channel_status": raw}
        text = validate_voice_status(raw)
        if isinstance(text, RuleError):
            message = {STATUS_TOO_LONG: MSG_VOICE_LENGTH, STATUS_CHARS: MSG_VOICE_CHARS}.get(
                text.code, MSG_VOICE_PLACEHOLDER
            )
            return ParseResult({}, {"voice_channel_status": message}, echo)
        return ParseResult({"voice_status_permanent": permanent, "voice_channel_status": text}, {}, echo)

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        cfg = resolve_voice_status(new)
        for bot in ctx.pool.get_guild_bots(ctx.guild_id):
            player = active_player(bot, ctx.guild_id)
            if player is not None:
                player.voice_status_saved_default = False  # há uma escolha salva: o aviso de padrão não vale mais
                schedule_permanent_voice_status(player, cfg)  # não espera: o POST responde sem aguardar o Discord


class LanguageForm(SettingsForm):
    """Idioma do servidor (``settings.language`` no documento único): vazio = automático (``None``).

    Só valores iguais aos de ``pool.i18n.enabled`` entram (I-11); um erro nunca ecoa o que foi enviado. Uma troca que
    chega ao Discord respeita ``LANGUAGE_COOLDOWN_SECONDS`` por servidor; o cooldown só conta trocas gravadas.
    """

    key = "idioma"
    category = "geral"
    scope_kind = "servidor"
    fields = ("language",)
    markers = fields

    def __init__(self) -> None:
        self._last_change: dict[int, float] = {}

    def extract(self, doc: dict) -> dict[str, Any]:
        return {"language": doc.get("language")}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["language"] = new["language"]

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        i18n = panel_i18n(ctx.pool)
        options = [(code, i18n.display_name(code)) for code in sorted(i18n.enabled)]
        saved = current.get("language")
        if saved is not None and saved not in i18n.enabled:  # gravado, mas desligado: continua visível, só é pulado
            options.append((saved, panel_translator(ctx.pool)("panel.language.unavailable", code=saved)))
        return {"idiomas": tuple(options)}

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        sent = first(args, "language") or ""
        value = sent if sent else None
        if value == current.get("language"):
            return ParseResult({"language": value}, {}, {})
        tr = panel_translator(ctx.pool)
        if value is not None and value not in panel_i18n(ctx.pool).enabled:  # igualdade exata, sem normalizar
            return ParseResult({}, {"language": tr("panel.language.unknown")}, {})
        if self._in_cooldown(ctx.guild_id):
            return ParseResult({}, {"language": tr("panel.language.cooldown", seconds=LANGUAGE_COOLDOWN_SECONDS)}, {})
        return ParseResult({"language": value}, {}, {})

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        now = monotonic()
        recent = {gid: at for gid, at in self._last_change.items() if now - at < LANGUAGE_COOLDOWN_SECONDS}
        self._last_change = {**recent, ctx.guild_id: now}
        logger.info(
            "Idioma alterado: servidor=%s usuario=%s %s -> %s",
            ctx.guild_id,
            ctx.user_id,
            current.get("language"),
            new["language"],
        )
        await panel_i18n(ctx.pool).apply_language(ctx.guild_id)  # coroutine; nunca levanta (Turbine, §3)

    def _in_cooldown(self, guild_id: int) -> bool:
        last = self._last_change.get(guild_id)
        return last is not None and monotonic() - last < LANGUAGE_COOLDOWN_SECONDS
