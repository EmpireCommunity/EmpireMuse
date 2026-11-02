"""A configuração das rádios (contrato-nomes-radios §2 e §4.3): canal, bot reserva fixo e tipo; e o redefinir.

Uma função só para as duas superfícies: o ``/radio setup`` e ``/radio reset`` do Discord e a página Rádios do painel web chamam o mesmo
``pool.radio.setup``. As checagens rodam AQUI, em execução, a cada chamada (nenhuma vale por ter sido feita antes numa tela):

- o servidor é premium e quem pede tem ``manage_guild`` (consultado no cache do bot agora, nunca no que o painel diz);
- o bot é reserva do pool, não é o principal, está no servidor e pronto, e não está em call aqui (a não ser a rádio dele mesmo);
- uma rádio por bot e por servidor, e no máximo ``MAX_PER_GUILD`` configuradas (as pausadas contam);
- o canal é de voz ou de palco deste servidor, não é o da Jukebox, e o bot consegue ver, conectar e falar nele;
- escrita com compare-and-set em ``rev``; numa rádio LIGADA trocar canal, bot ou tipo exige desligar antes.

Recusas: ``RadioForbidden`` (premium ou manage_guild), ``RadioNotFound`` (rádio inexistente ou de outro servidor), ``RadioConflict`` (``rev``
velho) e ``RadioInvalid`` (``error.key`` = ``kind``, ``channel_id`` ou ``bot_id``; ``error.code`` o motivo). Nada aqui monta texto para o usuário
nem põe nome de canal ou de bot no log (R-12).
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import (
    CHANNEL_INVALID,
    KIND_INVALID,
    KINDS,
    MAX_PER_GUILD,
    Radio,
    check_radio,
    radio_id,
    split_radio_id,
    to_document,
)
from empiremuse.rules.servers import is_main_identifier
from empiremuse.utils.radio.premium import GLOBAL_DB, RadioUnavailable
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

REASON_PREMIUM = "premium"
REASON_MANAGE_GUILD = "manage_guild"

# Códigos de ``RadioInvalid.error.code`` que as duas superfícies traduzem (``key`` é o campo).
RESERVE_ONLY = "reserve_only"
BOT_IN_CALL = "bot_in_call"
ONE_PER_BOT = "one_per_bot"
MAX_RADIOS = "max_radios"
CHANNEL_KIND = "channel_kind"
JUKEBOX_CHANNEL = "jukebox_channel"
CANNOT_CONNECT = "cannot_connect"
RADIO_ON = "radio_on"
CHANNEL_TAKEN = "channel_taken"

CONFIG_BACKUP_DB = "config_backup"  # ``global.config_backup``: o documento da rádio que o reset apagou
CONFIG_BACKUP_TTL_S = 30 * 24 * 3600
CHANNEL_VOICE = "voice"
CHANNEL_STAGE = "stage"


class RadioForbidden(Exception):
    """Quem pede (ou o servidor) não pode: ``reason`` é ``premium`` ou ``manage_guild``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SetupOptions:
    """O que a tela oferece: ``bots`` = ``(bot_id, nome)`` dos reservas livres; ``channels`` = ``(channel_id, nome, "voice"|"stage")``."""

    bots: tuple[tuple[int, str], ...]
    channels: tuple[tuple[int, str, str], ...]


async def jukebox_channel_of(pool: Any, guild_id: int) -> Optional[int]:
    """O canal da Jukebox do servidor (a rádio nunca toca nele: a varredura do chat apagaria o painel da Jukebox)."""
    return getattr(await pool.servers.request.get(guild_id), "channel", None)


class RadioSetup:
    def __init__(self, hub: RadioHub, pool: Any) -> None:
        self.hub = hub
        self.pool = pool

    # ------------------------------------------------------------------ o que o Discord mostra

    def _bots(self) -> list[Any]:
        return [bot for bot in self.pool.get_all_bots() if bot.user is not None]

    def _guild(self, guild_id: int) -> Any:
        """O servidor como o cache do gateway o vê (o do principal primeiro): a fonte de membros, canais e permissões."""
        bots = sorted(self._bots(), key=lambda b: not is_main_identifier(b.identifier))
        return next((g for bot in bots if (g := bot.get_guild(guild_id)) is not None), None)

    def _free_reserves(self, guild_id: int, taken: set[int]) -> list[Any]:
        """Reservas prontos neste servidor, sem call nem player aqui e sem rádio nesta configuração (``taken`` = bots que já têm uma)."""
        free = []
        for bot in self._bots():
            guild = bot.get_guild(guild_id)
            exclusive = getattr(bot, "exclusive_guild_id", None)
            if (
                guild is None
                or is_main_identifier(bot.identifier)
                or (exclusive and exclusive != guild_id)
                or not bot.bot_ready
                or bot.user.id in taken
                or guild_id in bot.music.players
                or guild.me.voice
            ):
                continue
            free.append(bot)
        return free

    async def options(self, guild_id: int) -> SetupOptions:
        """Os reservas livres e os canais de voz e de palco que valem (a lista que o painel e o comando mostram; ``configure`` confere de novo)."""
        guild_id = int(guild_id)
        existing = await self.hub.store.list_for_guild(guild_id)
        bots = tuple(
            (bot.user.id, str(bot.user.name)) for bot in self._free_reserves(guild_id, {r.bot_id for r in existing})
        )
        guild = self._guild(guild_id)
        jukebox = await jukebox_channel_of(self.pool, guild_id)
        channels: list[tuple[int, str, str]] = []
        if guild is not None:
            for channel in (*guild.voice_channels, *guild.stage_channels):
                if channel.id != jukebox:
                    kind = CHANNEL_STAGE if isinstance(channel, disnake.StageChannel) else CHANNEL_VOICE
                    channels.append((channel.id, str(channel.name), kind))
        return SetupOptions(bots=bots, channels=tuple(channels))

    # ------------------------------------------------------------------ as checagens

    async def check_actor(self, guild_id: int, actor_id: int) -> None:
        if await self.hub.premium.fresh(guild_id) is None:
            raise RadioForbidden(REASON_PREMIUM)
        if not await self._can_manage(guild_id, actor_id):
            raise RadioForbidden(REASON_MANAGE_GUILD)

    async def _can_manage(self, guild_id: int, actor_id: int) -> bool:
        """``manage_guild`` de quem pede, agora, no servidor (o dono e administrador valem). Não achar o membro nega (fecha por padrão)."""
        guild = self._guild(guild_id)
        if guild is None:
            return False
        member = guild.get_member(actor_id)
        if member is None:
            try:
                member = await guild.fetch_member(actor_id)
            except disnake.HTTPException:
                return False
        permissions = member.guild_permissions
        return bool(permissions.manage_guild or permissions.administrator or guild.owner_id == actor_id)

    @staticmethod
    def _check_kind(kind: object) -> str:
        if kind not in KINDS:
            raise RadioInvalid(RuleError(KIND_INVALID, "kind"))
        return str(kind)

    def _check_bot(self, guild_id: int, bot_id: int, *, current: Optional[Radio]) -> Any:
        bot = next((b for b in self._bots() if b.user.id == bot_id), None)
        guild = bot.get_guild(guild_id) if bot is not None else None
        exclusive = getattr(bot, "exclusive_guild_id", None)
        if (
            bot is None
            or guild is None
            or is_main_identifier(bot.identifier)
            or (exclusive and exclusive != guild_id)
            or not bot.bot_ready
        ):
            raise RadioInvalid(RuleError(RESERVE_ONLY, "bot_id"))
        own = current is not None and current.bot_id == bot_id
        if not own and (guild_id in bot.music.players or guild.me.voice):
            raise RadioInvalid(RuleError(BOT_IN_CALL, "bot_id"))
        return bot

    async def _check_channel(self, guild_id: int, channel_id: int, bot: Any) -> None:
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if not isinstance(channel, (disnake.VoiceChannel, disnake.StageChannel)):
            raise RadioInvalid(RuleError(CHANNEL_KIND, "channel_id"))
        if channel_id == await jukebox_channel_of(self.pool, guild_id):
            raise RadioInvalid(RuleError(JUKEBOX_CHANNEL, "channel_id"))
        permissions = channel.permissions_for(guild.me)
        speaks = permissions.speak or isinstance(channel, disnake.StageChannel)  # no palco o bot pede a palavra
        if not (permissions.view_channel and permissions.connect and speaks):
            raise RadioInvalid(RuleError(CANNOT_CONNECT, "channel_id"))

    # ------------------------------------------------------------------ configurar

    async def configure(
        self,
        guild_id: int,
        actor_id: int,
        *,
        radio_id: Optional[str],
        channel_id: int,
        bot_id: int,
        kind: str,
        rev: Optional[int],
    ) -> Radio:
        """Cria (``radio_id`` ``None``) ou muda uma rádio: canal, bot e tipo. Devolve a rádio gravada (desligada se é nova; sem estação até alguém escolher)."""
        try:
            guild_id, actor_id, channel_id, bot_id = int(guild_id), int(actor_id), int(channel_id), int(bot_id)
        except TypeError, ValueError:
            raise RadioInvalid(RuleError(CHANNEL_INVALID, "channel_id")) from None
        await self.check_actor(guild_id, actor_id)
        current = await self._current(guild_id, radio_id, rev)
        kind = self._check_kind(kind)
        existing = await self.hub.store.list_for_guild(guild_id)
        others = [r for r in existing if current is None or r.id != current.id]
        wanted = (channel_id, bot_id, kind)
        if current is not None and current.enabled and (current.channel_id, current.bot_id, current.kind) != wanted:
            field = (
                "channel_id" if current.channel_id != channel_id else "bot_id" if current.bot_id != bot_id else "kind"
            )
            raise RadioInvalid(RuleError(RADIO_ON, field))  # ligada: desligar antes de trocar canal, bot ou tipo
        bot = self._check_bot(guild_id, bot_id, current=current)
        if any(r.bot_id == bot_id for r in others):
            raise RadioInvalid(RuleError(ONE_PER_BOT, "bot_id"))
        if any(r.channel_id == channel_id for r in others):
            raise RadioInvalid(
                RuleError(CHANNEL_TAKEN, "channel_id")
            )  # uma rádio por canal: o ``on`` e o ``off`` acham a rádio pelo canal
        if current is None and len(existing) >= MAX_PER_GUILD:
            raise RadioInvalid(RuleError(MAX_RADIOS, "bot_id"))
        await self._check_channel(guild_id, channel_id, bot)
        if current is not None and (current.channel_id, current.bot_id, current.kind) == wanted:
            return current  # nada mudou: sem escrita, sem novo rev
        return await self._write(guild_id, actor_id, current, channel_id, bot_id, kind)

    async def _current(self, guild_id: int, rid: Optional[str], rev: Optional[int]) -> Optional[Radio]:
        """A rádio que o pedido muda: de OUTRO servidor é 404 (nunca se confirma que existe) e ``rev`` velho é conflito."""
        if rid is None:
            return None
        parts = split_radio_id(rid)
        if parts is None or parts[1] != guild_id:
            raise RadioNotFound
        current = await self.hub.store.get(rid)
        if current is None:
            raise RadioNotFound
        if rev is None or current.rev != rev:
            raise RadioConflict
        return current

    async def _write(
        self, guild_id: int, actor_id: int, current: Optional[Radio], channel_id: int, bot_id: int, kind: str
    ) -> Radio:
        store = self.hub.store
        by = str(actor_id)
        if current is None:
            return await store.create(_new_radio(radio_id(bot_id, guild_id), channel_id, kind), updated_by=by)
        # outro tipo, outra fonte: a estação escolhida num tipo não vale no outro; outro canal, outro painel
        moved = dataclasses.replace(
            current,
            channel_id=channel_id,
            kind=kind,
            source={} if kind != current.kind else dict(current.source),
            panel_message_id=None if channel_id != current.channel_id else current.panel_message_id,
            clock_started_at=None,
        )
        if bot_id == current.bot_id:
            return await store.save_config(moved, expected_rev=current.rev, updated_by=by)
        # outro bot é outro documento (``bot:guild``): grava o novo com CAS e só então apaga o antigo, que estava desligado
        created = await store.create(
            dataclasses.replace(moved, id=radio_id(bot_id, guild_id), bot_id=bot_id, panel_message_id=None),
            updated_by=by,
        )
        await self._delete_panel(current)
        try:
            await store.delete(current.id)
        except RadioNotFound:
            pass
        return created

    # ------------------------------------------------------------------ redefinir

    async def reset(self, guild_id: int, actor_id: int, radio_id: str, rev: Optional[int]) -> None:
        """Desliga (se ligada), apaga o painel da call, guarda uma cópia em ``global.config_backup`` (30 dias) e apaga o documento da rádio.

        Ficam as estações do servidor, o premium, o ``radio_only`` do bot, a saúde das estações e as outras rádios.
        """
        guild_id, actor_id = int(guild_id), int(actor_id)
        await self.check_actor(guild_id, actor_id)
        current = await self._current(guild_id, radio_id, rev)
        if current is None:
            raise RadioNotFound
        if current.enabled:
            await self.hub.store.set_enabled(
                current.id, False, updated_by=str(actor_id)
            )  # devolve a vaga (transição da R1)
        await self.hub.manager.stop_radios(self.hub.manager.running_in(guild_id, current.channel_id))
        await self._delete_panel(current)
        await self._backup(current, actor_id)
        await self.hub.store.delete(current.id)

    async def _backup(self, radio: Radio, actor_id: int) -> None:
        """A cópia do documento que o reset vai apagar. Sem a cópia gravada o documento NÃO é apagado (a falha sobe)."""
        database = self.pool.database
        now = time.time()
        document = {
            "radio": to_document(radio, updated_by=str(actor_id)),
            "at": now,
            "guild_id": str(radio.guild_id),
            "expire_at": datetime.datetime.fromtimestamp(now + CONFIG_BACKUP_TTL_S, tz=datetime.timezone.utc),
        }
        try:
            index = getattr(database, "ensure_event_indexes", None)
            if index is not None:
                await index(db_name=CONFIG_BACKUP_DB, collection=GLOBAL_DB)  # o TTL de ``expire_at``
            saved = await database.save_doc_cas(
                f"radio_reset:{radio.id}:{int(now)}",
                document,
                expected_rev=None,
                db_name=CONFIG_BACKUP_DB,
                collection=GLOBAL_DB,
            )
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Cópia da rádio antes do reset não gravada (%s)", type(error).__name__)
            raise RadioUnavailable from error
        if not saved:
            raise RadioConflict

    async def _delete_panel(self, radio: Radio) -> None:
        """Apaga a mensagem do painel da call, se ainda existe. Falha aqui (sem permissão, já apagada) nunca impede o reset."""
        if radio.panel_message_id is None:
            return
        bot = next((b for b in self._bots() if b.user.id == radio.bot_id), None)
        guild = bot.get_guild(radio.guild_id) if bot is not None else None
        channel = guild.get_channel(radio.channel_id) if guild is not None else None
        if channel is None:
            return
        try:
            await channel.get_partial_message(radio.panel_message_id).delete()
        except disnake.HTTPException as error:
            logger.info("Painel da rádio não apagado (%s)", type(error).__name__)


def _new_radio(rid: str, channel_id: int, kind: str) -> Radio:
    """A rádio nova: desligada, sem estação ainda."""
    checked = check_radio({"_id": rid, "channel_id": str(channel_id), "kind": kind, "source": {}})
    if isinstance(checked, RuleError):
        raise RadioInvalid(checked)
    return checked
