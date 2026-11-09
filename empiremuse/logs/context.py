"""Contexto do log (item 7): bot e servidor da tarefa atual, e o nível por servidor (``LOG_DEBUG_GUILDS``)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from empiremuse.logs.redact import sanitize_value

OWN_LOGGER = "empiremuse"
# Um snowflake do Discord cabe em 19 dígitos; mais que isso nunca é um ID (e o int() recusa texto de mais de 4300 dígitos).
MAX_GUILD_ID_DIGITS = 20

log_bot: ContextVar[str | None] = ContextVar("log_bot", default=None)
log_guild: ContextVar[int | None] = ContextVar("log_guild", default=None)


@contextmanager
def log_context(*, bot: str | None = None, guild: int | None = None) -> Iterator[None]:
    """Bot e servidor dos registros emitidos dentro do bloco; o que não vier fica como estava (aninhamento)."""
    tokens = []
    if bot is not None:
        tokens.append((log_bot, log_bot.set(bot)))
    if guild is not None:
        tokens.append((log_guild, log_guild.set(guild)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def guild_extra(value: object) -> dict[str, int]:
    """``extra`` com o servidor para o log: ``{"guild": id}`` se ``value`` é um ID (int ou texto só de dígitos ASCII), senão vazio.

    O ``LevelGate`` só deixa o DEBUG de um servidor de ``LOG_DEBUG_GUILDS`` passar com ``record.guild`` int, e o ``ContextFilter`` só
    aceita int: um ``_id`` de sessão vem como texto, então a conversão fica aqui e um valor inválido nunca levanta.
    """
    if isinstance(value, bool):
        return {}
    if isinstance(value, int):
        return {"guild": value}
    if isinstance(value, str) and len(value) <= MAX_GUILD_ID_DIGITS and value.isascii() and value.isdigit():
        return {"guild": int(value)}
    return {}


class ContextFilter(logging.Filter):
    """Copia o contexto para ``record.bot`` e ``record.guild``; um ``extra`` do próprio registro vence.

    Total: nunca levanta e nunca descarta. O nome do bot é texto livre, então passa pelo saneamento de controles.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            bot = getattr(record, "bot", None)
            if not isinstance(bot, str):
                bot = log_bot.get()
            record.bot = sanitize_value(bot) if bot is not None else None
            guild = getattr(record, "guild", None)
            if isinstance(guild, bool) or not isinstance(guild, int):
                guild = log_guild.get()
            record.guild = guild
        except Exception:
            record.bot = record.guild = None
        return True


class VoiceDependencyNoiseFilter(logging.Filter):
    """Descarta só o aviso do ``Client.__init__`` do disnake de que PyNaCl e/ou dave.py faltam (a voz toca pelo Lavalink).

    Casa pelo logger exato ``disnake.client``, nível WARNING e o fim da mensagem; o resto dos avisos do disnake segue.
    """

    LOGGER = "disnake.client"
    SUFFIX = "not installed, voice will NOT be supported"

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return not (
                record.name == self.LOGGER
                and record.levelno == logging.WARNING
                and record.getMessage().endswith(self.SUFFIX)
            )
        except Exception:
            return True  # na dúvida o registro segue, como no LevelGate


class LevelGate(logging.Filter):
    """Abaixo de ``level`` só passa o DEBUG de um logger nosso, no contexto de um servidor em ``debug_guilds``.

    O DEBUG de bibliotecas fora do ``LIBRARY_LOGGERS`` (``requests``, ``charset_normalizer``...) nunca passa só por estar dentro do
    contexto de um servidor em debug. Registro marcado com ``empiremuse_emit=True`` (auditoria, que também não é agregada) passa sempre.
    Não formata nada: lê o nível, o nome e o servidor do registro.
    """

    def __init__(self, level: int, debug_guilds: frozenset[int]) -> None:
        super().__init__()
        self.level = level
        self.debug_guilds = debug_guilds

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.levelno >= self.level:
                return True
            if getattr(record, "empiremuse_emit", None) is True:
                return (
                    True  # linha de auditoria (``extra={"empiremuse_emit": True}``): LOG_LEVEL=ERROR não apaga o rastro
                )
            if not self.debug_guilds:
                return False
            if record.name != OWN_LOGGER and not record.name.startswith(OWN_LOGGER + "."):
                return False
            guild = getattr(record, "guild", None)
            return isinstance(guild, int) and guild in self.debug_guilds
        except Exception:
            return True  # na dúvida o registro segue: a redação vem depois e nada se perde por um filtro defeituoso
