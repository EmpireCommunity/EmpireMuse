"""Base das rotas de dados (B3): sessão obrigatória, ``Idempotency-Key``, autorização por servidor e o mapa do resultado de
um serviço do painel para o envelope da API (contrato T-456 §4 m4 e m5)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from empiremuse.api.base import AUTH_SESSION, ApiHandler
from empiremuse.api.errors import (
    BOT_UNAVAILABLE,
    CONFLICT,
    FORBIDDEN,
    NOT_FOUND,
    ApiError,
    conflict,
    validation_failed,
)
from empiremuse.api.idempotency import (
    KEY_PATTERN,
    IdempotencyConflict,
    Reply,
    request_fingerprint,
)
from empiremuse.panel.authz import GuildAccess, GuildAccessDenied, ready_bots_with_guild
from empiremuse.panel.services.account_base import AccountContext
from empiremuse.panel.services.base import Formulario, FormContext
from empiremuse.rules.panel_serialize import error_fields, serialize_form

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
STATUS_OK, STATUS_FORBIDDEN, STATUS_CONFLICT, STATUS_INVALID, STATUS_UNAVAILABLE = 200, 403, 409, 422, 503
FIELD_ALIASES = {"acao": "action"}  # o nome que o serviço dá ao campo, no nome que a API recebe


def form_reply(status: int, form: Formulario, scope: str) -> Reply:
    """O resultado de um serviço do painel (``status`` e ``Formulario``) como resposta de sucesso ou erro da API.

    O texto de usuário final (``mensagem``, texto dos erros) nunca sai: só o código, os nomes dos campos e, no
    conflito, a versão que o formulário tem agora.
    """
    if status == STATUS_OK:
        return Reply(200, serialize_form(form, scope))
    if status == STATUS_CONFLICT:
        raise conflict(form.versao)
    if status == STATUS_INVALID:
        raise validation_failed(*(FIELD_ALIASES.get(name, name) for name in error_fields(form)))
    if status == STATUS_FORBIDDEN:
        raise ApiError(403, FORBIDDEN)
    if status == STATUS_UNAVAILABLE:
        raise ApiError(503, BOT_UNAVAILABLE)
    raise ApiError(500, BOT_UNAVAILABLE)  # status que a API não conhece: falha fechada


class DataHandler(ApiHandler):
    auth = AUTH_SESSION

    # ------------------------------------------------------------------ resposta e Idempotency-Key

    def send(self, reply: Reply) -> None:
        self.set_status(reply.status)
        if reply.body is not None:
            self.write(reply.body)

    def query_arguments(self, *names: str) -> dict[str, str]:
        """Os parâmetros de query pedidos, um valor cada (repetido é erro). A query nunca vai para o log da API."""
        found: dict[str, str] = {}
        for name in names:
            values = self.get_query_arguments(name, strip=False)
            if len(values) > 1:
                raise validation_failed(name)
            if values:
                found[name] = values[0]
        return found

    def _idempotency_key(self) -> Optional[str]:
        key = self.request.headers.get(IDEMPOTENCY_HEADER)
        if key is None:
            return None
        if not KEY_PATTERN.fullmatch(key):
            raise validation_failed("idempotency_key")
        return key

    async def run_write(self, body: Any, work: Callable[[], Awaitable[Reply]], *, replayable: bool = True) -> None:
        """Executa uma escrita já autorizada. Com ``Idempotency-Key`` (e ``replayable``), a mesma chave com o mesmo pedido
        repete a resposta de sucesso; com outro pedido, ou com o original ainda em andamento, é ``409``.

        ``replayable=False`` (o token novo do RPC, que só pode sair uma vez) ignora a chave: o bot nunca guarda segredo.
        """
        key = self._idempotency_key() if replayable else None
        if key is None:
            self.send(await work())
            return
        fingerprint = request_fingerprint(self.request.method, self.request.path, body)
        try:
            claim = self.ctx.idempotency.claim(self.session.user.id, key, fingerprint)
        except IdempotencyConflict:
            raise ApiError(409, CONFLICT) from None
        if claim.replay is not None:
            self.set_header(REPLAYED_HEADER, "true")
            self.send(claim.replay)
            return
        reply: Optional[Reply] = None
        try:
            reply = await work()
        finally:
            self.ctx.idempotency.settle(claim.ticket, reply)
        self.send(reply)

    # ------------------------------------------------------------------ contextos (o usuário é sempre o da sessão)

    def account_context(self) -> AccountContext:
        pool = self.ctx.pool
        bot = next((candidate for candidate in pool.bots if candidate.is_ready()), None)
        if bot is None:
            raise ApiError(503, BOT_UNAVAILABLE)
        return AccountContext(pool=pool, bot=bot, user_id=int(self.session.user.id))

    async def guild_access(self, guild_id: str, *, write: bool) -> tuple[FormContext, GuildAccess]:
        """Autoriza o usuário no servidor: ``write`` reconfere agora (``fresh=True``), a leitura aceita o cache de 30 s.

        Servidor negado e inexistente dão o mesmo ``404``.
        """
        try:
            access = await self.ctx.authorizer.authorize(self.session, guild_id, fresh=write)
        except GuildAccessDenied:
            raise ApiError(404, NOT_FOUND) from None
        pool = self.ctx.pool
        pair = next(
            (
                found
                for found in ready_bots_with_guild(pool, int(guild_id))
                if str(found[0].user.id) == access.bots[0].id
            ),
            None,
        )
        if pair is None:
            raise ApiError(404, NOT_FOUND)
        bot, guild = pair
        return FormContext(
            pool=pool, bot=bot, guild=guild, guild_id=int(guild_id), user_id=self.session.user.id
        ), access
