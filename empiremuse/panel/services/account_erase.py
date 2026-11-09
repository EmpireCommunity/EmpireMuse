"""Apagar meus dados no painel (E3): confere o pedido e chama o serviço de apagamento do bot.

Contrato ``apagar-dados-contrato.md`` §3.2. Este serviço **não segura o lock do usuário** em volta de ``erase_user``: o lock
(``pool.user_locks``) é um ``asyncio.Lock`` não reentrante e ``erase_user`` o pega sozinho; segurá-lo aqui travaria na primeira chamada.
Por isso a conferência do pedido (login recente e palavra ``APAGAR``) acontece antes da chamada, fora do lock; o XSRF é do Tornado.

Quem chama lê ``user_id`` e ``created_at`` da sessão **antes** (o passo 2 do apagamento destrói todas as sessões do membro) e não consulta
a sessão depois. Os textos daqui nunca mostram contagens nem nomes de armazenamento e o de ``PARTIAL`` não promete que repetir resolve.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from math import ceil
from typing import TYPE_CHECKING, Optional

from empiremuse.rules.erasure import ERASE_MAX_SESSION_AGE_S, ErasureStatus
from empiremuse.utils.erasure import ErasureBusy, ErasureRateLimited

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

CONFIRMATION_WORD = "APAGAR"
SECONDS_PER_MINUTE = 60

MSG_BAD_CONFIRMATION = f"Para apagar, digite {CONFIRMATION_WORD} (em maiúsculas) no campo de confirmação."
MSG_BUSY = "O serviço está ocupado agora. Tente novamente em instantes."
MSG_UNAVAILABLE = "Não foi possível apagar seus dados agora. Tente novamente em instantes."
MSG_PARTIAL = (
    "Apagamos parte dos seus dados, mas nem tudo pôde ser apagado agora. "
    "Sua sessão no painel foi encerrada. Se precisar, procure o suporte do bot."
)
MSG_OK = "Seus dados foram apagados e sua sessão no painel foi encerrada."


def msg_rate_limited(retry_after_s: float) -> str:
    minutes = max(1, ceil(retry_after_s / SECONDS_PER_MINUTE))
    return f"Você já fez pedidos demais por agora. Tente de novo em cerca de {minutes} min."


class EraseResult(StrEnum):
    DONE = "done"  # OK: nada sobrou
    PARTIAL = "partial"  # parte apagada; o texto não promete que repetir resolve
    LOGIN_REQUIRED = "login_required"  # sessão antiga: o handler redireciona ao login, nada foi apagado
    BAD_CONFIRMATION = "bad_confirmation"
    RATE_LIMITED = "rate_limited"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"  # sem serviço de apagamento no pool: nada foi apagado


@dataclass(frozen=True)
class EraseOutcome:
    result: EraseResult
    message: str = ""
    retry_after_s: int = 0

    @property
    def erased(self) -> bool:
        """Verdadeiro quando houve execução (``OK`` ou ``PARTIAL``): as sessões do membro já foram destruídas."""
        return self.result in (EraseResult.DONE, EraseResult.PARTIAL)


class AccountEraseService:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock  # o mesmo relógio do ``SessionStore``: ``created_at`` vem dele

    def login_is_recent(self, created_at: float) -> bool:
        return self._clock() - created_at <= ERASE_MAX_SESSION_AGE_S

    async def erase(
        self, pool: BotPool, *, user_id: int, created_at: float, confirmation: Optional[str]
    ) -> EraseOutcome:
        """Confere login recente e a palavra de confirmação, e só então apaga. ``user_id`` é sempre o da sessão."""
        if not self.login_is_recent(created_at):
            return EraseOutcome(EraseResult.LOGIN_REQUIRED)
        if confirmation != CONFIRMATION_WORD:
            return EraseOutcome(EraseResult.BAD_CONFIRMATION, MSG_BAD_CONFIRMATION)
        service = getattr(pool, "erasure", None)
        if service is None:
            return EraseOutcome(EraseResult.UNAVAILABLE, MSG_UNAVAILABLE)
        try:
            report = await service.erase_user(
                user_id, source="panel"
            )  # sem lock aqui: ``erase_user`` serializa sozinho
        except ErasureRateLimited as exc:
            retry = max(1, ceil(exc.retry_after_s))
            return EraseOutcome(EraseResult.RATE_LIMITED, msg_rate_limited(exc.retry_after_s), retry)
        except ErasureBusy:
            return EraseOutcome(EraseResult.BUSY, MSG_BUSY, SECONDS_PER_MINUTE)
        if report.status is ErasureStatus.OK:
            return EraseOutcome(EraseResult.DONE, MSG_OK)
        return EraseOutcome(EraseResult.PARTIAL, MSG_PARTIAL)
