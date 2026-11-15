"""Regras puras do status automático do canal de voz (2.1.2, contrato I2): padrão, resolução do documento, texto seguro e limites.

O "permanente" é UM valor por servidor, para todo o pool: ``voice_status_permanent`` (``None`` = nunca escolhido = ligado) e o modelo
``voice_channel_status`` (vazio ou inválido = ``DEFAULT_VOICE_TEMPLATE``). Aqui só há texto e números; o envio é do player.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from empiremuse.rules.limits import VOICE_PLACEHOLDERS
from empiremuse.rules.settings import validate_voice_status
from empiremuse.rules.text import clean_legacy_text, has_control_or_format

DEFAULT_VOICE_TEMPLATE = "{track.emoji} Tocando: {track.title} - {track.author}"

VOICE_STATUS_MAX_UNITS = 500  # limite do Discord, em unidades UTF-16
VOICE_STATUS_MIN_INTERVAL = 5  # s entre dois PUT de status do mesmo player (throttle de borda)
VOICE_STATUS_BOT_RATE = 2  # PUT/s por bot (token bucket compartilhado pelos players do bot)
VOICE_STATUS_RESUME_JITTER = 10  # s: espalha a reaplicação das sessões retomadas
VOICE_STATUS_REAPPLY_INTERVAL = 300  # s: reenvio do status depois de edição manual
VOICE_CONTROL_LOCK_SECONDS = 5  # trava de comandos em canal de voz com o status ligado
STAGE_CONTROL_LOCK_SECONDS = 120  # a mesma trava no palco

_ELLIPSIS = "..."
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
_PLACEHOLDER = re.compile("|".join(re.escape(placeholder) for placeholder in VOICE_PLACEHOLDERS))
_LINE_BREAK = re.compile(r"\r\n|[\r\n  ]")
_MARKDOWN = re.compile(r"[_\\~|*`\[]")


@dataclass(frozen=True)
class VoiceStatusConfig:
    """O permanente do servidor já resolvido: se está ligado e qual o modelo."""

    permanent: bool
    template: str


def resolve_voice_status(doc: Mapping[str, object]) -> VoiceStatusConfig:
    """Lê o permanente e o modelo do documento global do servidor; o que não for válido vira o padrão. Nunca levanta."""
    if not isinstance(doc, Mapping):
        return VoiceStatusConfig(True, DEFAULT_VOICE_TEMPLATE)
    saved_flag = doc.get("voice_status_permanent")
    permanent = saved_flag if isinstance(saved_flag, bool) else True
    template = DEFAULT_VOICE_TEMPLATE
    saved_template = doc.get("voice_channel_status")
    if isinstance(saved_template, str):
        # gravado antes da regra atual: lido limpo (``clean_legacy_text``), nunca trocado pelo padrão (o formulário o regravaria vazio)
        checked = validate_voice_status(clean_legacy_text(saved_template))
        if isinstance(checked, str) and checked:
            template = checked
    return VoiceStatusConfig(permanent, template)


def control_lock_seconds(channel_is_stage: bool) -> int:
    """A trava de comandos depois de a faixa começar: 120 s no palco, 5 s em canal de voz."""
    return STAGE_CONTROL_LOCK_SECONDS if channel_is_stage else VOICE_CONTROL_LOCK_SECONDS


def voice_status_value(raw: object) -> str:
    """Texto externo (título, autor, playlist, nome) pronto para entrar no status: inerte, sem menção, emoji, link nem formatação."""
    if not isinstance(raw, str):
        return ""
    text = _LINE_BREAK.sub(" ", raw)
    text = "".join(char for char in text if not has_control_or_format(char))
    # Todo "<" e todo "@" (não só os de tokens completos): os valores são colados por um modelo de admin e dois pedaços
    # inertes não podem formar um token (``<@&1>``, ``<a:n:id>``, ``@everyone``) na junção. U+2060 seria Cf; "‹" e "＠" são texto.
    text = text.replace("<", "‹").replace("@", "＠")
    text = _MARKDOWN.sub(lambda match: "\\" + match.group(), text)
    return text.replace("//", "／／")  # o escape de markdown não pega uma URL nua, nem "https:" + "//x" colados


def render_voice_status(template: str, values: Mapping[str, str]) -> str:
    """Troca os placeholders numa passada só: o valor inserido nunca é reprocessado (título "{requester.id}" sai literal)."""
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(), match.group()), template)


def _units(char: str) -> int:
    return 2 if ord(char) > 0xFFFF else 1


def fit_voice_status(text: str, limit: int = VOICE_STATUS_MAX_UNITS) -> str:
    """Corta em ``limit`` unidades UTF-16 sem partir um ``<a?:nome:id>``; termina em "..." quando corta."""
    if sum(_units(char) for char in text) <= limit:
        return text
    budget = max(limit - len(_ELLIPSIS), 0)
    pieces: list[str] = []
    used = 0
    position = 0
    while position < len(text):
        emoji = _CUSTOM_EMOJI.match(text, position)
        piece = emoji.group() if emoji else text[position]
        size = sum(_units(char) for char in piece)
        if used + size > budget:
            break
        pieces.append(piece)
        used += size
        position += len(piece)
    return ("".join(pieces) + _ELLIPSIS)[: max(limit, 0)]
