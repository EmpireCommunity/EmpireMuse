"""Base das rotas de rádio e de estação (B4, contrato T-456 §4 m4): a ordem das conferências é a mesma em todas.

1. sessão e limites (``prepare``); 2. o servidor, agora nas escritas (``fresh=True``) e com o cache de 30 s na leitura, e o
mesmo ``404`` de servidor negado e inexistente; 3. a rádio ou a estação, **sempre** conferida contra o servidor do
caminho (``{r}`` com o sufixo ``:{g}``, estação com ``guild_id`` igual); outro servidor é o mesmo ``404`` de um id que não
existe; 4. premium e ``manage_messages`` no canal, só onde a página do painel também pede.
"""

from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, NamedTuple, Optional

from empiremuse.api.data import DataHandler
from empiremuse.api.errors import BOT_UNAVAILABLE, FORBIDDEN, ApiError, conflict, not_found, validation_failed
from empiremuse.panel.services.base import FormContext, panel_translator
from empiremuse.panel.services.radio import belongs_to, can_write, radio_id_of
from empiremuse.panel.services.radio_stations import belongs_to as station_belongs_to
from empiremuse.panel.services.radio_stations import station_id_of
from empiremuse.rules.i18n import Translator
from empiremuse.utils.radio.premium import RadioUnavailable

STATUS_OK, STATUS_FORBIDDEN, STATUS_NOT_FOUND, STATUS_CONFLICT, STATUS_INVALID = 200, 403, 404, 409, 422
RADIO_ID = r"([0-9]{1,20}:[0-9]{1,20})"
STATION_ID = r"([0-9a-f]{24})"
GUILD_ID = r"([0-9]{1,20})"


def radio_endpoint[**P](method: Callable[P, Awaitable[None]]) -> Callable[P, Awaitable[None]]:
    """O armazém das rádios fora do ar (``RadioUnavailable``) vira ``503 bot_unavailable``, como as outras páginas."""

    @functools.wraps(method)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            await method(*args, **kwargs)
        except RadioUnavailable:
            raise ApiError(503, BOT_UNAVAILABLE) from None

    return wrapper


class RadioContext(NamedTuple):
    ctx: FormContext
    radio: Any
    premium: bool  # o servidor tem premium de rádio


class RadioRoute(DataHandler):
    @property
    def tr(self) -> Translator:
        return panel_translator(self.ctx.pool)

    @staticmethod
    def hub(ctx: FormContext) -> Any:
        hub = getattr(ctx.pool, "radio", None)
        if hub is None:
            raise ApiError(503, BOT_UNAVAILABLE)  # a rádio não está ligada neste bot
        return hub

    async def guild_context(self, guild_id: str, *, write: bool) -> tuple[FormContext, bool]:
        """``(contexto, premium)`` depois de autorizar o servidor."""
        ctx, _ = await self.guild_access(guild_id, write=write)
        premium = can_write(await self.hub(ctx).premium.get(ctx.guild_id))
        return ctx, premium

    async def radio_of(self, ctx: FormContext, radio_id: str) -> Any:
        """A rádio do caminho, **só** se é deste servidor; de outro servidor, malformada ou inexistente: o mesmo ``404``."""
        wanted = radio_id_of(ctx.guild_id, radio_id)
        radio = await self.hub(ctx).store.get(wanted) if wanted is not None else None
        if radio is None or not belongs_to(radio, ctx.guild_id):
            raise not_found()
        return radio

    async def radio_context(self, guild_id: str, radio_id: str, *, write: bool) -> RadioContext:
        ctx, premium = await self.guild_context(guild_id, write=write)
        return RadioContext(ctx, await self.radio_of(ctx, radio_id), premium)

    async def editable_radio(self, guild_id: str, radio_id: str, *, write: bool) -> RadioContext:
        """A rádio que o usuário pode editar: premium **e** ``manage_messages`` no canal (R-4, R-6); senão ``403``."""
        found = await self.radio_context(guild_id, radio_id, write=write)
        if not found.premium or not await self.ctx.radio_page.can_edit(found.ctx, found.radio):
            raise ApiError(403, FORBIDDEN)
        return found

    async def station_context(self, guild_id: str, station_id: str, *, write: bool) -> tuple[FormContext, Any]:
        """A estação do servidor (premium obrigatório); a de outro servidor é ``404``."""
        ctx, premium = await self.guild_context(guild_id, write=write)
        if not premium:
            raise ApiError(403, FORBIDDEN)
        return ctx, await self.station_of(ctx, station_id)

    async def station_of(self, ctx: FormContext, station_id: str) -> Any:
        """A estação do servidor do contexto; a de outro servidor (ou que não existe) é ``404``."""
        wanted = station_id_of(station_id)
        station = await self.hub(ctx).stations.get(ctx.guild_id, wanted) if wanted is not None else None
        if station is None or not station_belongs_to(station, ctx.guild_id):
            raise not_found()
        return station

    @staticmethod
    def require_premium(premium: bool) -> None:
        if not premium:
            raise ApiError(403, FORBIDDEN)

    @staticmethod
    async def current_rev(ctx: FormContext, radio_id: str) -> Optional[str]:
        """O ``rev`` que a rádio tem agora (para o ``409``); ``None`` se ela já não existe."""
        radio = await ctx.pool.radio.store.get(radio_id)
        return str(radio.rev) if radio is not None else None

    async def fail_unless_ok(self, status: int, ctx: FormContext, radio: Any, fields: Any = ()) -> None:
        """O status de um serviço como erro da API; no ``409`` a versão que a rádio tem agora (relida da store)."""
        if status == STATUS_CONFLICT:
            raise conflict(await self.current_rev(ctx, str(radio.id)))
        raise_for_status(status, fields)

    def spend_directory_budget(self) -> None:
        """Busca e lista de local gastam o balde global do diretório: valem como escrita no limite do usuário."""
        self._spend(self.ctx.limits.user_write, self.session.user.id)


def raise_for_status(status: int, fields: Any = (), current_version: Optional[str] = None) -> None:
    """O status de um serviço do painel como o erro da API (``200`` não faz nada)."""
    if status == STATUS_OK:
        return
    if status == STATUS_CONFLICT:
        raise conflict(current_version)
    if status == STATUS_INVALID:
        raise validation_failed(*fields)
    if status == STATUS_FORBIDDEN:
        raise ApiError(403, FORBIDDEN)
    if status == STATUS_NOT_FOUND:
        raise not_found()
    raise ApiError(500, BOT_UNAVAILABLE)  # status que a API não conhece: falha fechada
