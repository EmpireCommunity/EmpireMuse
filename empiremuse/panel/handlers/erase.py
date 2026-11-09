"""Apagar meus dados: ``GET/POST /painel/conta/apagar`` (E3, contrato ``apagar-dados-contrato.md`` §3.2).

Só HTTP: a conferência e a chamada ao serviço de apagamento ficam em ``panel/services/account_erase.py``. O alvo é SEMPRE o usuário da sessão
(um ``user_id`` no corpo ou na URL nunca é lido). Todas as respostas saem com ``Cache-Control: no-store``. O XSRF é do Tornado (``xsrf_cookies``).

Costura com o template ``conta_apagar.html`` (do Vitral): este handler renderiza essa página, GET e POST, com estas variáveis:

* ``resultado``: ``None`` (formulário), ``"ok"`` ou ``"partial"`` (já apagou; a sessão acabou e o topo sai deslogado) ou ``"erro"`` (nada foi apagado);
* ``mensagem``: texto pronto para mostrar (vazio no formulário inicial); nunca leva contagem, valor nem nome de armazenamento;
* ``palavra_confirmacao``: o texto a digitar (``APAGAR``); o formulário é um ``POST`` a ``/painel/conta/apagar`` com o campo ``confirmacao`` e
  ``{% module xsrf_form_html() %}``;
* ``login_recente``: ``False`` quando não há login com consentimento nos últimos 30 min (login silencioso ou antigo): o template NÃO mostra o formulário, só o aviso e o link para
  ``/painel/login?next=/painel/conta/apagar`` (o POST com login antigo responde 303 de volta ao GET e não apaga nada).
"""

from __future__ import annotations

from typing import Optional

from empiremuse.panel.handlers.servers import MAX_BODY_BYTES, AuthenticatedHandler, decode_args
from empiremuse.panel.services.account_erase import (
    CONFIRMATION_WORD,
    AccountEraseService,
    EraseOutcome,
    EraseResult,
)

ERASE_PATH = "/painel/conta/apagar"
PAGE_TEMPLATE = "conta_apagar.html"
CONFIRMATION_FIELD = "confirmacao"
RETRY_AFTER_WRITE_LIMIT_S = "60"
_MSG_RATE = "Muitas alterações em pouco tempo. Aguarde um minuto e tente novamente."
_TOO_LARGE = ("Envio grande demais", "Os dados enviados excedem o limite permitido.")

_RESULT_NAME = {EraseResult.DONE: "ok", EraseResult.PARTIAL: "partial"}
_ERROR_STATUS = {
    EraseResult.BAD_CONFIRMATION: 422,
    EraseResult.RATE_LIMITED: 429,
    EraseResult.BUSY: 503,
    EraseResult.UNAVAILABLE: 503,
}


class AccountEraseHandler(AuthenticatedHandler):
    @property
    def erasure(self) -> AccountEraseService:
        return self.application.settings["account_erase_service"]

    def set_default_headers(self) -> None:
        super().set_default_headers()
        self.set_header("Cache-Control", "no-store")  # também no redirecionamento e nos erros

    def _page(self, status: int, resultado: Optional[str], mensagem: str, login_recente: bool) -> None:
        self.set_status(status)
        self.render_page(
            PAGE_TEMPLATE,
            resultado=resultado,
            mensagem=mensagem,
            palavra_confirmacao=CONFIRMATION_WORD,
            login_recente=login_recente,
        )

    def get(self) -> None:
        self._page(200, None, "", self.erasure.consent_is_recent(self.session.consented_at))

    async def post(self) -> None:  # type: ignore[override]
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_TOO_LARGE)
            return
        if not self._write_allowed():
            self.set_header("Retry-After", RETRY_AFTER_WRITE_LIMIT_S)
            self.render_error(429, "Muitas requisições", _MSG_RATE)
            return
        # A sessão é lida AQUI, uma vez: o apagamento destrói todas as sessões do membro e depois dele ela não é consultada.
        user_id, consented_at = int(self.session.user.id), self.session.consented_at
        confirmation = (decode_args(self).get(CONFIRMATION_FIELD) or [""])[0]
        try:
            outcome = await self.erasure.erase(
                self.application.settings["pool"], user_id=user_id, consented_at=consented_at, confirmation=confirmation
            )
        except Exception:
            self.forget_session()  # o passo 2 pode ter destruído a sessão antes do erro
            raise
        self._respond(outcome)

    def _respond(self, outcome: EraseOutcome) -> None:
        if outcome.result is EraseResult.LOGIN_REQUIRED:
            # 303 de volta ao GET: ele não oferece formulário, só o link para entrar de novo (um POST seguido de 302 ao Discord
            # esbarraria no ``form-action 'self'`` da CSP, e o login silencioso não prova presença).
            self.redirect(ERASE_PATH, status=303)
            return
        if outcome.erased:
            self.forget_session()
            self._page(200, _RESULT_NAME[outcome.result], outcome.message, False)
            return
        if outcome.retry_after_s:
            self.set_header("Retry-After", str(outcome.retry_after_s))
        self._page(_ERROR_STATUS[outcome.result], "erro", outcome.message, True)
