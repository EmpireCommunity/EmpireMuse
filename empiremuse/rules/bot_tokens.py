"""Um token por variável, com papel explícito e diagnósticos sem segredos (T-428)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re

token_regex = re.compile(r"[a-zA-Z0-9_-]{23,28}\.[a-zA-Z0-9_-]{6,7}\.[a-zA-Z0-9_-]{27,}")
_NAME = re.compile(r"TOKEN_(RESERVE|RADIO)_[A-Z0-9_]{1,32}")
_NUMBER = re.compile(r"(\d+)")
_PREFIXES = ("TOKEN_RESERVE_", "TOKEN_RADIO_")


class Role(StrEnum):
    MAIN = "main"
    RESERVE = "reserve"
    RADIO = "radio"


class BotToken:
    __slots__ = ("identifier", "role", "_token")

    def __init__(self, identifier: str, role: Role, token: str) -> None:
        self.identifier = identifier
        self.role = role
        self._token = token

    def reveal(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return f"BotToken({self.identifier!r}, {self.role!r})"

    def __reduce__(self):
        raise TypeError("BotToken não pode ser serializado")

    def __getstate__(self):
        raise TypeError("BotToken não pode ser serializado")


@dataclass(frozen=True)
class TokenIssue:
    code: str
    variable: str
    other: str | None = None


@dataclass(frozen=True)
class TokenLoad:
    bots: tuple[BotToken, ...]
    issues: tuple[TokenIssue, ...]
    fatal: bool


def role_of(identifier: object) -> Role | None:
    if identifier == "TOKEN":
        return Role.MAIN
    if not isinstance(identifier, str) or not (match := _NAME.fullmatch(identifier)):
        return None
    return Role.RESERVE if match[1] == "RESERVE" else Role.RADIO


def is_radio_only_identifier(identifier: object) -> bool:
    """Se o identificador pertence a um bot dedicado exclusivamente à rádio."""
    return role_of(identifier) is Role.RADIO


def identifier_sort_key(identifier: str) -> tuple:
    """Ordem natural dos identificadores, como _2 antes de _10."""
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in _NUMBER.split(identifier))


def load_bot_tokens(source: Mapping[str, object]) -> TokenLoad:
    accepted = []
    issues = []
    for name in source:
        role = role_of(name)
        if role is not None:
            accepted.append((name, role))
        else:
            for prefix in _PREFIXES:
                if name.upper().startswith(prefix):
                    issues.append(TokenIssue("NAME", prefix))
                    break
    accepted.sort(key=lambda item: (list(Role).index(item[1]), identifier_sort_key(item[0])))
    bots: list[BotToken] = []
    seen: dict[str, str] = {}
    main_seen = False
    for name, role in accepted:
        value = source[name]
        if not isinstance(value, str) or not value.strip():
            continue
        if role is Role.MAIN:
            main_seen = True
        if len(value) > 256:
            issues.append(TokenIssue("TOO_LONG", name))
            continue
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
            if not value:
                if role is Role.MAIN:
                    main_seen = False
                continue
        if not token_regex.fullmatch(value):
            issues.append(TokenIssue("FORMAT", name))
            continue
        if value in seen:
            issues.append(TokenIssue("DUPLICATE", name, seen[value]))
            continue
        seen[value] = name
        bots.append(BotToken(name, role, value))
    if not main_seen:
        issues.insert(0, TokenIssue("MISSING", "TOKEN"))
    return TokenLoad(tuple(bots), tuple(issues), not any(bot.role is Role.MAIN for bot in bots))


def describe(issue: TokenIssue) -> str:
    if issue.code == "NAME":
        return f"prefixo {issue.variable}: nome inválido (NAME)"
    messages = {
        "MISSING": "não definido",
        "FORMAT": "deve ter 1 token válido",
        "TOO_LONG": "valor excede o limite permitido",
        "DUPLICATE": f"duplicado de {issue.other}",
    }
    return f"{issue.variable}: {messages[issue.code]} ({issue.code})"
