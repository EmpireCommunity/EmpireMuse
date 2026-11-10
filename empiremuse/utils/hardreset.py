"""``hardreset`` (só dono, só prefixo): apaga o banco do bot inteiro, MENOS o premium (``global.premium``) e as listas de banimento (``global.banned_users`` e ``global.banned_guilds``), e esquece o que está em memória.

A ordem importa: para as rádios (senão o ticker as religa e o runner regrava o estado), apaga, esquece os caches e para as rádios de novo
(uma partida em andamento pode ter terminado no meio). Só os bancos do próprio bot são tocados: ``global``, as sessões e um banco por bot
(o id dele); o cluster pode ter outros e eles não são listados nem apagados.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from empiremuse.utils.db import DBModel
from empiremuse.utils.music import session_store
from empiremuse.utils.radio.premium import GLOBAL_DB, PREMIUM_DB

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

# Sobrevivem: o premium e as DUAS listas de banimento (só ``unbanuser`` e o desban de servidor encerram uma sanção; apagar o banco não pode).
KEEP = frozenset({(GLOBAL_DB, PREMIUM_DB), (GLOBAL_DB, DBModel.banned_users), (GLOBAL_DB, DBModel.banned_guilds)})
SCROBBLES_DB = "scrobbles"  # sempre no banco local, mesmo com o MongoDB (cogs/lastfm.py)


@dataclass(frozen=True)
class HardResetReport:
    tables: int
    documents: int
    session_files: int


def _bot_ids(pool: BotPool) -> list[str]:
    return sorted({str(bot.user.id) for bot in pool.get_all_bots() if getattr(bot, "user", None) is not None})


def _remove_session_files(bot_ids: list[str]) -> int:
    removed = 0
    for bot_id in bot_ids:
        folder = session_store.sessions_dir(bot_id)
        if not folder.is_dir():
            continue
        removed += sum(1 for item in folder.iterdir() if item.is_file())
        shutil.rmtree(folder, ignore_errors=True)
    return removed


async def _stop_radios(pool: BotPool) -> None:
    manager = pool.radio.manager
    await manager.stop_radios(manager.known_radios(), destroy_players=True)


def _forget_memory(pool: BotPool) -> None:
    pool.radio.forget_all()
    pool.servers.forget_all()
    pool.user_prefix_cache.clear()
    pool.guild_prefix_cache.clear()


async def hard_reset(pool: BotPool, *, author_id: int, now: datetime | None = None) -> HardResetReport:
    """Apaga tudo menos ``global.premium`` e os banimentos e devolve as contagens; grava uma linha no log (quem, quando, quantos)."""
    when = now or datetime.now(timezone.utc)
    bot_ids = _bot_ids(pool)
    await _stop_radios(pool)
    local = getattr(pool, "local_database", None)
    databases = ["global", session_store.NEW_COLLECTION, *bot_ids]
    if local is pool.database:  # sem MongoDB: os scrobbles estão no mesmo motor
        databases.append(SCROBBLES_DB)
    dropped = await pool.database.wipe(databases, KEEP)
    if local is not None and local is not pool.database:
        dropped.update(await local.wipe([SCROBBLES_DB], frozenset()))
    files = _remove_session_files(bot_ids)
    _forget_memory(pool)
    await _stop_radios(pool)
    report = HardResetReport(tables=len(dropped), documents=sum(dropped.values()), session_files=files)
    logger.warning(
        "hardreset: autor=%s em=%s tabelas=%d documentos=%d arquivos_de_sessao=%d",
        author_id,
        when.isoformat(timespec="seconds"),
        report.tables,
        report.documents,
        report.session_files,
    )
    return report
