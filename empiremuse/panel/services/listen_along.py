"""Categoria Geral · "Ouvir junto" (G-15): listar e remover convites (``listen_along_invites``, escopo global).

Adicionar convites fica fora da v1 (exige ``fetch_invite`` e voz). Remover é capacidade nova do painel; exige a mesma
permissão do comando (``manage_channels``), reavaliada na escrita.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional

import disnake

from empiremuse.panel.services.base import DIGITS, Args, ForbiddenAction, FormContext, ParseResult, SettingsForm, first
from empiremuse.panel.services.player import active_player

logger = logging.getLogger(__name__)

ACTION = "remover_ouvir_junto"
MSG_ACTION = "Ação desconhecida."
MSG_CHANNEL = "Convite não encontrado para esse canal."


class InviteRow(NamedTuple):
    canal_id: str
    canal_nome: Optional[str]
    convite: str


def can_manage_channels(guild: Any, member: Any, user_id: int) -> bool:
    """Dono, administrador ou ``manage_channels`` (a permissão que o comando exige)."""
    if member is None:
        return False
    permissions = member.guild_permissions
    return bool(guild.owner_id == user_id or permissions.administrator or permissions.manage_channels)


class ListenAlongForm(SettingsForm):
    key = "ouvir_junto"
    category = "geral"
    scope_kind = "global"
    fields = ("listen_along_invites",)
    markers = ("acao",)
    template_alias = "ouvir_junto"

    @property
    def template(self) -> str:
        return "bloco_ouvir_junto.html"

    @property
    def form_id(self) -> str:
        return "bloco-ouvir-junto"

    def extract(self, doc: dict) -> dict[str, Any]:
        return {"listen_along_invites": {str(key): value for key, value in doc["listen_along_invites"].items()}}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["listen_along_invites"] = dict(new["listen_along_invites"])

    def aux(self, ctx: FormContext, doc: dict) -> dict[str, Any]:
        member = ctx.guild.get_member(int(ctx.user_id)) if ctx.user_id.isdigit() else None
        return {"pode_remover": can_manage_channels(ctx.guild, member, int(ctx.user_id)) if member else False}

    async def guard(self, ctx: FormContext, args: Args) -> None:
        if first(args, "acao") != ACTION:
            return  # a ação inválida vira 422 na validação
        if not ctx.user_id.isdigit() or not await self._member_can_manage(ctx, int(ctx.user_id)):
            raise ForbiddenAction

    @staticmethod
    async def _member_can_manage(ctx: FormContext, user_id: int) -> bool:
        member = ctx.guild.get_member(user_id)
        if member is None:
            try:
                member = await ctx.guild.fetch_member(user_id)
            except disnake.HTTPException, OSError:
                return False
        return can_manage_channels(ctx.guild, member, user_id)

    def _channel_name(self, ctx: FormContext, channel_id: str) -> Optional[str]:
        if not DIGITS.fullmatch(channel_id):
            return None
        for bot in ctx.pool.get_all_bots():
            channel = bot.get_channel(int(channel_id))
            if channel is not None:
                return str(channel.name)
        return None

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        rows = tuple(
            InviteRow(channel_id, self._channel_name(ctx, channel_id), str(invite))
            for channel_id, invite in current["listen_along_invites"].items()
        )
        return {"itens": rows, "pode_remover": bool(current["pode_remover"])}

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        channel_id = first(args, "canal") or ""
        echo = {"canal": channel_id}
        invites = current["listen_along_invites"]
        if first(args, "acao") != ACTION:
            return ParseResult({}, {"acao": MSG_ACTION}, echo)
        if not DIGITS.fullmatch(channel_id) or channel_id not in invites:
            return ParseResult({}, {"canal": MSG_CHANNEL}, echo)
        remaining = {key: value for key, value in invites.items() if key != channel_id}
        return ParseResult({"listen_along_invites": remaining}, {}, echo)

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        removed = {int(key) for key in current["listen_along_invites"] if key not in new["listen_along_invites"]}
        for bot in ctx.pool.get_all_bots():
            player = active_player(bot, ctx.guild_id)
            if player is not None:
                await self._clear_player(player, removed)

    @staticmethod
    async def _clear_player(player: Any, removed: set[int]) -> None:
        try:
            if player.guild.me.voice.channel.id not in removed:
                return
            player.listen_along_invite = ""
            await player.process_rpc()
            await player.process_save_queue()
        except AttributeError:
            return  # player sem canal de voz
        except Exception as exc:  # o dado já foi gravado: um player com falha não impede os demais
            logger.warning("Falha ao limpar o convite de ouvir junto no player (%s)", type(exc).__name__)
