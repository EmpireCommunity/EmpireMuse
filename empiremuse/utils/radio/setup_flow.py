"""`/radio setup` e `/radio reset` no Discord (contrato-nomes-radios §2): a tela em cima do ``pool.radio.setup`` (``utils/radio/setup.py``).

O comando só junta o que o usuário escolheu (canal, bot, tipo) e chama ``configure``; quem decide é o ``setup.py`` (as mesmas checagens do painel web). Depois
de configurar, ``arrive`` liga a rádio em espera e a estação se escolhe no painel. O reset pede confirmação efêmera (botão, 60 s).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.radio import KIND_CUSTOM, KIND_LIVE, Radio
from empiremuse.rules.radio_stations import Station
from empiremuse.rules.text import external_text
from empiremuse.utils.ephemeral import CONFIRMATION_TTL_S, ERROR_TTL_S, cleanup_later
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio import actions, radio_commands
from empiremuse.utils.radio.actions import Actor, Refused
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.setup import REASON_PREMIUM, RadioForbidden, jukebox_channel_of
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

if TYPE_CHECKING:
    from empiremuse.cogs.radio import Radio as RadioCog

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 60.0
NAME_LIMIT = 80
KNOWN_CODES = frozenset(
    {
        "reserve_only",
        "bot_in_call",
        "one_per_bot",
        "max_radios",
        "channel_kind",
        "jukebox_channel",
        "cannot_connect",
        "radio_on",
        "channel_taken",
    }
)


def refusal(tr: Any, error: Exception) -> GenericError:
    """A recusa de ``configure``/``reset`` como erro para o usuário: sempre um texto traduzido, nunca a exceção."""
    if isinstance(error, RadioForbidden):
        key = "no_premium" if error.reason == REASON_PREMIUM else "no_manage_guild"
        return GenericError(tr(f"radio.refused.{key}"))
    if isinstance(error, (RadioNotFound, RadioConflict)):
        return GenericError(tr("radio.refused.changed"))
    if isinstance(error, RadioInvalid):
        code, field = str(error.error.code), str(error.error.key)
        return GenericError(tr(f"radio.setup.error.{code if code in KNOWN_CODES else field}"))
    return GenericError(tr("radio.owner.unavailable"))


async def _no_bot_reason(cog: RadioCog, inter: Any, tr: Any) -> GenericError:
    """Sem id de bot válido: diz POR QUÊ (sem permissão, sem premium, ou nenhum reserva livre, com quem ocupa cada um), nunca um erro mudo."""
    hub = cog.hub
    try:
        await hub.setup.check_actor(inter.guild_id, inter.author.id)
        options = await hub.setup.options(inter.guild_id, inter.author.id)
        busy = await hub.setup.busy_reserves(inter.guild_id)
    except (RadioForbidden, RadioUnavailable) as error:
        return refusal(tr, error)
    if options.bots:
        return GenericError(tr("radio.setup.error.bot_id"))
    if not busy:
        return GenericError(tr("radio.setup.error.no_reserve_none"))
    lines = "\n".join(tr("radio.setup.error.busy_line", bot=name, channel=f"<#{channel}>") for name, channel in busy)
    return GenericError(tr("radio.setup.error.no_reserve", busy=lines))


async def setup(cog: RadioCog, inter: disnake.ApplicationCommandInteraction, channel: Any, bot: str, kind: str) -> None:
    """Configura (cria ou muda) a rádio deste canal e segue para a escolha da estação."""
    tr = await cog._gate(inter)
    hub = cog.hub
    try:
        bot_id = int(bot)
    except TypeError, ValueError:
        raise await _no_bot_reason(cog, inter, tr) from None
    try:
        existing = await hub.store.list_for_guild(inter.guild_id)
        current = next((r for r in existing if r.channel_id == channel.id), None) or next(
            (r for r in existing if r.bot_id == bot_id), None
        )
        radio = await hub.setup.configure(
            inter.guild_id,
            inter.author.id,
            radio_id=current.id if current else None,
            channel_id=channel.id,
            bot_id=bot_id,
            kind=kind,
            rev=current.rev if current else None,
        )
    except (RadioForbidden, RadioNotFound, RadioConflict, RadioInvalid, RadioUnavailable) as error:
        raise refusal(tr, error) from None
    await arrive(cog, inter, tr, radio, channel)


async def arrive(cog: RadioCog, inter: Any, tr: Any, radio: Radio, channel: Any) -> None:
    """Depois de configurar (R1): o bot ENTRA na call já, o chat dela é limpo e o painel é posto; a estação se escolhe no painel (nada de seletor aqui).
    Se algo impede de entrar (sem permissão, bot em call), a configuração fica e a resposta diz o motivo."""
    kind = tr("radio.setup.kind.live" if radio.kind == KIND_LIVE else "radio.setup.kind.custom")
    lines = [tr("radio.setup.done", channel=channel.mention, bot=f"<@{radio.bot_id}>", kind=kind)]
    if radio.kind == KIND_CUSTOM and not cog.hub.lastfm.configured:
        lines.append(tr("radio.setup.lastfm_missing"))
    try:
        await _start_waiting(cog, inter, radio, channel)
    except Refused as refused:
        lines.append(tr(f"radio.refused.{refused.code}"))
    except RadioUnavailable:
        lines.append(tr("radio.owner.unavailable"))
    else:
        lines.append(tr("radio.setup.arriving"))
    await inter.edit_original_message(content="\n".join(lines), allowed_mentions=disnake.AllowedMentions.none())
    cleanup_later(inter, CONFIRMATION_TTL_S)


async def _start_waiting(cog: RadioCog, inter: Any, radio: Radio, channel: Any) -> None:
    """Liga a rádio recém-configurada SEM estação: o runner entra na call e fica em espera (``Phase.STANDBY``) com o painel no chat."""
    actor = Actor(
        user_id=inter.author.id,
        manage_guild=bool(inter.author.guild_permissions.manage_guild),
        manage_messages_in_channel=bool(channel.permissions_for(inter.author).manage_messages),
    )
    can_join, free = cog._bot_state(radio, channel)
    jukebox = await jukebox_channel_of(cog.bot.pool, inter.guild_id)
    enabled = await actions.turn_on(
        cog.hub,
        guild_id=inter.guild_id,
        channel_id=channel.id,
        actor=actor,
        bot_can_join=can_join,
        bot_free=free,
        jukebox_channel_id=jukebox,
        allow_no_station=True,
    )
    await cog.hub.manager.activate(enabled, wait=False, fresh=True)


async def choose_station(cog: RadioCog, inter: Any, tr: Any, radio: Radio, channel: Any) -> None:
    """Depois de configurar: a estação que a rádio toca. Sem ela a rádio existe, mas não liga."""
    if radio.kind == KIND_LIVE:
        picker = cog._picker(
            inter,
            tr,
            lambda click, station, station_list: apply_live(cog, click, tr, radio.id, station, station_list),
            tr("radio.setup.confirm_station", channel=channel.name),
        )
        rendered = await picker.render()
        done = tr(
            "radio.setup.done", channel=channel.mention, bot=f"<@{radio.bot_id}>", kind=tr("radio.setup.kind.live")
        )
        rendered["content"] = f"{done}\n{rendered.get('content') or ''}".strip()
        await inter.edit_original_message(**rendered, allowed_mentions=disnake.AllowedMentions.none())


async def _save_source(cog: RadioCog, tr: Any, interaction: Any, radio_id: str, write: Any) -> Radio:
    """Grava a estação escolhida na rádio de AGORA (o select vive minutos: premium, ``manage_guild`` e ``rev`` são conferidos aqui)."""
    hub = cog.hub
    try:
        await hub.setup.check_actor(interaction.guild_id, interaction.author.id)
        fresh = await hub.store.get(radio_id)
        if fresh is None:
            raise RadioNotFound
        updated = await write(fresh)
    except (RadioForbidden, RadioNotFound, RadioConflict, RadioInvalid, RadioUnavailable) as error:
        raise refusal(tr, error) from None
    if updated.enabled:
        await hub.manager.change_station(updated)  # ligada: troca ao vivo, pelo recuo
    return updated


async def apply_live(
    cog: RadioCog, click: Any, tr: Any, radio_id: str, station: Station, station_list: tuple[str, ...]
) -> str:
    async def write(fresh: Radio) -> Radio:
        if fresh.kind != KIND_LIVE:
            raise RadioInvalid(_kind_error())
        return await cog.hub.store.set_station(
            fresh.id,
            station.uuid,
            expected_rev=fresh.rev,
            updated_by=str(click.author.id),
            station_list=station_list or None,
        )

    await _save_source(cog, tr, click, radio_id, write)
    return tr("radio.setup.station_saved", station=external_text(station.name, NAME_LIMIT))


def _kind_error():
    from empiremuse.rules.errors import RuleError
    from empiremuse.rules.radio import SOURCE_INVALID

    return RuleError(SOURCE_INVALID, "kind")


async def prepare_reset(cog: RadioCog, tr: Any, author: Any, guild_id: int, channel: Any) -> tuple[str, ConfirmReset]:
    """O texto e a confirmação (botão, 60 s) de apagar a configuração da rádio de um canal. Vale para o slash e para o prefixo."""
    if not author.guild_permissions.manage_guild:
        raise GenericError(tr("radio.refused.no_manage_guild"))
    try:
        radios = await cog.hub.store.list_for_guild(guild_id)
    except RadioUnavailable:
        raise GenericError(tr("radio.owner.unavailable")) from None
    radio = next((r for r in radios if r.channel_id == channel.id), None)
    if radio is None:
        raise GenericError(tr("radio.reset.none", channel=channel.mention))
    return tr("radio.reset.ask", channel=channel.mention, bot=f"<@{radio.bot_id}>"), ConfirmReset(
        cog, tr, [radio], author.id
    )


async def reset(
    cog: RadioCog, inter: disnake.ApplicationCommandInteraction, bot: str | None, channel: str | None = None
) -> None:
    """``/radio reset [canal:] [bot:]``: pede a confirmação efêmera (botão, 60 s). Com canal e/ou bot, só a rádio apontada; sem nenhum, TODAS as do servidor."""
    tr = await cog._gate(inter)
    if not inter.author.guild_permissions.manage_guild:
        raise GenericError(tr("radio.refused.no_manage_guild"))
    radios = await radio_commands.pick(inter, tr, channel, bot, all_if_empty=True)
    if not radios:
        raise GenericError(tr("radio.pick.none"))
    if len(radios) == 1:
        radio = radios[0]
        ask = tr("radio.reset.ask", channel=f"<#{radio.channel_id}>", bot=f"<@{radio.bot_id}>")
    else:
        ask = tr("radio.reset.ask_all", total=len(radios))
    view = ConfirmReset(cog, tr, list(radios), inter.author.id)
    await inter.edit_original_message(content=ask, view=view, allowed_mentions=disnake.AllowedMentions.none())
    view.message_interaction = inter


class ConfirmReset(disnake.ui.View):
    def __init__(self, cog: RadioCog, tr: Any, radios: list[Radio], author_id: int) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.cog, self.tr, self.radios, self.author_id = cog, tr, radios, author_id
        self.message_interaction: Any = None  # a resposta com a pergunta: some no prazo se ninguém responder (R8)
        confirm = disnake.ui.Button(
            label=fit(tr("radio.reset.confirm"), 80), style=disnake.ButtonStyle.danger, custom_id="radio_reset_confirm"
        )
        cancel = disnake.ui.Button(label=fit(tr("radio.reset.cancel"), 80), custom_id="radio_reset_cancel")
        confirm.callback, cancel.callback = self.confirmed, self.cancelled
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.author_id:
            await interaction.response.send_message(self.tr("radio.setup.not_yours"), ephemeral=True)
            return False
        return True

    async def cancelled(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        await interaction.response.edit_message(content=self.tr("radio.reset.cancelled"), view=None)
        cleanup_later(interaction, CONFIRMATION_TTL_S)

    async def on_timeout(self) -> None:
        self.stop()
        if self.message_interaction is not None:
            cleanup_later(self.message_interaction, 0)

    async def confirmed(self, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.defer()
        self.stop()
        failures = []
        for radio in self.radios:
            try:
                await self.cog.hub.setup.reset(interaction.guild_id, interaction.author.id, radio.id, radio.rev)
            except (RadioForbidden, RadioNotFound, RadioConflict, RadioInvalid, RadioUnavailable) as error:
                failures.append(str(refusal(self.tr, error)))
        if failures:
            await interaction.edit_original_message(content="\n".join(dict.fromkeys(failures)), view=None)
            cleanup_later(interaction, ERROR_TTL_S)
            return
        logger.info("radio.reset: configuração apagada (%d)", len(self.radios))
        done = (
            self.tr("radio.reset.done", channel=f"<#{self.radios[0].channel_id}>")
            if len(self.radios) == 1
            else self.tr("radio.reset.done_many", total=len(self.radios))
        )
        await interaction.edit_original_message(content=done, view=None)
        cleanup_later(interaction, CONFIRMATION_TTL_S)
