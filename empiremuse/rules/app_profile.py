"""Regras puras do perfil da aplicação no Discord (descrição e tags): modelo, validação e diferença para o valor atual.

A descrição vem de um modelo com ``{prefix}``, ``{bots}``, ``{panel_url}``, ``{support_url}`` e ``{main}`` (nome do bot principal):
a linha cujo marcador resolve vazio sai inteira. ``APP_DESCRIPTION``/``APP_TAGS`` vazios = não mexer no campo. Aqui só há texto; o envio é de ``utils/app_profile``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from empiremuse.rules.text import has_control_or_format

DESCRIPTION_LIMIT = 400  # limite do Discord para a descrição da aplicação, depois de renderizada
TAGS_MAX = 5
TAG_MAX_LENGTH = 20
PANEL_PATH = "/painel"
DEFAULT_PANEL_PRESENCE = "⚙️ Painel: {panel_url}"
LEGACY_PANEL_PRESENCE = "⚙️ Painel: muse.empirecommunity.org/panel"

DEFAULT_DESCRIPTION = "\n".join(
    (
        "🎵 Bot de música da comunidade EmpireHub: YouTube, Spotify e Deezer.",
        "▶️ Comece com /play ou {prefix}play <nome ou link>. Ajuda: {prefix}help",
        "🤖 Somos {bots} bots: se eu estiver ocupado, uma reserva entra na sua call.",
        "⚙️ Painel: {panel_url}",
        "💬 Suporte: {support_url}",
    )
)
DEFAULT_RESERVE_DESCRIPTION = "\n".join(
    (
        "🎵 Reserva do {main}: entro na call quando o {main} está ocupado.",
        "▶️ Comandos de barra ficam no {main}; aqui use {prefix}play. Ajuda: {prefix}help",
        "⚙️ Painel: {panel_url}",
        "💬 Suporte: {support_url}",
    )
)
DEFAULT_TAGS = "música,music,player,spotify,youtube"

_ELLIPSIS = "…"
_PLACEHOLDER = re.compile(r"\{(prefix|bots|panel_url|support_url|main)\}")


@dataclass(frozen=True)
class DesiredProfile:
    """O que se quer gravar: ``None`` num campo = não mexer nele."""

    description: str | None = None
    tags: tuple[str, ...] | None = None
    warnings: tuple[str, ...] = field(default=())


def utf16_length(text: str) -> int:
    """Tamanho em unidades UTF-16 (emoji conta 2): a medida mais conservadora que o Discord pode usar."""
    return len(text.encode("utf-16-le")) // 2


def _clean_value(value: object) -> str:
    """Valor de configuração a inserir no texto: uma linha, sem caractere de controle ou de formatação."""
    return "".join(char for char in str(value or "") if not has_control_or_format(char)).strip()


def panel_url_of(base_url: object, enabled: object) -> str:
    """Endereço público do painel (``PANEL_BASE_URL`` + ``/painel``), ou vazio se o painel está desligado ou sem endereço."""
    base = _clean_value(base_url).rstrip("/")
    return f"{base}{PANEL_PATH}" if enabled and base else ""


def _render_line(line: str, values: dict[str, str]) -> str | None:
    """A linha com os marcadores trocados, ou ``None`` quando algum marcador dela resolve vazio."""
    names = _PLACEHOLDER.findall(line)
    if any(not values[name] for name in names):
        return None
    return _PLACEHOLDER.sub(lambda found: values[found.group(1)], line)


def truncate_description(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    """Cabe no limite sem cortar uma linha (e um link) ao meio; só uma primeira linha grande demais é cortada, com reticências."""
    if utf16_length(text) <= limit:
        return text
    lines = text.split("\n")
    while len(lines) > 1 and utf16_length("\n".join(lines)) > limit:
        lines.pop()
    kept = "\n".join(lines).rstrip()
    if utf16_length(kept) <= limit:
        return kept
    while kept and utf16_length(kept) + utf16_length(_ELLIPSIS) > limit:
        kept = kept[:-1]
    return kept.rstrip() + _ELLIPSIS


def render_description(
    template: object, *, prefix: object, bots: int, panel_url: object, support_url: object, main: object = ""
) -> tuple[str, bool]:
    """A descrição pronta e se foi preciso encurtá-la. Vazia = não há nada a gravar.

    ``\\n`` escrito à mão no modelo vale quebra de linha (o ``.env`` e o ``docker-compose`` guardam o valor em uma linha só).
    ``{bots}`` fica vazio com um bot só (a linha sai) e ``{main}`` sem o principal logado (as linhas dele saem).
    """
    if not isinstance(template, str):
        return "", False
    values = {
        "prefix": _clean_value(prefix),
        "bots": str(bots) if bots > 1 else "",
        "panel_url": _clean_value(panel_url),
        "support_url": _clean_value(support_url),
        "main": _clean_value(main),
    }
    raw = template.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    lines = [rendered for line in raw.split("\n") if (rendered := _render_line(line.rstrip(), values)) is not None]
    text = "\n".join(lines).strip()
    fitted = truncate_description(text)
    return fitted, fitted != text


def parse_tags(raw: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(tags válidas, avisos)`` de uma lista separada por vírgula. Inválida (grande demais, com controle) ou repetida sai com aviso;
    passando de ``TAGS_MAX`` ficam as primeiras."""
    if not isinstance(raw, str):
        return (), ()
    tags: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for position, item in enumerate(raw.split(","), start=1):
        tag = item.strip()
        if not tag:
            continue
        # o aviso diz o motivo e a posição, nunca o texto da tag (vai para o log)
        if utf16_length(tag) > TAG_MAX_LENGTH or has_control_or_format(tag):
            warnings.append(
                f"tag {position} descartada (mais de {TAG_MAX_LENGTH} caracteres ou com caractere inválido)"
            )
        elif tag.casefold() in seen:
            warnings.append(f"tag {position} repetida descartada")
        elif len(tags) >= TAGS_MAX:
            warnings.append(f"tag {position} descartada (o Discord aceita no máximo {TAGS_MAX})")
        else:
            seen.add(tag.casefold())
            tags.append(tag)
    return tuple(tags), tuple(warnings)


def build_profile(
    *,
    description: object,
    tags: object,
    prefix: object,
    bots: int,
    panel_url: object,
    support_url: object,
    main: object = "",
) -> DesiredProfile:
    """O perfil desejado a partir da configuração. Vazio (ou sem nada válido) deixa o campo como está no Discord."""
    warnings: list[str] = []
    desired_description = None
    if isinstance(description, str) and description.strip():
        text, truncated = render_description(
            description, prefix=prefix, bots=bots, panel_url=panel_url, support_url=support_url, main=main
        )
        if truncated:
            warnings.append(f"descrição passou de {DESCRIPTION_LIMIT} caracteres e foi encurtada")
        desired_description = text or None
    desired_tags = None
    if isinstance(tags, str) and tags.strip():
        valid, tag_warnings = parse_tags(tags)
        warnings.extend(tag_warnings)
        desired_tags = valid or None
    return DesiredProfile(description=desired_description, tags=desired_tags, warnings=tuple(warnings))


def _same_description(current: object, wanted: str) -> bool:
    return isinstance(current, str) and current.replace("\r\n", "\n").strip() == wanted


def plan_changes(
    current_description: object, current_tags: Sequence[str] | None, desired: DesiredProfile
) -> dict[str, object]:
    """Só os campos que diferem do valor atual, nos nomes do ``AppInfo.edit`` (vazio = nada a enviar)."""
    changes: dict[str, object] = {}
    if desired.description is not None and not _same_description(current_description, desired.description):
        changes["description"] = desired.description
    if desired.tags is not None and tuple(current_tags or ()) != desired.tags:
        changes["tags"] = list(desired.tags)
    return changes
