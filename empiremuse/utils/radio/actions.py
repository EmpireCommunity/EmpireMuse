"""O que `/radio ligar`, `/radio desligar` e `/radio status` fazem (item 8, R1), sem Discord: as permissões chegam já conferidas em execução.

Matriz do contrato: ligar e desligar pedem ``manage_guild``; ligar também pede ``manage_messages`` NO CANAL (R-4: quem só tem ``manage_guild``
passaria a apagar mensagens alheias pelo bot). O premium e o teto de 3 são da store (transição confirmada, R-2). Nenhum texto sai daqui: o
motivo da recusa é um código estável (``Refused.code``) que o comando traduz.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from empiremuse.rules.radio_stations import Station
from empiremuse.rules.radio import KIND_CUSTOM, KIND_LIVE, Radio, source_chosen
from empiremuse.utils.radio.premium import Premium
from empiremuse.utils.radio.service import RadioState
from empiremuse.utils.radio.store import KEEP, NoPremium, NoRoom, RadioConflict, RadioInvalid, RadioNotFound

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

NO_MANAGE_GUILD = "no_manage_guild"
NO_MANAGE_MESSAGES = "no_manage_messages"
NO_PREMIUM = "no_premium"
PAUSED_ALL = "paused_all"
NO_PLAYLIST = "no_playlist"
NO_FREE_BOT = "no_free_bot"
BOT_CANNOT_JOIN = "bot_cannot_join"
CHANNEL_TAKEN = "channel_taken"
JUKEBOX_CHANNEL = "jukebox_channel"
STATION_DOWN = "station_down"
CHOOSE_ONE = "choose_one"
CAP_REACHED = "cap_reached"
INVALID = "invalid"
CHANGED = "changed"
NOT_FOUND = "not_found"
NOT_CONFIGURED = "not_configured"  # nenhuma rádio configurada neste canal: o ``/radio setup`` vem antes
NO_STATION = "no_station"  # configurada, mas sem estação escolhida


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
    actor: Actor,
    bot_can_join: bool,
    bot_free: bool = True,
    jukebox_channel_id: int | None = None,
) -> Radio:
    """Liga a rádio JÁ CONFIGURADA neste canal (``/radio setup``): o bot, o tipo e a estação são os dela, nada se escolhe aqui. Devolve a rádio ligada
    (já gravada, ainda sem runner). Sem rádio no canal é ``NOT_CONFIGURED`` e sem estação escolhida é ``NO_STATION``; as duas apontam para o ``setup``."""
    if not actor.manage_guild:
        raise Refused(NO_MANAGE_GUILD)
    if not actor.manage_messages_in_channel:
        raise Refused(NO_MANAGE_MESSAGES)
    if (
        hub.control.paused_all
    ):  # a válvula de segurança do dono: nenhuma rádio liga (nem ocupa vaga) enquanto ela está fechada
        raise Refused(PAUSED_ALL)
    if await hub.premium.fresh(guild_id) is None:
        raise Refused(NO_PREMIUM)
    radio = next((r for r in await hub.store.list_for_guild(guild_id) if r.channel_id == channel_id), None)
    if radio is None:
        raise Refused(NOT_CONFIGURED)
    if not source_chosen(radio):
        raise Refused(NO_STATION)
    if jukebox_channel_id is not None and jukebox_channel_id == channel_id:
        raise Refused(JUKEBOX_CHANNEL)  # o chat da Jukebox é dela: a varredura da rádio apagaria o painel (V181-1)
    if radio.kind == KIND_LIVE and hub.health.unavailable(radio.source.get("station")):
        raise Refused(STATION_DOWN)
    if not bot_can_join:
        raise Refused(BOT_CANNOT_JOIN)
    if not bot_free:
        raise Refused(NO_FREE_BOT)  # o bot da rádio está em call aqui tocando música
    try:
        return await hub.store.set_enabled(radio.id, True, updated_by=str(actor.user_id))
    except NoPremium as error:
        raise Refused(NO_PREMIUM) from error
    except NoRoom as error:
        raise Refused(CAP_REACHED) from error
    except RadioConflict as error:
        raise Refused(CHANGED) from error
    except RadioInvalid as error:
        raise Refused(INVALID) from error


async def change_station(
    hub: RadioHub, *, radio_id: str, station: Station, actor: Actor, station_list: Sequence[str] | None | object = KEEP
) -> Radio:
    """Troca a estação PRINCIPAL de uma rádio ao vivo LIGADA (painel da call): a store grava com ``rev`` e o gerenciador troca ao vivo.

    ``manage_guild`` e premium são conferidos AQUI, em execução (o select que trouxe a estação vive 5 minutos). Lê a rádio de novo: o ``rev`` usado é o de agora.
    """
    if not actor.manage_guild:
        raise Refused(NO_MANAGE_GUILD)
    current = await hub.store.get(radio_id)
    if current is None or not current.enabled or current.kind != KIND_LIVE:
        raise Refused(NOT_FOUND)
    if await hub.premium.fresh(current.guild_id) is None:
        raise Refused(NO_PREMIUM)
    if hub.health.unavailable(station.uuid):
        raise Refused(STATION_DOWN)
    try:
        updated = await hub.store.set_station(
            radio_id, station.uuid, expected_rev=current.rev, updated_by=str(actor.user_id), station_list=station_list
        )
    except RadioConflict as error:
        raise Refused(CHANGED) from error
    except RadioInvalid as error:
        raise Refused(INVALID) from error
    except RadioNotFound as error:
        raise Refused(NOT_FOUND) from error
    await hub.manager.change_station(updated)
    return updated


async def change_custom_station(hub: RadioHub, *, radio_id: str, station_id: str, actor: Actor) -> Radio:
    """Troca a estação do servidor de uma rádio PERSONALIZADA ligada (anterior e próxima do painel, comando): a store grava com ``rev`` e o gerenciador troca.

    ``manage_guild`` e premium são conferidos AQUI, em execução; a estação tem de ser do MESMO servidor da rádio (a store confere de novo).
    """
    if not actor.manage_guild:
        raise Refused(NO_MANAGE_GUILD)
    current = await hub.store.get(radio_id)
    if current is None or not current.enabled or current.kind != KIND_CUSTOM:
        raise Refused(NOT_FOUND)
    if await hub.premium.fresh(current.guild_id) is None:
        raise Refused(NO_PREMIUM)
    try:
        updated = await hub.store.set_custom_station(
            radio_id, station_id, expected_rev=current.rev, updated_by=str(actor.user_id)
        )
    except RadioConflict as error:
        raise Refused(CHANGED) from error
    except RadioInvalid as error:
        raise Refused(INVALID) from error
    except RadioNotFound as error:
        raise Refused(NOT_FOUND) from error
    await hub.manager.change_station(updated)
    return updated


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
