"""Regras de texto: nomes normalizados, nomes iguais sem diferenciar a caixa, status de voz e trechos de texto externo."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Optional

from empiremuse.rules.usage import escape_markdown


def has_control_or_format(text: str) -> bool:
    """O texto tem caractere de controle (``Cc``) ou de formatação (``Cf``, como U+202E e U+200B)?"""
    return any(unicodedata.category(char) in ("Cc", "Cf") for char in text)


def clean_title(raw: Any, low: int, high: int) -> Optional[str]:
    """Nome normalizado (strip, ``low``–``high`` caracteres, sem controle/formatação) ou ``None``."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not low <= len(name) <= high:
        return None
    if has_control_or_format(name):
        return None
    return name


def clean_import_name(name: str, low: int, high: int, already: dict) -> Optional[str]:
    """Nome normalizado (regra do painel) que ainda não apareceu em ``already``; ``None`` se inválido ou repetido."""
    clean = clean_title(name, low, high)
    if clean is None or clean.casefold() in {key.casefold() for key in already}:
        return None
    return clean


def drop_same_names(entries: dict, names) -> None:
    """Tira de ``entries`` (cópia de trabalho) as chaves iguais a ``names``, sem diferenciar maiúsculas de minúsculas."""
    wanted = {name.casefold() for name in names}
    for key in [key for key in entries if key.casefold() in wanted]:
        del entries[key]


def normalize_status(text: str) -> str:
    """Quebras de linha viram espaço e as pontas são aparadas, como o modal do comando."""
    flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    return flat.strip()


def find_same_name(entries: Mapping[str, Any], name: str) -> Optional[str]:
    """A chave de ``entries`` igual a ``name`` sem diferenciar maiúsculas de minúsculas, ou ``None``."""
    wanted = name.casefold()
    return next((key for key in entries if key.casefold() == wanted), None)


def clean_track_name(raw: Any, low: int, high: int) -> Optional[str]:
    """Nome vindo do título de uma faixa: sem controle/formatação, aparado, cortado em ``high`` e aparado de novo.

    ``None`` se não for texto ou se sobrar menos de ``low`` caracteres.
    """
    if not isinstance(raw, str):
        return None
    visible = "".join(char for char in raw if unicodedata.category(char) not in ("Cc", "Cf"))
    name = visible.strip()[:high].strip()
    return name if len(name) >= low else None


def plain_snippet(value: Any, limit: int) -> str:
    """Texto externo (por exemplo, a mensagem de erro do Lavalink) seguro para mostrar numa linha de mensagem.

    Uma linha só, sem caracteres de controle/formatação e sem crase (não quebra o markdown nem abre bloco de código);
    mais longo que ``limit`` é cortado com reticências.
    """
    text = "" if value is None else str(value)
    text = "".join(" " if char.isspace() else char for char in text)
    text = "".join(char for char in text if unicodedata.category(char) not in ("Cc", "Cf"))
    text = " ".join(text.replace("`", "'").split())
    return text if len(text) <= limit else text[:limit] + "…"


def format_mib(num_bytes: int) -> str:
    """Tamanho em MiB (base binária, uma casa e vírgula decimal): ``126353408`` vira ``120,5 MiB``."""
    return f"{num_bytes / (1 << 20):.1f}".replace(".", ",") + " MiB"


def format_int(n: int) -> str:
    """Inteiro com ponto de milhar: ``1035`` vira ``1.035``."""
    return f"{n:,}".replace(",", ".")


# ---------------------------------------------------------------------- texto externo publicado (R-11 do item 8)

_EXT_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_EXT_INVISIBLE = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad\u2800\u3164\u115f\u1160\uffa0\u180e")
_EXT_COMBINING_RUN = 4
_EXT_MENTION = re.compile(r"@(everyone|here)", re.IGNORECASE)
_EXT_ANGLE = re.compile(r"<(?=(@|#|/|id:|:|a:))", re.IGNORECASE)
_EXT_DOMAIN = re.compile(r"(?<![a-z0-9-])((?:[a-z0-9-]{1,63}\.){1,8}[a-z]{2,24})(?![a-z0-9-])", re.IGNORECASE)
_EXT_ONE_DOT = "\u2024"  # ponto de líder: parece um ponto, mas não forma domínio nem link
_EXT_ELLIPSIS = "…"


def _utf16(char: str) -> int:
    return 2 if ord(char) > 0xFFFF else 1


def external_text(value: Any, limit: int, *, markdown: bool = True) -> str:
    """O ÚNICO caminho do texto vindo de fora (nome e tags de estação, ``StreamTitle``, nome de playlist) até o que o bot publica.

    Em ordem: NFKC (``ｄｉｓｃｏｒｄ．ｇｇ`` vira ``discord.gg`` antes da detecção); tira controle (``Cc``, inclusive quebra de linha, que vira espaço),
    formatação (``Cf``), bidi, invisíveis e o que passa de 4 marcas combinantes seguidas; neutraliza ``@everyone``/``@here``, ``<@``, ``<@&``,
    ``<#``, ``</``, ``<id:``, ``<:``, ``<a:`` e links (``://``, ``www.``, ``](`` e domínio nu, o que cobre ``discord.gg``, ``dsc.gg`` e
    ``discord.com/invite``); escapa o markdown (``markdown=False`` para destinos de texto puro, como status de voz e tópico de palco); corta
    em ``limit`` unidades UTF-16 com reticências. Nunca devolve menção viva nem link clicável.
    """
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    kept: list[str] = []
    run = 0
    for char in text:
        category = unicodedata.category(char)
        if char.isspace() or category == "Cc" or char in ("\u2028", "\u2029"):
            kept.append(" ")
            run = 0
            continue
        if category in ("Cf", "Cs", "Co", "Cn") or char in _EXT_BIDI or char in _EXT_INVISIBLE:
            continue
        run = run + 1 if category in ("Mn", "Me") else 0
        if run > _EXT_COMBINING_RUN:
            continue
        kept.append(char)
    text = " ".join("".join(kept).split())
    text = _EXT_MENTION.sub(lambda m: "@" + _EXT_ONE_DOT + m.group(1), text)
    text = _EXT_ANGLE.sub("\u2039", text)
    text = text.replace("://", "\u2236//").replace("](", "] (")
    text = re.sub(r"www\.", "www" + _EXT_ONE_DOT, text, flags=re.IGNORECASE)
    text = _EXT_DOMAIN.sub(lambda m: m.group(1).replace(".", _EXT_ONE_DOT), text)
    if markdown:
        text = escape_markdown(text)
    if sum(_utf16(c) for c in text) <= limit:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        if used + _utf16(char) > limit - 1:
            break
        out.append(char)
        used += _utf16(char)
    return "".join(out).rstrip("\\ ") + _EXT_ELLIPSIS
