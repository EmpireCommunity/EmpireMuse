"""Um lock por usuário, único no processo (``pool.user_locks``).

As gravações da conta no painel, o apagamento dos dados (slash, painel e dono) e a passada 2 do apagamento usam o MESMO objeto de lock
de um usuário: é o que impede uma gravação concorrente de regravar o que o apagamento acabou de tirar.
"""

from __future__ import annotations

import asyncio
import weakref

USERS_SCOPE = "users"  # o escopo de ``SettingsService.lock(escopo, id)`` que é do usuário


class UserLocks:
    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

    def lock(self, user_id: int) -> asyncio.Lock:
        """O lock do usuário; o mesmo objeto enquanto alguém o usa ou o segura."""
        key = int(user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock
