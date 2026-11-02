"""Escolha da rádio nos comandos ``/radio on|off|reset`` (R2, R2b, R4): o usuário aponta por CANAL ou por BOT, e cada lista oferece SÓ o que existe.

Nenhum comando oferece uma escolha que depois falha ("não há rádio configurada neste canal"): as listas saem das rádios gravadas do servidor.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.radio import Radio
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio.premium import RadioUnavailable

MAX_CHOICES = 25
NO_CHOICE = "none"


async def _radios(inter: disnake.ApplicationCommandInteraction) -> list[Radio]:
    if inter.guild_id is None or not inter.author.guild_permissions.manage_guild:
        return []
    try:
        return list(await inter.bot.pool.radio.store.list_for_guild(inter.guild_id))
    except RadioUnavailable:
        return []


def _bot_name(inter: Any, bot_id: int) -> str:
    bot = next((b for b in inter.bot.pool.get_all_bots() if b.user is not None and b.user.id == bot_id), None)
    return str(bot.user.name) if bot is not None else str(bot_id)


async def channel_autocomplete(inter: disnake.ApplicationCommandInteraction, current: str) -> dict[str, str]:
    """Os canais que TÊM rádio, ``{#nome: id}``. Sem rádio ou sem ``manage_guild``, vazio (não há o que escolher)."""
    wanted = (current or "").strip().casefold()
    guild = inter.guild
    names = {}
    for radio in await _radios(inter):
        channel = guild.get_channel(radio.channel_id) if guild is not None else None
        label = f"#{channel.name}" if channel is not None else f"#{radio.channel_id}"
        if wanted in label.casefold():
            names[fit(label, 100)] = str(radio.channel_id)
    return dict(list(names.items())[:MAX_CHOICES])


async def bot_autocomplete(inter: disnake.ApplicationCommandInteraction, current: str) -> dict[str, str]:
    """Os bots que SERVEM uma rádio neste servidor, ``{nome: id}``."""
    wanted = (current or "").strip().casefold()
    names = {}
    for radio in await _radios(inter):
        name = _bot_name(inter, radio.bot_id)
        if wanted in name.casefold():
            names[fit(name, 100)] = str(radio.bot_id)
    return dict(list(names.items())[:MAX_CHOICES])


def _snowflake(raw: object) -> int | None:
    try:
        value = int(str(raw))
    except TypeError, ValueError:
        return None
    return value if value > 0 else None


async def pick(inter: Any, tr: Any, channel: str | None, bot: str | None, *, all_if_empty: bool = False) -> list[Radio]:
    """As rádios apontadas por canal e/ou bot. Os dois juntos têm de apontar para a MESMA rádio. Nenhum dos dois: erro que pede um (``on``/``off``) ou,
    com ``all_if_empty``, todas as do servidor (``reset``). Valor que não é de uma rádio existente (digitado à mão) é recusado com o motivo."""
    if not inter.author.guild_permissions.manage_guild:
        raise GenericError(
            tr("radio.refused.no_manage_guild")
        )  # antes de dizer se a rádio existe: quem não gerencia não sonda o servidor
    try:
        radios = list(await inter.bot.pool.radio.store.list_for_guild(inter.guild_id))
    except RadioUnavailable:
        raise GenericError(tr("radio.owner.unavailable")) from None
    channel_id, bot_id = _snowflake(channel), _snowflake(bot)
    if channel is None and bot is None:
        if all_if_empty:
            return radios
        raise GenericError(tr("radio.pick.need_one"))
    found = [
        r for r in radios if (channel is None or r.channel_id == channel_id) and (bot is None or r.bot_id == bot_id)
    ]
    if not found:
        raise GenericError(tr("radio.pick.none"))
    return found


def channel_of(inter: Any, radio: Radio) -> Any:
    """O canal da rádio como o Discord o mostra; apagado, um substituto com ``id`` e ``mention`` (desligar e redefinir ainda funcionam)."""
    lookup = getattr(inter.guild, "get_channel", None)
    channel = lookup(radio.channel_id) if lookup is not None else None
    return channel or SimpleNamespace(id=radio.channel_id, mention=f"<#{radio.channel_id}>", name=str(radio.channel_id))
