"""`/radio setup` e `/radio reset` no Discord (contrato-nomes-radios §2): a tela em cima do ``pool.radio.setup`` (``utils/radio/setup.py``).

O comando só junta o que o usuário escolheu (canal, bot, tipo) e chama ``configure``; quem decide é o ``setup.py`` (as mesmas checagens do painel web). Depois
de configurar, o fluxo pede a estação: ``custom`` mostra um select das estações do servidor (com a dica de criar em ``/radio station create``) e ``live``
abre o seletor de local da R4. Escolhida a estação, a rádio está pronta para o ``/radio on``. O reset pede confirmação efêmera (botão, 60 s).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.radio import KIND_CUSTOM, KIND_LIVE, Radio
from empiremuse.rules.radio_stations import Station
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.setup import REASON_PREMIUM, RadioForbidden
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

if TYPE_CHECKING:
    from empiremuse.cogs.radio import Radio as RadioCog

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 60.0
SELECT_TIMEOUT_S = 300.0
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


async def setup(cog: RadioCog, inter: disnake.ApplicationCommandInteraction, channel: Any, bot: str, kind: str) -> None:
    """Configura (cria ou muda) a rádio deste canal e segue para a escolha da estação."""
    tr = await cog._gate(inter)
    try:
        bot_id = int(bot)
    except TypeError, ValueError:
        raise GenericError(tr("radio.setup.error.bot_id")) from None
    hub = cog.hub
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
    await choose_station(cog, inter, tr, radio, channel)


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
        return
    stations = await cog.hub.stations.list_for_guild(inter.guild_id)
    lines = [
        tr("radio.setup.done", channel=channel.mention, bot=f"<@{radio.bot_id}>", kind=tr("radio.setup.kind.custom"))
    ]
    if not cog.hub.lastfm.configured:
        lines.append(tr("radio.setup.lastfm_missing"))
    if not stations:
        lines.append(tr("radio.setup.no_stations"))
        await inter.edit_original_message(content="\n".join(lines), allowed_mentions=disnake.AllowedMentions.none())
        return
    lines.append(tr("radio.setup.pick_station"))
    view = StationSelect(cog, tr, radio.id, inter.author.id, stations)
    await inter.edit_original_message(
        content="\n".join(lines), view=view, allowed_mentions=disnake.AllowedMentions.none()
    )


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


class StationSelect(disnake.ui.View):
    """O select das estações do servidor para uma rádio ``custom``; só para quem abriu. Nada do usuário vai em ``custom_id``."""

    def __init__(self, cog: RadioCog, tr: Any, radio_id: str, author_id: int, stations: list[Any]) -> None:
        super().__init__(timeout=SELECT_TIMEOUT_S)
        self.cog, self.tr, self.radio_id, self.author_id = cog, tr, radio_id, author_id
        self.by_id = {s.id: s for s in stations[:25]}
        select = disnake.ui.StringSelect(
            custom_id="radio_setup_station",
            placeholder=fit(tr("radio.setup.select_placeholder"), 150),
            options=[
                disnake.SelectOption(label=fit(external_text(s.name, NAME_LIMIT), 100), value=s.id)
                for s in self.by_id.values()
            ],
        )
        select.callback = self.picked
        self.add_item(select)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.author_id:
            await interaction.response.send_message(self.tr("radio.setup.not_yours"), ephemeral=True)
            return False
        return True

    async def picked(self, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.defer(ephemeral=True)
        station = self.by_id.get(interaction.values[0])
        if station is None:
            raise GenericError(self.tr("radio.setup.expired"))

        async def write(fresh: Radio) -> Radio:
            if fresh.kind != KIND_CUSTOM:
                raise RadioInvalid(_kind_error())
            return await self.cog.hub.store.set_custom_station(
                fresh.id, station.id, expected_rev=fresh.rev, updated_by=str(interaction.author.id)
            )

        await _save_source(self.cog, self.tr, interaction, self.radio_id, write)
        self.stop()
        await interaction.edit_original_message(
            content=self.tr("radio.setup.station_saved", station=external_text(station.name, NAME_LIMIT)), view=None
        )


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
        cog, tr, radio, author.id
    )


async def reset(cog: RadioCog, inter: disnake.ApplicationCommandInteraction, channel: Any) -> None:
    """``/radio reset canal:``: pede a confirmação efêmera (botão, 60 s) antes de apagar a configuração da rádio daquele canal."""
    tr = await cog._gate(inter)
    content, view = await prepare_reset(cog, tr, inter.author, inter.guild_id, channel)
    await inter.edit_original_message(content=content, view=view, allowed_mentions=disnake.AllowedMentions.none())


class ConfirmReset(disnake.ui.View):
    def __init__(self, cog: RadioCog, tr: Any, radio: Radio, author_id: int) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.cog, self.tr, self.radio, self.author_id = cog, tr, radio, author_id
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

    async def confirmed(self, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.defer()
        self.stop()
        try:
            await self.cog.hub.setup.reset(interaction.guild_id, interaction.author.id, self.radio.id, self.radio.rev)
        except (RadioForbidden, RadioNotFound, RadioConflict, RadioInvalid, RadioUnavailable) as error:
            await interaction.edit_original_message(content=str(refusal(self.tr, error)), view=None)
            return
        logger.info("radio.reset: configuração apagada")
        await interaction.edit_original_message(
            content=self.tr("radio.reset.done", channel=f"<#{self.radio.channel_id}>"), view=None
        )
