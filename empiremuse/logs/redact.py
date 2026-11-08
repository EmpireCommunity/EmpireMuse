"""Saneamento de caracteres de controle, redação de segredos e teto de tamanho dos registros de log (item 7, 3.5).

Funções puras sobre texto mais o redator único do processo (``configure_secrets`` / ``register_secret`` / ``redact`` /
``scrub``) e o filtro ``SanitizeAndRedact``. Só usa a stdlib e ``empiremuse.rules.owner_shell``; não importa nada de
``empiremuse.logs``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import numbers
import operator
import re
import threading
import traceback
from collections.abc import Iterable, Mapping
from contextlib import suppress
from decimal import Decimal
from fractions import Fraction
from urllib.parse import quote

from empiremuse.rules.owner_shell import DISCORD_TOKEN_SHAPE, MIN_SECRET_LENGTH, mask_secrets

REDACTED = "***"
OMITTED = "[registro omitido: falha ao redigir]"
MAX_MESSAGE_CHARS = 8192
MAX_TRACEBACK_CHARS = 32768
MAX_TEMPLATE_KEY_CHARS = 256
_MAX_CHAINED_EXCEPTIONS = 8

_LINE_BREAKS = re.compile(r"[\r\n\u2028\u2029]")
# C0 exceto \t (\x09) e \n (\x0a) — inclui \r (\x0d) e ESC (\x1b) — mais DEL (\x7f), C1 (\x80-\x9f),
# separadores de linha/parágrafo Unicode (\u2028-\u2029) e controles bidirecionais (\u202a-\u202e, \u2066-\u2069).
_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")

_WEBHOOK_TOKEN = re.compile(r"(/api(?:/v\d+)?/webhooks/\d+/)[A-Za-z0-9._~-]+")
_URL_USERINFO = re.compile(r"://[^/\s]*@")
_QUERY_SECRET_NAMES = r"api_key|apikey|api_sig|sk|token|access_token|refresh_token|client_secret|password|key|signature"
_QUERY_SECRET = re.compile(rf"([?&](?:{_QUERY_SECRET_NAMES})=)[^&#\s'\")]*", re.IGNORECASE)
_DICT_SECRET_NAMES = rf"{_QUERY_SECRET_NAMES}|sessionkey|session_key|authorization"
_DICT_SECRET = re.compile(
    rf"""(?P<prefix>(?P<q1>['"])(?:{_DICT_SECRET_NAMES})(?P=q1)\s*:\s*)(?:(?P<sq>')(?:[^'\\]|\\.)*+'|(?P<dq>")(?:[^"\\]|\\.)*+")""",
    re.IGNORECASE,
)
_AUTH_HEADER = re.compile(r"(\bauthorization\s*:\s*)(?:[A-Za-z]+\s+)?[^\s,'\"\r\n}]+", re.IGNORECASE)

_LOCK = threading.Lock()
_base_secrets: frozenset[str] = frozenset()
_runtime_secrets: frozenset[str] = frozenset()
_all_secrets: frozenset[str] = frozenset()


def _replace_dict_secret(match: re.Match[str]) -> str:
    quote_char = match.group("sq") or match.group("dq")
    return f"{match.group('prefix')}{quote_char}{REDACTED}{quote_char}"


def sanitize_value(text: str) -> str:
    """Saneia valor interpolado: quebras de linha viram ``⏎``; controles (exceto ``\\t``) e bidi viram ``\ufffd``."""
    return _TEXT_CONTROLS.sub("\ufffd", _LINE_BREAKS.sub("⏎", text))


def sanitize_text(text: str) -> str:
    """Saneia texto mantendo ``\\n`` e ``\\t``; demais controles (inclusive ``\\r`` e ESC) e bidi viram ``\ufffd``."""
    return _TEXT_CONTROLS.sub("\ufffd", text)


def _expand_secret(value: object) -> frozenset[str]:
    if not isinstance(value, str) or len(value) < MIN_SECRET_LENGTH:
        return frozenset()
    candidates: set[str] = {
        value,
        value.strip("\r\n"),
        sanitize_value(value),
        sanitize_text(value),
    }
    with suppress(Exception):
        candidates.add(quote(value, safe="", errors="backslashreplace"))
    for raw in (value, value.strip("\r\n")):
        if not raw:
            continue
        with suppress(Exception):
            candidates.add(repr(raw)[1:-1])
        with suppress(Exception):
            candidates.add(json.dumps(raw)[1:-1])
        with suppress(Exception):
            candidates.add(json.dumps(raw, ensure_ascii=False)[1:-1])
    return frozenset(item for item in candidates if len(item) >= MIN_SECRET_LENGTH)


def configure_secrets(values: Iterable[str]) -> None:
    """Troca o conjunto base de segredos (config + ambiente) mantendo os registrados via ``register_secret``."""
    global _base_secrets, _all_secrets
    expanded: set[str] = set()
    for value in values:
        expanded.update(_expand_secret(value))
    new_base = frozenset(expanded)
    with _LOCK:
        _base_secrets = new_base
        _all_secrets = _base_secrets | _runtime_secrets


def register_secret(value: str) -> None:
    """Registra um segredo aprendido em tempo de execução e suas formas escapadas (piso ``MIN_SECRET_LENGTH``)."""
    global _runtime_secrets, _all_secrets
    added = _expand_secret(value)
    if not added:
        return
    with _LOCK:
        _runtime_secrets = _runtime_secrets | added
        _all_secrets = _base_secrets | _runtime_secrets


def cap(text: str, limit: int) -> str:
    """Corta ``text`` em ``limit`` caracteres e acrescenta ``… [+N]`` quando excede o limite."""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}… [+{dropped}]"


def redact(text: str) -> str:
    """Troca valores registrados (do mais longo ao mais curto) e padrões conhecidos de segredo por ``***``."""
    secrets = _all_secrets
    if secrets:
        text = mask_secrets(text, secrets)
    text = DISCORD_TOKEN_SHAPE.sub(REDACTED, text)
    text = _WEBHOOK_TOKEN.sub(rf"\1{REDACTED}", text)
    text = _URL_USERINFO.sub(f"://{REDACTED}@", text)
    text = _QUERY_SECRET.sub(rf"\1{REDACTED}", text)
    text = _DICT_SECRET.sub(_replace_dict_secret, text)
    return _AUTH_HEADER.sub(rf"\1{REDACTED}", text)


def scrub(text: str, limit: int) -> str:
    """Aplica ``sanitize_text`` → ``redact`` → ``cap`` nesta ordem."""
    return cap(redact(sanitize_text(text)), limit)


_PLAIN_NUMBER_BASES: tuple[type, ...] = (int, float, complex, Decimal, Fraction)


def _is_plain_number(value: object) -> bool:
    """Número da stdlib sem ``__str__``/``__repr__`` próprios: não carrega texto de terceiros, então passa como está."""
    if not isinstance(value, numbers.Number):
        return False
    kind = type(value)
    return any(
        issubclass(kind, base) and kind.__str__ is base.__str__ and kind.__repr__ is base.__repr__
        for base in _PLAIN_NUMBER_BASES
    )


class _SanitizedArg:
    """Envolve um argumento de log: ``%s`` e ``%r`` devolvem o ``str()``/``repr()`` original saneado e redigido.

    Preguiçoso de propósito: só chama ``str`` ou ``repr`` do objeto quando o formato pede (um ``__str__`` que levanta
    continua levantando em ``getMessage`` e o filtro troca o registro pelo aviso). O teto de tamanho fica no ``scrub`` final,
    depois da redação (cortar aqui deixaria um segredo pela metade).
    """

    __slots__ = ("_value",)

    def __init__(self, value: object) -> None:
        self._value = value

    def __str__(self) -> str:
        return redact(sanitize_value(str(self._value)))

    def __repr__(self) -> str:
        return redact(sanitize_value(repr(self._value)))


class _SanitizedNumberArg(_SanitizedArg):
    """Como ``_SanitizedArg`` para número com ``__str__``/``__repr__`` próprio: ``%d``/``%f``/``%x`` seguem funcionando."""

    __slots__ = ()

    def __int__(self) -> int:
        return int(self._value)  # type: ignore[call-overload]

    def __index__(self) -> int:
        return operator.index(self._value)  # type: ignore[call-overload]

    def __float__(self) -> float:
        return float(self._value)  # type: ignore[arg-type]


def _sanitize_arg(value: object) -> object:
    if value is None or isinstance(value, bool) or _is_plain_number(value):
        return value
    if isinstance(value, str):
        return sanitize_value(value)
    if isinstance(value, Mapping):
        return {key: _sanitize_arg(item) for key, item in value.items()}
    if isinstance(value, numbers.Number):
        return _SanitizedNumberArg(value)
    return _SanitizedArg(value)


def _qualified_exc_type(exc_type: object) -> str | None:
    if exc_type is None:
        return None
    module = getattr(exc_type, "__module__", None)
    qualname = getattr(exc_type, "__qualname__", getattr(exc_type, "__name__", None))
    if not isinstance(qualname, str):
        return None
    if isinstance(module, str) and module:
        return f"{module}.{qualname}"
    return qualname


def _structured_tb_fingerprint(exc_value: BaseException | None, exc_tb: object) -> str | None:
    frames: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc_value
    tb_only = exc_tb
    depth = 0
    while depth < _MAX_CHAINED_EXCEPTIONS:
        depth += 1
        tb = current.__traceback__ if current is not None and current.__traceback__ is not None else tb_only
        tb_only = None
        if tb is not None:
            for frame, lineno in traceback.walk_tb(tb):  # type: ignore[arg-type]
                code = frame.f_code
                frames.append(f"{code.co_filename}:{lineno}:{code.co_name}")
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
        else:
            break
    if not frames:
        return None
    return hashlib.sha256("\n".join(frames).encode("utf-8", errors="backslashreplace")).hexdigest()


class SanitizeAndRedact(logging.Filter):
    """Filtro total de saneamento, redação e corte; executa só na 1ª passagem por ``LogRecord``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if getattr(record, "empiremuse_redacted", False):
                return True
            if isinstance(record.msg, str):
                # Com args o template é constante do código; sem args a própria mensagem é o template (a linha do stderr do Java) e
                # pode carregar segredo: a chave do agregador nasce do texto já saneado e redigido, nunca do cru.
                template_text = record.msg if record.args else redact(sanitize_text(record.msg))
                record.empiremuse_template = template_text[:MAX_TEMPLATE_KEY_CHARS]
                # Mensagens longas que só diferem depois do corte do template não podem virar a mesma chave de agregação.
                record.empiremuse_template_fp = (
                    hashlib.sha256(template_text.encode("utf-8", errors="backslashreplace")).hexdigest()[:16]
                    if len(template_text) > MAX_TEMPLATE_KEY_CHARS
                    else None
                )
            else:
                record.empiremuse_template = _qualified_exc_type(type(record.msg)) or type(record.msg).__qualname__
                record.empiremuse_template_fp = None
            if record.args:
                if isinstance(record.args, Mapping):
                    record.args = {key: _sanitize_arg(val) for key, val in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(_sanitize_arg(val) for val in record.args)
                else:
                    record.args = (_sanitize_arg(record.args),)
            message = record.getMessage()
            record.msg = scrub(message, MAX_MESSAGE_CHARS)
            record.args = None
            if record.exc_info and isinstance(record.exc_info, tuple) and record.exc_info[0] is not None:
                record.empiremuse_exc_type = _qualified_exc_type(record.exc_info[0])
                record.empiremuse_tb_fp = _structured_tb_fingerprint(record.exc_info[1], record.exc_info[2])
                formatted = logging.Formatter().formatException(record.exc_info)
                record.exc_text = scrub(formatted, MAX_TRACEBACK_CHARS)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = scrub(str(record.exc_text), MAX_TRACEBACK_CHARS)
                record.empiremuse_exc_type = getattr(record, "empiremuse_exc_type", None)
                record.empiremuse_tb_fp = getattr(record, "empiremuse_tb_fp", None)
            else:
                record.empiremuse_exc_type = None
                record.empiremuse_tb_fp = None
            if record.stack_info:
                record.stack_info = scrub(str(record.stack_info), MAX_TRACEBACK_CHARS)
            record.empiremuse_redacted = True
        except Exception:
            with suppress(Exception):
                record.msg = OMITTED
                record.args = None
                record.exc_info = None
                record.exc_text = None
                record.stack_info = None
                record.empiremuse_template = OMITTED
                record.empiremuse_exc_type = None
                record.empiremuse_tb_fp = None
                record.empiremuse_redacted = True
        return True
