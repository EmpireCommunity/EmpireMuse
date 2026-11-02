"""Configuração das rádios pelo painel (contrato-nomes-radios §4.3): tipo, canal, bot reserva e redefinir.

Uma função só para o Discord e o painel, do Turbine em ``utils/radio/setup.py``, exposta pelo hub como
``pool.radio.setup``: ``options(guild_id)`` (os bots reserva livres e os canais de voz/palco que valem),
``configure(guild_id, actor_id, *, radio_id, channel_id, bot_id, kind, rev)`` e ``reset(guild_id, actor_id, radio_id, rev)``.
As checagens (premium, ``manage_guild`` de quem faz, só reserva livre, uma rádio por bot, máximo 3, canal de voz ou
palco que não é o da Jukebox e onde o bot conecta e fala, rádio ligada exige desligar antes, CAS em ``rev``) rodam lá;
aqui ficam só a forma dos campos (allowlist ``kind``, ``channel_id``, ``bot_id``), a versão otimista, o token da
confirmação do redefinir e a tradução das recusas para o campo certo: ``RadioNotFound`` 404, ``RadioForbidden`` 403,
``RadioConflict`` 409, ``RadioInvalid`` 422 (``.error.key`` é o campo). Nada do usuário vai para o log além dos ids.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, NamedTuple, Optional

from empiremuse.panel.services.base import (
    CONFLICT_TEXT,
    INVALID_TEXT,
    KIND_ERROR,
    KIND_OK,
    SAVED_TEXT,
    VERSION_TEXT,
    Args,
    FormContext,
    Formulario,
    first,
)
from empiremuse.panel.services.radio import CATEGORY, REV
from empiremuse.panel.services.service import STATUS_CONFLICT, STATUS_INVALID, STATUS_OK
from empiremuse.panel.services.tokens import SignedTokens
from empiremuse.rules.i18n import Translator
from empiremuse.rules.radio import KINDS
from empiremuse.utils.radio.setup import RadioForbidden
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

logger = logging.getLogger(__name__)

FIELDS = ("kind", "channel_id", "bot_id")
ACTION_RESET = "reiniciar"
ACTION_CONFIRM = "confirmar_reiniciar"
NEW_PATH = "nova"
CONFIG_PATH = "config"
RESET_PATH = "reset"
TOKEN_SCOPE = "radio_reset"
STATUS_FORBIDDEN = 403
STATUS_NOT_FOUND = 404
_SNOWFLAKE = re.compile(r"[0-9]{1,20}", re.ASCII)
_ERROR_CODES = frozenset(
    {
        "reserve_only",
        "bot_in_call",
        "one_per_bot",
        "max_radios",
        "channel_kind",
        "jukebox_channel",
        "cannot_connect",
        "radio_on",
    }
)
MSG_TOKEN_KEY = "panel.radio.setup.token"


class SetupOutcome(NamedTuple):
    status: int
    form: Optional[Formulario]  # ``None`` só no 404
    radio: Any = None


class ResetView(NamedTuple):
    """O bloco de redefinir de uma rádio: o passo 1 (sem token) ou a confirmação (com token)."""

    radio_id: str
    slug: str
    acao: str
    versao: str
    bot_nome: str
    canal_nome: str
    ligada: bool
    token: Optional[str] = None
    mensagem: Optional[str] = None
    mensagem_tipo: Optional[str] = None
    removida: bool = False


def slug_of(radio_id: str) -> str:
    return str(radio_id).replace(":", "-")


class RadioSetupService:
    def __init__(self, secret: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.tokens = SignedTokens(secret, clock)

    @staticmethod
    def api(ctx: FormContext) -> Any:
        return ctx.pool.radio.setup

    async def options(self, ctx: FormContext) -> Any:
        return await self.api(ctx).options(ctx.guild_id)

    # ------------------------------------------------------------------ formulário

    @staticmethod
    def form(
        radio: Any,
        action: str,
        options: Any,
        *,
        current_bot: Optional[tuple[int, str]] = None,
        values: Optional[dict[str, Any]] = None,
        errors: Optional[dict[str, str]] = None,
        message: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> Formulario:
        """O formulário de criar (``radio`` None) ou de configurar uma rádio; as listas vêm do bot (``options``).
        Numa rádio existente o bot atual entra na lista mesmo ocupado (ocupado por ela mesma)."""
        bots = tuple((int(bot_id), str(name)) for bot_id, name in options.bots)
        if current_bot is not None and current_bot[0] not in {b for b, _ in bots}:
            bots = (current_bot,) + bots
        channels = tuple((int(cid), str(name), str(ckind)) for cid, name, ckind in options.channels)
        current = (
            {"kind": "", "channel_id": "", "bot_id": ""}
            if radio is None
            else {"kind": str(radio.kind), "channel_id": str(radio.channel_id), "bot_id": str(radio.bot_id)}
        )
        slug = NEW_PATH if radio is None else slug_of(radio.id)
        return Formulario(
            key="radio_setup",
            form_id=f"form-radio-config-{slug}",
            template="form_radio_setup.html",
            acao=action,
            versao="" if radio is None else str(radio.rev),
            valores={**current, **(values or {})},
            erros=errors or {},
            mensagem=message,
            mensagem_tipo=kind,
            extras={
                "bots": bots,
                "channels": channels,
                "kinds": KINDS,
                "nova": radio is None,
                "ligada": bool(radio is not None and radio.enabled),
                "slug": slug,
                "radio_id": "" if radio is None else str(radio.id),
            },
        )

    @staticmethod
    def parse(args: Args, tr: Translator) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
        """Só ``kind``, ``channel_id`` e ``bot_id`` entram (allowlist); forma aqui, regra no bot."""
        new: dict[str, Any] = {}
        errors: dict[str, str] = {}
        echo: dict[str, str] = {}
        for name in FIELDS:
            raw = (first(args, name) or "").strip()
            echo[name] = raw
            if name == "kind":
                if raw in KINDS:
                    new[name] = raw
                else:
                    errors[name] = tr(f"panel.radio.setup.error.{name}")
            elif _SNOWFLAKE.fullmatch(raw):
                new[name] = int(raw)
            else:
                errors[name] = tr(f"panel.radio.setup.error.{name}")
        return new, errors, echo

    # ------------------------------------------------------------------ criar e configurar

    async def configure(
        self,
        ctx: FormContext,
        radio: Any,
        args: Args,
        action: str,
        tr: Translator,
        *,
        current_bot: Optional[tuple[int, str]] = None,
    ) -> SetupOutcome:
        options = await self.options(ctx)

        def form(**kwargs: Any) -> Formulario:
            return self.form(radio, action, options, current_bot=current_bot, **kwargs)

        if radio is not None:
            sent = first(args, "versao")
            if sent is None or not REV.fullmatch(sent):
                return SetupOutcome(STATUS_INVALID, form(message=VERSION_TEXT, kind=KIND_ERROR), radio)
            if int(sent) != int(radio.rev):
                return SetupOutcome(STATUS_CONFLICT, form(message=CONFLICT_TEXT, kind=KIND_ERROR), radio)
        new, errors, echo = self.parse(args, tr)
        if errors:
            return SetupOutcome(
                STATUS_INVALID, form(values=echo, errors=errors, message=INVALID_TEXT, kind=KIND_ERROR), radio
            )
        try:
            saved = await self.api(ctx).configure(
                ctx.guild_id,
                int(ctx.user_id),
                radio_id=None if radio is None else str(radio.id),
                channel_id=new["channel_id"],
                bot_id=new["bot_id"],
                kind=new["kind"],
                rev=None if radio is None else int(radio.rev),
            )
        except RadioNotFound:
            return SetupOutcome(STATUS_NOT_FOUND, None, radio)
        except RadioForbidden as exc:
            message = tr(f"panel.radio.setup.forbidden.{_reason(exc)}")
            return SetupOutcome(STATUS_FORBIDDEN, form(values=echo, message=message, kind=KIND_ERROR), radio)
        except RadioConflict:
            return SetupOutcome(STATUS_CONFLICT, form(values=echo, message=CONFLICT_TEXT, kind=KIND_ERROR), radio)
        except RadioInvalid as exc:
            field, text = _refusal(exc, tr)
            form_ = form(values=echo, errors={field: text}, message=INVALID_TEXT, kind=KIND_ERROR)
            return SetupOutcome(STATUS_INVALID, form_, radio)
        self._audit(ctx, saved, "criada" if radio is None else "configurada")
        message = tr("panel.radio.setup.created") if radio is None else SAVED_TEXT
        done = self.form(
            None if radio is None else saved, action, await self.options(ctx), message=message, kind=KIND_OK
        )
        return SetupOutcome(STATUS_OK, done, saved)

    # ------------------------------------------------------------------ redefinir (dois passos)

    @staticmethod
    def reset_view(
        radio: Any,
        action: str,
        names: tuple[str, str],
        *,
        token: Optional[str] = None,
        message: Optional[str] = None,
        kind: Optional[str] = None,
        removed: bool = False,
    ) -> ResetView:
        return ResetView(
            radio_id=str(radio.id),
            slug=slug_of(radio.id),
            acao=action,
            versao=str(radio.rev),
            bot_nome=names[0],
            canal_nome=names[1],
            ligada=bool(radio.enabled),
            token=token,
            mensagem=message,
            mensagem_tipo=kind,
            removida=removed,
        )

    def confirmation(self, ctx: FormContext, radio: Any, action: str, names: tuple[str, str]) -> ResetView:
        token = self.tokens.issue(ctx.user_id, ctx.guild_id, TOKEN_SCOPE, f"{radio.id}:{radio.rev}")
        return self.reset_view(radio, action, names, token=token)

    async def reset(
        self, ctx: FormContext, radio: Any, args: Args, action: str, names: tuple[str, str], tr: Translator
    ) -> tuple[int, Optional[ResetView]]:
        """``acao=reiniciar`` devolve a confirmação (200, sem efeitos); ``acao=confirmar_reiniciar`` com o token redefine."""
        view = lambda **kw: self.reset_view(radio, action, names, **kw)  # noqa: E731
        sent = first(args, "versao")
        if sent is None or not REV.fullmatch(sent):
            return STATUS_INVALID, view(message=VERSION_TEXT, kind=KIND_ERROR)
        if int(sent) != int(radio.rev):
            return STATUS_CONFLICT, view(message=CONFLICT_TEXT, kind=KIND_ERROR)
        step = first(args, "acao") or ""
        if step == ACTION_RESET:
            return STATUS_OK, self.confirmation(ctx, radio, action, names)
        if step != ACTION_CONFIRM:
            return STATUS_INVALID, view(message=INVALID_TEXT, kind=KIND_ERROR)
        token = first(args, "token") or ""
        status = self.tokens.check(token, ctx.user_id, ctx.guild_id, TOKEN_SCOPE, f"{radio.id}:{radio.rev}")
        if status == "outro":
            return STATUS_CONFLICT, view(message=CONFLICT_TEXT, kind=KIND_ERROR)
        if status is not None:
            return STATUS_INVALID, view(message=tr(MSG_TOKEN_KEY), kind=KIND_ERROR)
        try:
            await self.api(ctx).reset(ctx.guild_id, int(ctx.user_id), str(radio.id), int(radio.rev))
        except RadioNotFound:
            return STATUS_NOT_FOUND, None
        except RadioForbidden as exc:
            return STATUS_FORBIDDEN, view(message=tr(f"panel.radio.setup.forbidden.{_reason(exc)}"), kind=KIND_ERROR)
        except RadioConflict:
            return STATUS_CONFLICT, view(message=CONFLICT_TEXT, kind=KIND_ERROR)
        except RadioInvalid as exc:
            return STATUS_INVALID, view(message=_refusal(exc, tr)[1], kind=KIND_ERROR)
        self.tokens.consume(token)
        self._audit(ctx, radio, "redefinida")
        return STATUS_OK, view(message=tr("panel.radio.setup.reset_done"), kind=KIND_OK, removed=True)

    @staticmethod
    def _audit(ctx: FormContext, radio: Any, what: str) -> None:
        logger.info(  # só ids (R-12): nomes de canal e de bot nunca vão para o log
            "Configuração alterada: usuario=%s servidor=%s escopo=radio:%s categoria=%s campos=%s",
            ctx.user_id,
            ctx.guild_id,
            radio.bot_id,
            CATEGORY,
            what,
        )


def _reason(exc: RadioForbidden) -> str:
    reason = str(getattr(exc, "reason", "") or "")
    return reason if reason in ("premium", "manage_guild") else "manage_guild"


def _refusal(exc: RadioInvalid, tr: Translator) -> tuple[str, str]:
    """(campo, texto) de uma recusa do bot: o campo é ``error.key`` (ou ``channel_id``) e o texto segue o ``code``."""
    error = getattr(exc, "error", None)
    key = str(getattr(error, "key", "") or "")
    code = str(getattr(error, "code", "") or "")
    field = key if key in FIELDS else "channel_id"
    text = tr(f"panel.radio.setup.error.{code}") if code in _ERROR_CODES else tr(f"panel.radio.setup.error.{field}")
    return field, text
