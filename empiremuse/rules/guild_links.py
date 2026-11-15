"""Links fixos do servidor (``player_controller.fav_links``): descrição e item de importação."""

from __future__ import annotations

from typing import Any, Mapping

from empiremuse.rules.errors import DESCRIPTION_INVALID, DESCRIPTION_TOO_LONG, ITEM_FORMAT, RuleError
from empiremuse.rules.limits import GUILD_LINK_DESCRIPTION_MAX, GUILD_LINK_DESCRIPTION_MIN, GUILD_LINK_URL_MAX
from empiremuse.rules.text import clean_track_name, has_control_or_format
from empiremuse.rules.urls import check_url


def check_description(raw: Any, stored: str = "") -> str | RuleError:
    """Descrição aparada: vazia ou de 3 a 100 caracteres, sem os caracteres de ``has_control_or_format``. Reenviar a que já
    estava gravada (``stored``) não é troca e não recusa o formulário: se ela foi gravada antes da regra atual, sai limpa
    como nos filtros de texto externo (``clean_track_name``: os caracteres recusados saem), ou vazia se não sobrar o mínimo."""
    if not isinstance(raw, str):
        return RuleError(DESCRIPTION_INVALID, value=raw)
    if raw == stored:
        return clean_track_name(raw, GUILD_LINK_DESCRIPTION_MIN, GUILD_LINK_DESCRIPTION_MAX) or ""
    text = raw.strip()
    if len(text) > GUILD_LINK_DESCRIPTION_MAX:
        return RuleError(DESCRIPTION_TOO_LONG, value=text, limit=GUILD_LINK_DESCRIPTION_MAX)
    if (text and len(text) < GUILD_LINK_DESCRIPTION_MIN) or has_control_or_format(text):
        return RuleError(DESCRIPTION_INVALID, value=text)
    return text


def import_entry(value: Any) -> dict[str, str] | RuleError:
    """Item de importação ``{"url", "description"}``: URL pura de até 250 caracteres e descrição vazia ou de 3 a 100."""
    if not (
        isinstance(value, Mapping) and isinstance(value.get("url"), str) and isinstance(value.get("description"), str)
    ):
        return RuleError(ITEM_FORMAT)
    url = check_url(value["url"], GUILD_LINK_URL_MAX)
    if isinstance(url, RuleError):
        return url
    description = check_description(value["description"])
    if isinstance(description, RuleError):
        return description
    return {"url": url, "description": description}
