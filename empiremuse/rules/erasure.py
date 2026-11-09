"""Regras puras do apagamento dos dados de um membro: o que apagar, limites, impressões e relatório.

Só biblioteca padrão e sem I/O: o relógio entra por parâmetro. O serviço que executa fica em ``empiremuse/utils/erasure.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ERASE_RECHECK_S = 60
ERASE_TOMBSTONE_MIN_S = 600
ERASE_OK_PER_HOUR = 3
ERASE_TRIES_PER_HOUR = 10
ERASE_GLOBAL_PER_HOUR = 60
ERASE_MAX_SESSION_AGE_S = 1800
ERASE_CONCURRENCY = 2
OWNER_GRANT_TTL_S = 86400
OWNER_GRANTS_PER_HOUR = 10

RATE_WINDOW_S = 3600
# Q1 (decidida): nome do comando por locale, num só lugar. O cog declara o comando pelas chaves ``cmd.delete_my_data.*`` dos catálogos,
# que repetem estes valores (um teste confere); renomear é mudar aqui e nos dois catálogos.
ERASE_COMMAND_NAMES = {"pt-BR": "apagar_meus_dados", "en-US": "delete_my_data"}
OWNER_COMMAND_NAME = "apagardados"  # comando do dono (§3.4): só no privado, só OWNER_IDS
ANONYMIZED = "deleted"
SNOWFLAKE_MIN_DIGITS = 17
SNOWFLAKE_MAX_DIGITS = 20
SNOWFLAKE_LIMIT = 2**64

GLOBAL_DB = "global"
SCROBBLES_DB = "scrobbles"


def tombstone_seconds(dbcache_ttl: float) -> float:
    """``ERASE_TOMBSTONE_S`` = max(600 s, 2 × ``DBCACHE_TTL``): a sepultura dura mais que o cache do banco."""
    return max(float(ERASE_TOMBSTONE_MIN_S), 2.0 * float(dbcache_ttl))


ERASE_TOMBSTONE_S = tombstone_seconds(300)  # com o ``DBCACHE_TTL`` padrão


class StoreAction(StrEnum):
    DELETE = "delete"
    DELETE_MATCHING = "delete_matching"
    ANONYMIZE = "anonymize"


@dataclass(frozen=True)
class StoreRef:
    """Um armazenamento da §2 do contrato. ``database`` é o argumento ``collection`` dos métodos do banco; ``collection`` é o ``db_name``."""

    key: str
    database: str
    collection: str
    action: StoreAction
    local: bool = False  # sempre o banco local, mesmo com MongoDB


STORES: tuple[StoreRef, ...] = (
    StoreRef("users", GLOBAL_DB, "users", StoreAction.DELETE),
    StoreRef("scrobbles", SCROBBLES_DB, "users", StoreAction.DELETE, local=True),
    StoreRef("radio_user", GLOBAL_DB, "radio_user", StoreAction.DELETE),  # a coleção pode não existir (J3)
    StoreRef("radio_custom_favorites", GLOBAL_DB, "radio_custom_favorites", StoreAction.DELETE_MATCHING),
    StoreRef("radios", GLOBAL_DB, "radios", StoreAction.ANONYMIZE),
    StoreRef("radio_stations", GLOBAL_DB, "radio_stations", StoreAction.ANONYMIZE),
)
MEMORY_KEY = "memory"
SAVED_QUEUE_KEY = "saved_queue"  # arquivo, fora de STORES (§2 linha 23)
INTERNAL_KEY = "internal"  # erro inesperado em algum passo (nunca texto da exceção)


def read_is_stale(erased_at: float | None, read_at: float, now: float, ttl_s: float) -> bool:
    """Uma leitura de ``global.users`` ou ``scrobbles.users`` feita em ``read_at`` não pode mais ser gravada de volta?

    Sim se houve apagamento depois dela (``erased_at`` é o FIM da exclusão e ``erased_at >= read_at``) ou se ela é velha demais
    (``now - read_at >= ttl_s``), com ou sem sepultura: passado o TTL a sepultura some e não dá para provar que não houve apagamento.
    """
    return (erased_at is not None and erased_at >= read_at) or now - read_at >= ttl_s


def saved_queue_file_names(user_id: int) -> tuple[str, ...]:
    """Os nomes da fila salva do membro (JSON e o ``.pkl`` antigo), montados só a partir do inteiro: nunca glob nem prefixo."""
    return (f"{int(user_id)}.json", f"{int(user_id)}.pkl")


def saved_queue_tmp_matches(name: str, user_id: int) -> bool:
    """O temporário órfão de uma queda no meio da gravação: ``<id>.json.<aleatório>.tmp`` (o aleatório do ``mkstemp`` não tem ponto)."""
    prefix, suffix = f"{int(user_id)}.json.", ".tmp"
    middle = name[len(prefix) : -len(suffix)] if name.startswith(prefix) and name.endswith(suffix) else ""
    return bool(middle) and middle.replace("_", "").isalnum() and middle.isascii()


def custom_favorite_key_matches(key: str, user_id: int) -> bool:
    """``"<servidor>:<membro>"``: o membro é o texto exato depois do último ``:`` (``:45`` nunca casa ``:145``)."""
    if not isinstance(key, str):
        return False
    guild, sep, member = key.rpartition(":")
    return bool(sep and guild) and member == str(user_id)


def is_snowflake(text: str) -> bool:
    """Só dígitos ASCII, 17 a 20, dentro de 64 bits."""
    if not isinstance(text, str) or not (text.isascii() and text.isdigit()):
        return False
    return SNOWFLAKE_MIN_DIGITS <= len(text) <= SNOWFLAKE_MAX_DIGITS and int(text) < SNOWFLAKE_LIMIT


def parse_owner_request(content: str, prefix: str) -> str | None:
    """Comando do dono (§3.4): ``<prefixo padrão>apagardados <id>``, casamento exato (maiúsculas contam, nada de alias).

    ``None`` = a mensagem não é este comando (ignorar em silêncio): outro prefixo, outro nome (``apagardadosx``) ou um separador que não
    seja espaço ou tabulação ASCII (NBSP e afins não separam). Texto = o argumento ainda por validar, SEM aparar mais que espaço e tabulação:
    uma segunda linha ou um caractere estranho ficam nele e ``owner_target_id`` os recusa.
    """
    head = f"{prefix}{OWNER_COMMAND_NAME}"
    if not prefix or not content.startswith(head):
        return None
    rest = content[len(head) :]
    if rest and rest[0] not in " \t":
        return None
    return rest.strip(" \t")


def owner_target_id(argument: str) -> int | None:
    """ID inteiro positivo, só dígitos ASCII, snowflake de 17 a 20 dígitos, sem zero à esquerda; senão ``None``."""
    if not is_snowflake(argument) or argument[0] == "0":
        return None
    return int(argument)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: float = 0.0
    reason: str = ""


def _recent(stamps: Sequence[float], now: float) -> list[float]:
    return [stamp for stamp in stamps if now - stamp < RATE_WINDOW_S]


def _wait(stamps: list[float], limit: int, now: float) -> float:
    """Segundos até a contagem cair abaixo de ``limit``; 0 se já está abaixo."""
    if len(stamps) < limit:
        return 0.0
    ordered = sorted(stamps)
    return max(0.0, ordered[len(ordered) - limit] + RATE_WINDOW_S - now)


def allow_erasure(ok: Sequence[float], tries: Sequence[float], global_ok: Sequence[float], now: float) -> Decision:
    """Limites por hora. ``ok`` e ``tries`` são do usuário: só ``OK`` entra em ``ok``; toda tentativa entra em ``tries`` (``PARTIAL`` só aqui).

    Passou de um limite: ``allowed=False`` e o tempo até todos os limites estourados abrirem.
    """
    checks = (
        ("user_ok", _wait(_recent(ok, now), ERASE_OK_PER_HOUR, now)),
        ("user_tries", _wait(_recent(tries, now), ERASE_TRIES_PER_HOUR, now)),
        ("global", _wait(_recent(global_ok, now), ERASE_GLOBAL_PER_HOUR, now)),
    )
    blocked = [(reason, wait) for reason, wait in checks if wait > 0]
    if not blocked:
        return Decision(True)
    reason, wait = max(blocked, key=lambda item: item[1])
    return Decision(False, retry_after_s=wait, reason=reason)


# Os campos de ``global.users`` com dado do membro (caminho com ponto) e o valor padrão do modelo. Um teste confere contra ``global_db_models``.
USER_FIELD_DEFAULTS: Mapping[str, Any] = {
    "custom_prefix": "",
    "fav_links": {},
    "integration_links": {},
    "token": "",
    "last_tracks": [],
    "lastfm.username": "",
    "lastfm.sessionkey": "",
}
_LASTFM_SCROBBLE = "lastfm.scrobble"


def _get_path(doc: Mapping[str, Any], path: str) -> Any:
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return USER_FIELD_DEFAULTS[path]
        node = node[part]
    return node


def _digest(user_id: int, path: str, value: Any, salt: bytes) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(salt, f"{user_id}\0{path}\0{body}".encode(), hashlib.sha256).hexdigest()


def fingerprint(user_id: int, doc: Mapping[str, Any], salt: bytes) -> Mapping[str, str]:
    """``caminho -> hash salgado`` dos campos de ``global.users`` com valor diferente do padrão (o valor nunca sai daqui)."""
    prints: dict[str, str] = {}
    for path, default in USER_FIELD_DEFAULTS.items():
        value = _get_path(doc, path)
        if value != default:
            prints[path] = _digest(user_id, path, value, salt)
    return prints


def resurrected_paths(user_id: int, doc: Mapping[str, Any], fp: Mapping[str, str], salt: bytes) -> list[str]:
    """Caminhos cujo valor agora é igual ao apagado (ressurreição). Valor diferente é uso novo do membro e não entra."""
    found: list[str] = []
    for path, expected in fp.items():
        value = _get_path(doc, path)
        if value != USER_FIELD_DEFAULTS[path] and hmac.compare_digest(_digest(user_id, path, value, salt), expected):
            found.append(path)
    return found


def reset_to_default(doc: Mapping[str, Any], paths: Sequence[str]) -> dict[str, Any]:
    """Cópia de ``doc`` com ``paths`` no padrão. Sem a chave do Last.fm o ``scrobble`` não tem sentido e também volta ao padrão."""
    new: dict[str, Any] = json.loads(json.dumps(doc, default=str))
    targets = list(paths) + ([_LASTFM_SCROBBLE] if "lastfm.sessionkey" in paths else [])
    for path in targets:
        *parents, leaf = path.split(".")
        node = new
        for part in parents:
            child = node.get(part)
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        node[leaf] = False if path == _LASTFM_SCROBBLE else json.loads(json.dumps(USER_FIELD_DEFAULTS[path]))
    return new


def only_defaults(doc: Mapping[str, Any]) -> bool:
    """Verdadeiro se o documento não guarda nada do membro (só o padrão)."""
    if any(_get_path(doc, path) != default for path, default in USER_FIELD_DEFAULTS.items()):
        return False
    lastfm = doc.get("lastfm")
    return not (isinstance(lastfm, Mapping) and lastfm.get("scrobble"))


class ErasureStatus(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class StoreResult:
    count: int = 0
    ok: bool = True


@dataclass(frozen=True)
class ErasureReport:
    status: ErasureStatus
    counts: Mapping[str, int]
    failed: tuple[str, ...] = ()


def summarize(results: Mapping[str, StoreResult]) -> ErasureStatus:
    return ErasureStatus.OK if all(result.ok for result in results.values()) else ErasureStatus.PARTIAL


def build_report(results: Mapping[str, StoreResult]) -> ErasureReport:
    return ErasureReport(
        status=summarize(results),
        counts={key: result.count for key, result in results.items()},
        failed=tuple(key for key, result in results.items() if not result.ok),
    )


GRANT_LOG_REF_CHARS = 12


def grant_log_ref(grant_id: str) -> str:
    """Referência curta da concessão para o log: hash do código de resgate, nunca o código. Emissão e apagamento usam esta mesma função."""
    return hashlib.sha256(grant_id.encode("utf-8")).hexdigest()[:GRANT_LOG_REF_CHARS]
