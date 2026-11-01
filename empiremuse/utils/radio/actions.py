"""O que `/radio ligar`, `/radio desligar` e `/radio status` fazem (item 8, R1), sem Discord: as permissões chegam já conferidas em execução.

Matriz do contrato: ligar e desligar pedem ``manage_guild``; ligar também pede ``manage_messages`` NO CANAL (R-4: quem só tem ``manage_guild``
passaria a apagar mensagens alheias pelo bot). O premium e o teto de 3 são da store (transição confirmada, R-2). Nenhum texto sai daqui: o
motivo da recusa é um código estável (``Refused.code``) que o comando traduz.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import KIND_CUSTOM, BotCandidate, Radio, check_radio, pick_radio_bot, radio_id
from empiremuse.utils.radio.premium import Premium
from empiremuse.utils.radio.service import RadioState
from empiremuse.utils.radio.store import NoPremium, NoRoom, RadioConflict, RadioInvalid, RadioNotFound

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

NO_MANAGE_GUILD = "no_manage_guild"
NO_MANAGE_MESSAGES = "no_manage_messages"
NO_PREMIUM = "no_premium"
NO_PLAYLIST = "no_playlist"
NO_FREE_BOT = "no_free_bot"
BOT_CANNOT_JOIN = "bot_cannot_join"
CHANNEL_TAKEN = "channel_taken"
CAP_REACHED = "cap_reached"
INVALID = "invalid"
CHANGED = "changed"
NOT_FOUND = "not_found"


class Refused(Exception):
    """A ação foi recusada por um motivo conhecido (``code``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Actor:
    """O que a execução já conferiu sobre quem pediu, no canal e no servidor."""

    user_id: int
    manage_guild: bool
    manage_messages_in_channel: bool = False


async def turn_on(
    hub: RadioHub,
    *,
    guild_id: int,
    channel_id: int,
    playlist: str,
    actor: Actor,
    candidates: Iterable[BotCandidate],
    playlist_exists: bool,
    bot_can_join: bool,
) -> Radio:
    """Liga uma rádio personalizada neste canal, no bot reserva escolhido. Devolve a rádio ligada (já gravada, ainda sem runner)."""
    if not actor.manage_guild:
        raise Refused(NO_MANAGE_GUILD)
    if not actor.manage_messages_in_channel:
        raise Refused(NO_MANAGE_MESSAGES)
    premium = await hub.premium.fresh(guild_id)
    if premium is None:
        raise Refused(NO_PREMIUM)
    if not playlist_exists:
        raise Refused(NO_PLAYLIST)
    if not bot_can_join:
        raise Refused(BOT_CANNOT_JOIN)
    existing = await hub.store.list_for_guild(guild_id)
    if any(radio.enabled and radio.channel_id == channel_id for radio in existing):
        raise Refused(CHANNEL_TAKEN)
    running_here = {radio.bot_id for radio in existing if radio.enabled}
    picked = pick_radio_bot(c._replace(has_radio_here=c.has_radio_here or c.bot_id in running_here) for c in candidates)
    if picked is None:
        raise Refused(NO_FREE_BOT)
    wanted = check_radio(
        {
            "_id": radio_id(picked, guild_id),
            "channel_id": str(channel_id),
            "kind": KIND_CUSTOM,
            "source": {"playlist": playlist},
        }
    )
    if isinstance(wanted, RuleError):
        raise Refused(INVALID)
    try:
        if any(radio.id == wanted.id for radio in existing):
            await hub.store.reconfigure(wanted, updated_by=str(actor.user_id))
        else:
            await hub.store.create(wanted, updated_by=str(actor.user_id))
        return await hub.store.set_enabled(wanted.id, True, updated_by=str(actor.user_id))
    except NoPremium as error:
        raise Refused(NO_PREMIUM) from error
    except NoRoom as error:
        raise Refused(CAP_REACHED) from error
    except (RadioConflict, RadioInvalid) as error:
        raise Refused(CHANGED if isinstance(error, RadioConflict) else INVALID) from error


async def turn_off(hub: RadioHub, *, guild_id: int, channel_id: int, actor: Actor) -> list[Radio]:
    """Desliga as rádios ligadas deste canal (de qualquer bot do servidor). Devolve as desligadas; nenhuma ligada ali é ``NOT_FOUND``."""
    if not actor.manage_guild:
        raise Refused(NO_MANAGE_GUILD)
    targets = [r for r in await hub.store.list_for_guild(guild_id) if r.enabled and r.channel_id == channel_id]
    if not targets:
        raise Refused(NOT_FOUND)
    off: list[Radio] = []
    for radio in targets:
        try:
            off.append(await hub.store.set_enabled(radio.id, False, updated_by=str(actor.user_id)))
        except RadioNotFound:
            continue
    return off


@dataclass(frozen=True)
class StatusLine:
    radio: Radio
    state: RadioState | None


@dataclass(frozen=True)
class Status:
    premium: Premium | None
    lines: list[StatusLine]
    paused_all: bool


async def status(hub: RadioHub, *, guild_id: int) -> Status:
    """As rádios do servidor com o estado deste processo. Sem premium, só o aviso (a página web e o comando mostram o mesmo)."""
    premium = await hub.premium.get(guild_id)
    radios = await hub.store.list_for_guild(guild_id) if premium is not None else []
    return Status(
        premium=premium,
        lines=[StatusLine(radio, hub.service.state(radio.id)) for radio in radios],
        paused_all=hub.control.paused_all,
    )
