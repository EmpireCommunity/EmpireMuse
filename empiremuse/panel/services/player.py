"""Categorias Player (G-01…G-04) e DJ (G-05)."""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from empiremuse.panel.services.base import Args, FormContext, ParseResult, SettingsForm, flag, first
from empiremuse.rules import limits
from empiremuse.rules.errors import ROLE_DUPLICATE, ROLE_LIMIT
from empiremuse.rules.settings import check_dj_roles, order_dj_roles, parse_volume

logger = logging.getLogger(__name__)

MSG_VOLUME = "Escolha um volume válido: de 5 a 100 (de 5 em 5) ou de 110 a 150 (de 10 em 10)."
MSG_ROLE_INVALID = "Selecione apenas cargos que existem neste servidor."
MSG_ROLE_DUPLICATE = "Há cargos repetidos na seleção."
MSG_ROLE_LIMIT = f"Selecione no máximo {limits.MAX_DJ_ROLES} cargos."


def active_player(bot: Any, guild_id: int) -> Any:
    """Player ativo do bot no servidor, ou ``None``."""
    try:
        return bot.music.players[guild_id]
    except KeyError:
        return None


def guild_players(ctx: FormContext) -> list[Any]:
    """Players ativos do servidor em todos os bots do pool (a configuração é do servidor, não de um bot)."""
    players = (active_player(bot, ctx.guild_id) for bot in ctx.pool.get_all_bots())
    return [player for player in players if player is not None]


class PlayerForm(SettingsForm):
    key = "player"
    category = "player"
    scope_kind = "servidor"
    fields = ("default_player_volume", "autoplay", "check_other_bots_in_vc", "enable_restrict_mode")
    markers = fields
    _FLAGS = fields[1:]

    def extract(self, doc: dict) -> dict[str, Any]:
        return {name: doc[name] for name in self.fields}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc.update(new)

    def display(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {**current, "default_player_volume": str(current["default_player_volume"])}

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {"volumes": limits.VOLUMES}

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        raw = first(args, "default_player_volume") or ""
        flags = {name: flag(args, name) for name in self._FLAGS}
        echo = {"default_player_volume": raw, **flags}
        volume = parse_volume(raw)
        if volume is None:
            return ParseResult({}, {"default_player_volume": MSG_VOLUME}, echo)
        return ParseResult({"default_player_volume": volume, **flags}, {}, echo)

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        for player in guild_players(ctx):
            try:
                await player.process_save_queue()
            except Exception as exc:  # o dado já foi gravado: um player com falha não impede os demais
                logger.warning("Falha ao salvar a fila do player depois da configuração (%s)", type(exc).__name__)


class RoleOption(NamedTuple):
    id: str
    name: str


class DjForm(SettingsForm):
    key = "dj"
    category = "dj"
    scope_kind = "servidor"
    fields = ("djroles",)
    markers = fields

    def extract(self, doc: dict) -> dict[str, Any]:
        return {"djroles": [str(role_id) for role_id in doc["djroles"]]}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["djroles"] = list(new["djroles"])

    async def store(self, ctx: FormContext, doc: dict, before: dict) -> None:
        """Cargos entram e saem um a um dentro da trava do store; nunca regrava a lista inteira lida antes (S-V3)."""
        # ids em texto, como o banco e o resto do código gravam
        kept = set(doc["djroles"])
        for role_id in before["djroles"]:
            if role_id not in kept:
                await ctx.pool.servers.remove_djrole(ctx.guild_id, role_id)
        already = set(before["djroles"])
        for role_id in doc["djroles"]:
            if role_id not in already:
                await ctx.pool.servers.add_djrole(ctx.guild_id, role_id)

    def _roles(self, ctx: FormContext) -> tuple[RoleOption, ...]:
        everyone = ctx.guild.default_role.id
        return tuple(RoleOption(str(role.id), role.name) for role in ctx.guild.roles if role.id != everyone)

    def display(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        existing = {role.id for role in self._roles(ctx)}
        return {"djroles": [role_id for role_id in current["djroles"] if role_id in existing]}

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {"cargos": self._roles(ctx)}

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        sent = list(args.get("djroles", []))
        error = self._validate(ctx, sent)
        if error:
            return ParseResult({}, {"djroles": error}, {"djroles": sent})
        ordered = order_dj_roles(current["djroles"], sent)  # o que já estava gravado mantém a posição
        return ParseResult({"djroles": ordered}, {}, {"djroles": sent})

    def _validate(self, ctx: FormContext, sent: list[str]) -> str:
        error = check_dj_roles(sent, {role.id for role in self._roles(ctx)})
        if error is None:
            return ""
        return {ROLE_LIMIT: MSG_ROLE_LIMIT, ROLE_DUPLICATE: MSG_ROLE_DUPLICATE}.get(error.code, MSG_ROLE_INVALID)
