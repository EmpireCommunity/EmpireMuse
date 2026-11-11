"""Apagar dados pela API (B5, contrato T-456 §3.5, §4 "Apagar dados", §11.1; ``apagar-dados-contrato.md`` §3 e §12).

Duas rotas, as duas sempre sobre o usuário da sessão (um ``user_id`` de corpo ou caminho nunca é lido):

* ``POST /account/erasure/prepare``: confere o login recente e emite o ``confirm_token`` (60 s, uso único, preso a usuário, sessão e
  finalidade) com o resumo do que sai e do que fica (códigos estáveis; o texto é do painel);
* ``POST /account/erasure {confirm_token, typed}``: confere de novo o login, o token e a palavra e chama **o mesmo serviço** que o slash e o
  painel genérico chamam (``AccountEraseService.run`` → ``pool.erasure.erase_user``). Nunca há uma segunda implementação.

Este serviço **não segura o lock do usuário** (``erase_user`` o pega sozinho; ``asyncio.Lock`` não é reentrante) e **não usa**
``DataHandler.run_write``: o apagamento destrói a sessão do pedido por desenho, e a resposta de sucesso ainda tem de sair. A
``Idempotency-Key`` é ignorada (o ``confirm_token`` de uso único já impede a repetição); a limpeza das respostas guardadas do usuário
vem do ouvinte ``purge_user`` do ``SessionStore``, que o apagamento aciona. O ``confirm_token`` é gasto **antes** da chamada: ``BUSY`` e
``429`` pedem outro ``prepare``. Quando a rota responde, o painel deve destruir a sessão dele (§9e).
"""

from __future__ import annotations

import hashlib
from typing import Any

from empiremuse.api.data import DataHandler
from empiremuse.api.errors import BOT_UNAVAILABLE, REAUTH_REQUIRED, ApiError, rate_limited, validation_failed
from empiremuse.panel.services.account_erase import CONFIRMATION_WORD, AccountEraseService, EraseOutcome, EraseResult
from empiremuse.panel.services.tokens import TOKEN_TTL_SECONDS
from empiremuse.rules.erasure import login_is_recent

ERASURE_SCOPE = "erasure"  # a finalidade do token (``SignedTokens`` a confere); a subchave também é só do apagamento
NO_GUILD = 0  # o apagamento não é de servidor
MAX_TOKEN_CHARS = 512  # um token de verdade tem pouco mais de 130 caracteres; o teto barra lixo antes do HMAC
MAX_TYPED_CHARS = 64
STATUS_OK, STATUS_PARTIAL, STATUS_BUSY = "OK", "PARTIAL", "BUSY"
# As palavras que a API aceita em ``typed``: a do painel em português e a do painel em inglês (a tela escolhe pelo idioma da página; o bot aceita as
# duas em qualquer idioma). SÓ da API: o painel genérico e o slash seguem com ``CONFIRMATION_WORD``. Conferência EXATA (sem ``casefold`` nem ``strip``).
CONFIRMATION_WORDS = frozenset({CONFIRMATION_WORD, "DELETE"})

# O resumo é só código estável (o texto de usuário final é do painel, §4 erro): o que o apagamento remove e o que fica.
SUMMARY = {
    "erases": ["preferences", "lastfm_and_scrobbles", "saved_queue", "radio_favorites", "radio_editor_ids", "sessions"],
    "keeps": [
        "access_and_security_logs",
        "live_player_records",
        "server_settings_and_statistics",
        "lastfm_remote_data",
    ],
    "backups_up_to_weeks": 4,
    "confirmation_word": CONFIRMATION_WORD,
}


def session_binding(session_token: str) -> str:
    """O que prende o ``confirm_token`` a UMA sessão: um resumo do token de sessão (sem ponto, que o ``SignedTokens`` usa como
    separador). A reautenticação troca o token da sessão, então o ``confirm_token`` do token antigo morre junto."""
    return hashlib.sha256(f"erasure-session/{session_token}".encode("utf-8")).hexdigest()[:32]


class ErasureHandler(DataHandler):
    """Base das duas rotas: confere o login recente e o serviço antes de qualquer coisa que gaste ou emita token."""

    def require_recent_login(self) -> None:
        """Idade do login = ``now - Session.created_at`` no relógio do store (§3.5 r4-E1); ``consented_at`` nunca entra."""
        if not login_is_recent(self.session.created_at, self.ctx.clock()):
            raise ApiError(401, REAUTH_REQUIRED)

    def require_service(self) -> None:
        if getattr(self.ctx.pool, "erasure", None) is None:
            raise ApiError(503, BOT_UNAVAILABLE)  # sem serviço de apagamento neste processo: nada é emitido nem apagado


class ErasurePrepareHandler(ErasureHandler):
    async def post(self) -> None:
        self.require_recent_login()
        self.require_service()
        token = self.ctx.erase_tokens.issue(
            self.session.user.id, NO_GUILD, ERASURE_SCOPE, session_binding(self.session_token)
        )
        self.write({"confirm_token": token, "expires_in": TOKEN_TTL_SECONDS, "summary": SUMMARY})


class ErasureRunHandler(ErasureHandler):
    async def post(self) -> None:
        confirm_token, typed = self._parse(self.json_body())
        self.require_recent_login()
        self.require_service()
        # A sessão é lida AQUI, uma vez: o apagamento destrói todas as sessões do usuário e depois dele ela não é consultada.
        user_id = int(self.session.user.id)
        tokens = self.ctx.erase_tokens
        binding = session_binding(self.session_token)
        if tokens.check(confirm_token, str(user_id), NO_GUILD, ERASURE_SCOPE, binding) is not None:
            raise validation_failed(
                "confirm_token"
            )  # expirado, usado, de outra sessão, usuário ou finalidade: não se distingue
        if typed not in CONFIRMATION_WORDS:
            raise validation_failed("typed")  # depois do token; o token fica valendo: errar a palavra não o gasta
        tokens.consume(
            confirm_token
        )  # sem ``await`` entre o ``check`` e o ``consume``: dois pedidos não gastam o mesmo token
        outcome = await AccountEraseService().run(self.ctx.pool, user_id=user_id)
        self._respond(outcome)

    @staticmethod
    def _parse(body: dict[str, Any]) -> tuple[str, str]:
        """Tipo e tamanho antes de qualquer ``SignedTokens.check`` (texto não ASCII ou não texto = ``400``, nunca ``TypeError``)."""
        token, typed = body.get("confirm_token"), body.get("typed")
        bad = [
            name
            for name, value, limit in (("confirm_token", token, MAX_TOKEN_CHARS), ("typed", typed, MAX_TYPED_CHARS))
            if not isinstance(value, str) or not value or len(value) > limit
        ]
        if not bad and not token.isascii():  # type: ignore[union-attr]
            bad.append("confirm_token")
        if bad:
            raise validation_failed(*bad)
        return token, typed  # type: ignore[return-value]

    def _respond(self, outcome: EraseOutcome) -> None:
        if outcome.result is EraseResult.RATE_LIMITED:
            raise rate_limited(outcome.retry_after_s)
        if outcome.result is EraseResult.UNAVAILABLE:
            raise ApiError(503, BOT_UNAVAILABLE)
        if outcome.result is EraseResult.BUSY:
            self.write({"status": STATUS_BUSY})
            return
        self.write({"status": STATUS_OK if outcome.result is EraseResult.DONE else STATUS_PARTIAL})


def erasure_routes() -> list[tuple[str, Any, str]]:
    """(caminho sob o prefixo, handler, caminho do OpenAPI)."""
    return [
        ("/account/erasure/prepare", ErasurePrepareHandler, "/account/erasure/prepare"),
        ("/account/erasure", ErasureRunHandler, "/account/erasure"),
    ]
