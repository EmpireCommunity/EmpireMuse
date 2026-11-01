"""Página Rádios (item 8; contrato R1 §3 "Página web" e R3 §4): as rádios do servidor e as regras do chat da call.

Os dados vêm de ``pool.radio`` (Turbine): ``store`` (``list_for_guild``, ``get``, ``update_chat``), ``premium`` (``get``,
cache de 30 s) e ``service`` (``state``). Aqui ficam as conferências da R-6 (id da rota com o sufixo ``:{guild_id}`` do
``GuildAccess``, servidor premium e ``manage_messages`` no canal da rádio, lido no bot da rádio), a allowlist ``chat.*``
(qualquer outro campo do POST é ignorado), a validação por ``rules.radio.check_radio`` e o compare-and-set em ``rev``.
Nenhum efeito no Discord: o bot relê o documento. Nada do usuário vai para o log além dos ids.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict
from typing import Any, Mapping, NamedTuple, Optional

import disnake

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
from empiremuse.panel.services.service import STATUS_CONFLICT, STATUS_INVALID, STATUS_OK, Outcome
from empiremuse.rules.errors import RuleError
from empiremuse.rules.i18n import Translator

logger = logging.getLogger(__name__)

CATEGORY = "radios"
MAX_RADIOS = 3  # teto por servidor (contrato R1 §2)
RADIO_ID = re.compile(r"[0-9]{1,20}:[0-9]{1,20}", re.ASCII)
REV = re.compile(r"[0-9]{1,12}", re.ASCII)
INTEGER = re.compile(r"[0-9]{1,6}", re.ASCII)
CHAT_FIELDS = ("voice_delete_after_s", "stage_max_messages", "stage_max_age_s")
# Faixas do contrato R1 §1, só para o ``min``/``max`` do campo e a mensagem de erro; quem decide é ``check_radio``.
CHAT_RANGES: dict[str, tuple[int, int]] = {
    "voice_delete_after_s": (5, 600),
    "stage_max_messages": (3, 50),
    "stage_max_age_s": (60, 3600),
}
KINDS = ("custom", "live")
STATE_DISABLED, STATE_OWNER, STATE_OFFLINE, STATE_RESERVE, STATE_PLAYING, STATE_PAUSED = (
    "desligada",
    "pausada_dono",
    "fora_do_ar",
    "reserva",
    "tocando",
    "pausada",
)
FORBIDDEN_KEY = "panel.radio.chat.forbidden"
STATUS_FORBIDDEN = 403


# Portas para o esqueleto da R1 (Turbine): importadas na chamada até ``rules/radio.py`` e ``utils/radio/store.py`` entrarem
# na base; os testes do painel as trocam pelo dublê. Depois viram imports normais no topo.
def check_radio(doc: Mapping[str, Any]) -> Any:
    """``rules.radio.check_radio``: devolve ``Radio`` ou ``RuleError(code, key)``; nunca levanta (R1 §1)."""
    from empiremuse.rules.radio import check_radio as real

    return real(doc)


def store_errors() -> tuple[type[Exception], type[Exception]]:
    """``(RadioConflict, RadioInvalid)`` de ``utils/radio/store.py``."""
    from empiremuse.utils.radio.store import RadioConflict, RadioInvalid

    return RadioConflict, RadioInvalid


class RadioRow(NamedTuple):
    """Uma rádio do servidor como a página mostra (textos já traduzidos; nada vai para ``href``/``src``)."""

    id: str
    slug: str  # id seguro para atributos ``id`` do HTML (``bot-guild``)
    bot_nome: str
    canal_nome: Optional[str]
    tipo: str
    estado: str
    titulo: Optional[str]
    form: Optional[Formulario]


def radio_id_of(guild_id: int, raw: str) -> Optional[str]:
    """O id da rota só vale com a forma ``bot:guild`` e o servidor do ``GuildAccess`` (R-6); senão ``None`` (404)."""
    if not RADIO_ID.fullmatch(raw) or not raw.endswith(f":{guild_id}"):
        return None
    return raw


def belongs_to(radio: Any, guild_id: int) -> bool:
    return str(radio.id).endswith(f":{guild_id}") and int(radio.guild_id) == guild_id


def state_of(radio: Any, state: Any) -> str:
    if not radio.enabled or state is None:
        return STATE_DISABLED
    if getattr(state, "paused_by_owner", False):
        return STATE_OWNER
    if getattr(state, "offline", False):
        return STATE_OFFLINE
    if getattr(state, "on_reserve", False):
        return STATE_RESERVE
    return STATE_PLAYING if getattr(state, "playing", False) else STATE_PAUSED


def can_write(premium: Any) -> bool:
    """Premium concedido pelo dono e com rádio ligada (``active`` 1..3); sem isso a página só mostra o aviso."""
    return premium is not None and int(getattr(premium, "active", 0) or 0) > 0


class RadioPage:
    """Leitura e edição das rádios de um servidor, sempre a partir do ``FormContext`` do usuário autorizado."""

    # ------------------------------------------------------------------ leitura

    @staticmethod
    def bot_of(ctx: FormContext, radio: Any) -> Any:
        """O bot da rádio, só se ele está neste servidor."""
        return next((b for b in ctx.pool.get_guild_bots(ctx.guild_id) if int(b.user.id) == int(radio.bot_id)), None)

    async def can_edit(self, ctx: FormContext, radio: Any) -> bool:
        """``manage_messages`` no canal da rádio, conferido no bot da rádio (R-4, R-6); sem bot, canal ou membro: não."""
        bot = self.bot_of(ctx, radio)
        if bot is None or not ctx.user_id.isdigit():
            return False
        guild = bot.get_guild(ctx.guild_id)
        channel = bot.get_channel(int(radio.channel_id))
        if guild is None or channel is None:
            return False
        if getattr(getattr(channel, "guild", None), "id", ctx.guild_id) != ctx.guild_id:
            return False
        member = guild.get_member(int(ctx.user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(ctx.user_id))
            except disnake.HTTPException, OSError:
                return False
        return bool(channel.permissions_for(member).manage_messages)

    async def rows(self, ctx: FormContext, action: str, editable: bool, tr: Translator) -> tuple[RadioRow, ...]:
        radios = await ctx.pool.radio.store.list_for_guild(ctx.guild_id)
        rows = []
        for radio in sorted(radios, key=lambda r: int(r.bot_id)):
            if not belongs_to(radio, ctx.guild_id):  # defesa em profundidade (R-6): a store já filtra pelo servidor
                continue
            bot = self.bot_of(ctx, radio)
            channel = bot.get_channel(int(radio.channel_id)) if bot is not None else None
            state = ctx.pool.radio.service.state(radio.id)
            form = None
            if editable and await self.can_edit(ctx, radio):
                form = self.form(radio, f"{action}/{radio.id}")
            kind = str(radio.kind)
            title = getattr(state, "title", None) if state is not None else None
            rows.append(
                RadioRow(
                    id=str(radio.id),
                    slug=str(radio.id).replace(":", "-"),
                    bot_nome=str(bot.user.name) if bot is not None else str(radio.bot_id),
                    canal_nome=str(channel.name) if channel is not None else None,
                    tipo=tr(f"panel.radio.kind.{kind}") if kind in KINDS else kind,
                    estado=tr(f"panel.radio.state.{state_of(radio, state)}"),
                    titulo=str(title) if title else None,
                    form=form,
                )
            )
        return tuple(rows)

    # ------------------------------------------------------------------ formulário chat.*

    @staticmethod
    def form(
        radio: Any,
        action: str,
        *,
        values: Optional[dict[str, Any]] = None,
        errors: Optional[dict[str, str]] = None,
        message: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> Formulario:
        current = {name: radio.chat.get(name) for name in CHAT_FIELDS}
        return Formulario(
            key="radio_chat",
            form_id=f"form-radio-{radio.bot_id}",
            template="form_radio_chat.html",
            acao=action,
            versao=str(radio.rev),
            valores={**current, **(values or {})},
            erros=errors or {},
            mensagem=message,
            mensagem_tipo=kind,
            extras={"faixas": CHAT_RANGES, "radio_id": str(radio.id), "bot_id": str(radio.bot_id)},
        )

    @staticmethod
    def parse(radio: Any, args: Args, tr: Translator) -> tuple[dict[str, int], dict[str, str], dict[str, str]]:
        """Só ``chat.*`` entra (allowlist R-6); inteiro por campo aqui, faixa e coerência por ``check_radio``."""
        new: dict[str, int] = {}
        errors: dict[str, str] = {}
        echo: dict[str, str] = {}
        for name in CHAT_FIELDS:
            raw = (first(args, name) or "").strip()
            echo[name] = raw
            if INTEGER.fullmatch(raw):
                new[name] = int(raw)
            else:
                errors[name] = _range_message(tr, name)
        if errors:
            return new, errors, echo
        checked = check_radio({**asdict(radio), "chat": {**radio.chat, **new}})
        if isinstance(checked, RuleError):
            field = checked.key if checked.key in CHAT_FIELDS else CHAT_FIELDS[0]
            errors[field] = _range_message(tr, field)
        return new, errors, echo

    async def submit(self, ctx: FormContext, radio: Any, args: Args, action: str, tr: Translator) -> Outcome:
        sent = first(args, "versao")
        if sent is None or not REV.fullmatch(sent):
            return Outcome(STATUS_INVALID, self.form(radio, action, message=VERSION_TEXT, kind=KIND_ERROR))
        if int(sent) != int(radio.rev):
            return Outcome(STATUS_CONFLICT, self.form(radio, action, message=CONFLICT_TEXT, kind=KIND_ERROR))
        new, errors, echo = self.parse(radio, args, tr)
        if errors:
            form = self.form(radio, action, values=echo, errors=errors, message=INVALID_TEXT, kind=KIND_ERROR)
            return Outcome(STATUS_INVALID, form)
        changed = tuple(name for name in CHAT_FIELDS if new[name] != radio.chat.get(name))
        if not changed:
            return Outcome(STATUS_OK, self.form(radio, action, message=SAVED_TEXT, kind=KIND_OK))
        conflict, invalid = store_errors()
        try:
            saved = await ctx.pool.radio.store.update_chat(
                radio.id, {**radio.chat, **new}, expected_rev=int(radio.rev), updated_by=ctx.user_id
            )
        except conflict:
            return Outcome(STATUS_CONFLICT, self.form(radio, action, message=CONFLICT_TEXT, kind=KIND_ERROR))
        except invalid:
            form = self.form(radio, action, values=echo, message=tr("panel.radio.chat.invalid"), kind=KIND_ERROR)
            return Outcome(STATUS_INVALID, form)
        logger.info(  # só ids e nomes de campo (R-12): valores nunca vão para o log
            "Configuração alterada: usuario=%s servidor=%s escopo=radio:%s categoria=%s campos=%s",
            ctx.user_id,
            ctx.guild_id,
            radio.bot_id,
            CATEGORY,
            ",".join(changed),
        )
        return Outcome(STATUS_OK, self.form(saved, action, message=SAVED_TEXT, kind=KIND_OK))


def _range_message(tr: Translator, field: str) -> str:
    low, high = CHAT_RANGES[field]
    return tr("panel.radio.chat.range", min=low, max=high)
