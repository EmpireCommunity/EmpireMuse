"""Gravação do documento do usuário (``global.users``) sob ``pool.user_locks``, o mesmo lock do apagamento de dados.

Quem altera o documento inteiro passa por ``change_user_doc``: a cópia é RELIDA dentro do lock, a mudança é uma função síncrona
(não há como esperar a rede com o lock tomado) e nada é gravado se o apagamento rodou depois de ``read_at``. Regras de quem chama:
nunca chamar com o lock do usuário já tomado (``asyncio.Lock`` não é reentrante), nunca chamar ``erase_user`` de dentro, e fazer
toda consulta externa (cota, perfil, Last.fm) ANTES; a resposta ao Discord vai DEPOIS.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, NamedTuple, Optional, Union

from empiremuse.utils.db import DBModel


class Skip(NamedTuple):
    """Devolvido por ``change``: nada a gravar (a cópia relida é descartada). ``reason`` segue para ``UserDocChange.result``."""

    reason: Any = None


SKIP = Skip()


class UserDocChange(NamedTuple):
    erased: bool  # o apagamento rodou depois de ``read_at`` (ou a leitura é velha demais): nada foi lido nem gravado
    saved: bool  # a cópia alterada foi gravada
    doc: Optional[dict]  # a cópia relida (com a mudança, se gravada); ``None`` quando ``erased``
    result: Any  # o que ``change`` devolveu (ou o ``reason`` do ``Skip``); ``None`` quando ``erased``


async def change_user_doc(
    bot: Any, user_id: int, change: Callable[[dict], Any], *, read_at: Optional[float]
) -> UserDocChange:
    """Relê o documento de ``user_id`` sob o lock dele, aplica ``change`` na cópia e grava.

    ``read_at`` é ``pool.erasure_tombstones.now()`` capturado no começo da ação do membro (antes de qualquer leitura ou espera);
    ``None`` (hora desconhecida) falha fechado. ``change(doc)`` é síncrona, altera a cópia no lugar e devolve o que quiser; se
    devolver ``SKIP`` ou ``Skip(motivo)`` nada é gravado. O chamador atualiza caches e responde ao membro depois, fora do lock.

    Uma exceção de ``change``, da leitura ou da gravação sobe para o chamador com o lock já solto. Se foi ``change`` ou a leitura
    que levantou, nada foi gravado; se foi a gravação, vale o que o banco disser (a cópia alterada é descartada).
    """
    tombstones = bot.pool.erasure_tombstones
    async with bot.pool.user_locks.lock(user_id):
        if tombstones.stale(user_id, read_at):
            return UserDocChange(True, False, None, None)
        doc = copy.deepcopy(await bot.get_global_data(user_id, db_name=DBModel.users))
        result = change(doc)
        if isinstance(result, Skip):
            return UserDocChange(False, False, doc, result.reason)
        await bot.update_global_data(user_id, doc, db_name=DBModel.users)
    return UserDocChange(False, True, doc, result)


def normalize_integration_links(doc: dict) -> Optional[Skip]:
    """``change`` de ``change_user_doc``: integrações no formato antigo (``{nome: url}``) viram ``{nome: {url, avatar}}``."""
    links = doc["integration_links"]
    old = [name for name, value in links.items() if not isinstance(value, dict)]
    for name in old:
        links[name] = {"url": links[name], "avatar": None}
    return None if old else SKIP


def toggle_lastfm_scrobble(doc: dict) -> Union[Skip, dict]:
    """``change`` de ``change_user_doc``: inverte o scrobble de quem tem o Last.fm vinculado e devolve ``doc["lastfm"]``.

    Sem vínculo (sem chave de sessão) nada é gravado.
    """
    lastfm = doc["lastfm"]
    if not lastfm["sessionkey"]:
        return SKIP
    lastfm["scrobble"] = not lastfm["scrobble"]
    return lastfm
