"""Rádios no banco (item 8, R1): leitura, criação, regras do chat e as transições de ligar/desligar com o contador do servidor.

Coleção ``radios`` do banco ``global``, um documento por bot e servidor (``_id = '<bot_id>:<guild_id>'``). Tudo que muda passa por
compare-and-set em ``rev``. O teto de 3 por servidor (R-2) vale para TODO caminho porque o contador (``PremiumStore.active``) só muda na
TRANSIÇÃO de ``enabled`` que este módulo confirmou: ligar (false → true) e só se casou reserva a vaga (sem vaga ou sem premium, desfaz
a transição); desligar (true → false) e só se casou devolve a vaga. Ligar de novo uma rádio ligada ou desligar duas vezes não mexe no
contador. Na subida ``reconcile`` recalcula o contador a partir das rádios realmente ligadas e desliga o que passa do teto ou do premium.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import MAX_PER_GUILD, Radio, check_chat, check_radio, to_document
from empiremuse.utils.radio.premium import CAS_ATTEMPTS, GLOBAL_DB, PremiumStore, RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

RADIOS_DB = "radios"  # nome da coleção


class RadioConflict(Exception):
    """O ``rev`` mudou (ou a rádio já existe): quem editava leu uma versão velha."""


class RadioInvalid(Exception):
    """``check_radio``/``check_chat`` recusou o valor; ``error`` leva o ``RuleError`` (código estável e o campo)."""

    def __init__(self, error: RuleError) -> None:
        super().__init__(error.code)
        self.error = error


class RadioNotFound(Exception):
    pass


class NoPremium(Exception):
    """O servidor não é premium (ou foi revogado no meio da operação)."""


class NoRoom(Exception):
    """O servidor já tem ``MAX_PER_GUILD`` rádios ligadas."""


@dataclass(frozen=True)
class ReconcileReport:
    without_premium: int = 0  # desligadas por falta de premium
    over_cap: int = 0  # desligadas por passar do teto
    counters_fixed: int = 0  # contadores que estavam errados


class RadioStore:
    def __init__(self, pool: BotPool, premium: PremiumStore) -> None:
        self.pool = pool
        self.premium = premium

    # ------------------------------------------------------------------ banco

    @property
    def _database(self) -> Any:
        return self.pool.database

    async def _find(self, radio_id: str) -> Optional[dict[str, Any]]:
        try:
            return await self._database.find_doc(radio_id, db_name=RADIOS_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Rádio não lida (%s)", type(error).__name__)
            raise RadioUnavailable from error

    async def _save(self, radio_id: str, doc: dict[str, Any], expected_rev: Optional[int]) -> bool:
        try:
            return bool(
                await self._database.save_doc_cas(
                    radio_id, doc, expected_rev=expected_rev, db_name=RADIOS_DB, collection=GLOBAL_DB
                )
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Rádio não gravada (%s)", type(error).__name__)
            raise RadioUnavailable from error

    async def _documents(self, flt: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        try:
            return [doc async for doc in self._database.iter_data(RADIOS_DB, GLOBAL_DB, flt)]
        except Exception as error:  # noqa: BLE001
            logger.warning("Rádios não listadas (%s)", type(error).__name__)
            raise RadioUnavailable from error

    @staticmethod
    def _radio(doc: Optional[Mapping[str, Any]]) -> Optional[Radio]:
        """O ``Radio`` de um documento; documento que não passa em ``check_radio`` é ignorado (e avisado, sem o conteúdo)."""
        if not doc:
            return None
        radio = check_radio(doc)
        if isinstance(radio, RuleError):
            logger.warning("Rádio com documento inválido ignorada (%s)", radio.code)
            return None
        return radio

    # ------------------------------------------------------------------ leitura

    async def get(self, radio_id: str) -> Optional[Radio]:
        return self._radio(await self._find(radio_id))

    async def list_for_guild(self, guild_id: int) -> list[Radio]:
        """Todas as rádios do servidor (de todos os bots), por id."""
        docs = await self._documents({"guild_id": str(int(guild_id))})
        return sorted((r for d in docs if (r := self._radio(d)) is not None), key=lambda r: r.id)

    async def list_enabled(self) -> list[Radio]:
        docs = await self._documents({"enabled": True})
        return sorted((r for d in docs if (r := self._radio(d)) is not None), key=lambda r: r.id)

    # ------------------------------------------------------------------ escrita

    async def create(self, radio: Radio, *, updated_by: str = "") -> Radio:
        """Grava uma rádio NOVA (desligada, ``rev`` 0). Já existir é ``RadioConflict``; ligar é ``set_enabled``."""
        checked = check_radio(to_document(dataclasses.replace(radio, enabled=False, rev=0)))
        if isinstance(checked, RuleError):
            raise RadioInvalid(checked)
        if not await self._save(checked.id, to_document(checked, updated_by=updated_by), None):
            raise RadioConflict
        return checked

    async def update_chat(
        self, radio_id: str, chat: Mapping[str, Any], *, expected_rev: int, updated_by: str = ""
    ) -> Radio:
        """Muda só as regras do chat (R-6: o painel web só consegue isto). ``RadioConflict`` se o ``rev`` já não é ``expected_rev``."""
        doc = await self._find(radio_id)
        current = self._radio(doc)
        if current is None:
            raise RadioNotFound
        if current.rev != expected_rev:
            raise RadioConflict
        merged = check_chat(chat, base=current.chat)
        if isinstance(merged, RuleError):
            raise RadioInvalid(merged)
        updated = dataclasses.replace(current, chat=merged, rev=current.rev + 1)
        checked = check_radio(to_document(updated))
        if isinstance(checked, RuleError):
            raise RadioInvalid(checked)
        if not await self._save(radio_id, to_document(checked, updated_by=updated_by), expected_rev):
            raise RadioConflict
        return checked

    async def reconfigure(self, radio: Radio, *, updated_by: str = "") -> Radio:
        """Troca canal, tipo e fonte de uma rádio DESLIGADA (o comando ``/radio ligar`` reaproveita o documento do bot). Ligada: ``RadioConflict``."""
        current = self._radio(await self._find(radio.id))
        if current is None:
            raise RadioNotFound
        if current.enabled:
            raise RadioConflict
        updated = dataclasses.replace(
            current,
            channel_id=radio.channel_id,
            kind=radio.kind,
            source=dict(radio.source),
            chat=dict(current.chat),  # as regras de chat gravadas pelo painel sobrevivem à troca de canal e fonte
            panel_message_id=None,
            clock_started_at=None,  # outra playlist, outra programação: o relógio recomeça quando ligar
            rev=current.rev + 1,
        )
        checked = check_radio(to_document(updated))
        if isinstance(checked, RuleError):
            raise RadioInvalid(checked)
        if not await self._save(radio.id, to_document(checked, updated_by=updated_by), current.rev):
            raise RadioConflict
        return checked

    async def ensure_clock(self, radio: Radio) -> Radio:
        """A rádio ligada com o relógio virtual gravado: documento antigo (sem ``clock_started_at``) ganha a época de agora, uma vez."""
        if radio.clock_started_at is not None:
            return radio
        for _ in range(CAS_ATTEMPTS):
            current = self._radio(await self._find(radio.id))
            if current is None:
                raise RadioNotFound
            if current.clock_started_at is not None:
                return current
            stamped = dataclasses.replace(current, clock_started_at=time.time(), rev=current.rev + 1)
            if await self._save(radio.id, to_document(stamped), current.rev):
                return stamped
        raise RadioConflict

    async def set_panel_message(self, radio_id: str, message_id: Optional[int]) -> None:
        """Guarda o id da mensagem do painel da rádio (sem conflito para quem chama: relê e tenta de novo)."""
        for _ in range(CAS_ATTEMPTS):
            current = self._radio(await self._find(radio_id))
            if current is None:
                raise RadioNotFound
            updated = dataclasses.replace(current, panel_message_id=message_id, rev=current.rev + 1)
            if await self._save(radio_id, to_document(updated), current.rev):
                return
        raise RadioConflict

    async def delete(self, radio_id: str) -> None:
        """Apaga a rádio; se estava ligada, devolve a vaga (uma transição como outra qualquer)."""
        await self.set_enabled(radio_id, False, updated_by="delete")
        try:
            await self._database.delete_data(radio_id, db_name=RADIOS_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001
            logger.warning("Rádio não apagada (%s)", type(error).__name__)
            raise RadioUnavailable from error

    # ------------------------------------------------------------------ transições (R-2)

    async def set_enabled(self, radio_id: str, enabled: bool, *, updated_by: str = "") -> Radio:
        """Liga ou desliga. Idempotente: pedir o estado em que já está não muda nada, nem o contador.

        **Ligar reserva a vaga ANTES de gravar ``enabled``**, então o contador nunca fica abaixo do número de rádios ligadas (um desligamento
        concorrente não consegue devolver uma vaga que ainda não foi reservada). Se a gravação perde a disputa ou falha, a vaga volta. Depois de
        ligar, o premium é conferido de novo: uma revogação que passou entre a reserva e a gravação não deixa rádio ligada. **Desligar grava
        primeiro e devolve a vaga depois**, só quem ganhou a transição. ``NoPremium``/``NoRoom`` deixam a rádio como estava.
        """
        for _ in range(CAS_ATTEMPTS):
            current = self._radio(await self._find(radio_id))
            if current is None:
                raise RadioNotFound
            if current.enabled == enabled:
                return current
            changed = dataclasses.replace(current, enabled=enabled, rev=current.rev + 1)
            if enabled:  # ligar de verdade começa a programação virtual agora; o reinício do bot a continua dessa época
                changed = dataclasses.replace(changed, clock_started_at=time.time())
            if not enabled:
                if not await self._save(radio_id, to_document(changed, updated_by=updated_by), current.rev):
                    continue  # outro escritor passou na frente: relê (a transição pode já ter sido feita por ele)
                await self.premium.release(current.guild_id)
                return changed
            if not await self.premium.reserve(current.guild_id):
                if await self.premium.fresh(current.guild_id) is None:
                    raise NoPremium
                raise NoRoom
            try:
                won = await self._save(radio_id, to_document(changed, updated_by=updated_by), current.rev)
            except BaseException:
                await self._give_back(current.guild_id)
                raise
            if not won:
                await self._give_back(current.guild_id)
                continue
            if await self.premium.fresh(current.guild_id) is None:  # revogada entre a reserva e a gravação
                await self._force_disable(changed, updated_by)
                raise NoPremium
            return changed
        raise RadioConflict

    async def _give_back(self, guild_id: int) -> None:
        """Devolve a vaga reservada de uma ligação que não aconteceu (falha aqui só deixa o contador alto até a próxima subida)."""
        try:
            await self.premium.release(guild_id)
        except RadioUnavailable:
            logger.warning("Vaga de rádio não devolvida; o recálculo da subida corrige")

    async def _force_disable(self, radio: Radio, updated_by: str) -> bool:
        """Desliga SEM devolver vaga (o contador é recalculado/zerado por quem chama). ``True`` se a transição foi nossa."""
        for _ in range(CAS_ATTEMPTS):
            current = self._radio(await self._find(radio.id))
            if current is None or not current.enabled:
                return False
            off = dataclasses.replace(current, enabled=False, rev=current.rev + 1)
            if await self._save(radio.id, to_document(off, updated_by=updated_by), current.rev):
                return True
        raise RadioConflict

    async def disable_all_in_guild(self, guild_id: int, *, updated_by: str = "system") -> list[Radio]:
        """Desliga as rádios ligadas do servidor, em todos os bots (revogação do premium, R-1). Devolve as que desligou.

        Cada desligamento é uma transição (devolve a vaga, se o documento do premium ainda existe). Quem chama para o runner e o player
        de cada uma: sem isso a rádio segue tocando mesmo desligada no banco.
        """
        off: list[Radio] = []
        for radio in await self.list_for_guild(guild_id):
            if radio.enabled:
                try:
                    result = await self.set_enabled(radio.id, False, updated_by=updated_by)
                except RadioNotFound:
                    continue
                if not result.enabled:
                    off.append(result)
        return off

    async def revoke_premium(self, guild_id: int, *, updated_by: str = "owner") -> list[Radio]:
        """A revogação inteira (R-1): 1) apaga o premium, 2) desliga as rádios do servidor em todos os bots. Devolve as desligadas."""
        await self.premium.revoke(guild_id)
        return await self.disable_all_in_guild(guild_id, updated_by=updated_by)

    # ------------------------------------------------------------------ subida

    async def reconcile(self) -> ReconcileReport:
        """Recalcula o contador de cada servidor a partir das rádios realmente ligadas e desliga o que passa do teto ou do premium."""
        without = over = fixed = 0
        by_guild: dict[int, list[Radio]] = {}
        for radio in await self.list_enabled():
            by_guild.setdefault(radio.guild_id, []).append(radio)
        for guild_id, radios in by_guild.items():
            premium = await self.premium.fresh(guild_id)
            if premium is None:
                for radio in radios:
                    without += int(await self._force_disable(radio, "boot"))
                continue
            kept = radios[:MAX_PER_GUILD]
            for radio in radios[MAX_PER_GUILD:]:
                over += int(await self._force_disable(radio, "boot"))
            if premium.active != len(kept):
                fixed += 1
                await self.premium.set_active(guild_id, len(kept))
        for premium in await self.premium.all():
            if premium.guild_id not in by_guild and premium.active != 0:
                fixed += 1
                await self.premium.set_active(premium.guild_id, 0)  # contador sobrando sem nenhuma rádio ligada
        return ReconcileReport(without_premium=without, over_cap=over, counters_fixed=fixed)
