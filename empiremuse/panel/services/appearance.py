"""Categoria Aparência: skins do servidor (G-08, G-09; doc único, valem para todos os bots), skins globais (G-10, G-11) e a
animação do disco das skins de imagem (2.3, documento global ``guilds``: ``disc_motion``; ``None`` = nunca escolhido = ligada).

As listas de skins vêm do que o bot carregou (o registro 2.3 entra pelo ``load_muse_skins``); os nomes das skins do EmpireMuse
saem do catálogo (``skin.name.*``), só para exibir.

As skins personalizadas do servidor não são lidas nem gravadas pelo painel: um valor atual "> custom_skin: X" só é
exibido e mantido se o formulário reenviar exatamente esse valor.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from empiremuse.panel.services.base import (
    DBModel,
    Args,
    FormContext,
    ParseResult,
    SettingsForm,
    first,
    flag,
    panel_translator,
)
from empiremuse.panel.services.player import active_player, guild_players
from empiremuse.rules import limits
from empiremuse.rules.settings import allowed_names
from empiremuse.rules.skins import JUKEBOX_SKINS, disc_motion_on, jukebox_choice, jukebox_skin, saved_player_skins

logger = logging.getLogger(__name__)

MSG_SKIN = "Escolha uma skin da lista."


def allowed_skins(bot: Any) -> tuple[str, ...]:
    """Skins do modo normal, menos as de IGNORE_SKINS (lido da config a cada requisição, como o comando)."""
    return allowed_names(bot.player_skins, bot.config["IGNORE_SKINS"])


def allowed_static_skins(bot: Any) -> tuple[str, ...]:
    return allowed_names(bot.player_static_skins, bot.config["IGNORE_STATIC_SKINS"])


def skin_names(pool: Any) -> dict[str, str]:
    """Nome de exibição das skins do EmpireMuse (``id -> nome`` no idioma do painel); as Clássicas mostram o id."""
    tr = panel_translator(pool)
    return {entry.id: tr(entry.name_key) for entry in JUKEBOX_SKINS}


def _skin_extras(ctx: FormContext) -> dict[str, Any]:
    return {
        "skins": allowed_skins(ctx.bot),
        "skins_estaticas": allowed_static_skins(ctx.bot),
        "nomes_skins": skin_names(ctx.pool),
    }


def _pick(raw: Optional[str], allowed: tuple[str, ...], stored: Optional[str]) -> bool:
    """Skin aceita se estiver na lista ou for exatamente o valor já gravado (reenviar não é trocar)."""
    return bool(raw) and (raw in allowed or raw == stored)


async def reload_skin(player: Any, skin: str, static_skin: str, text: str) -> None:
    """Recarrega a skin de um player ativo como o comando (music_settings.py), se a skin relevante mudou."""
    target, current = (static_skin, player.skin_static) if player.static else (skin, player.skin)
    if target == str(current):
        return
    if skin.startswith(limits.CUSTOM_SKIN_PREFIX) or static_skin.startswith(limits.CUSTOM_SKIN_PREFIX):
        logger.info("Skin personalizada não é recarregada pelo painel; use o Discord para aplicá-la.")
        return
    try:
        await player.destroy_message()
    except Exception as exc:
        logger.warning("Falha ao remover a mensagem do player (%s)", type(exc).__name__)
    try:
        player.skin = skin
        player.skin_static = static_skin
        player.setup_features()
        player.setup_hints()
        player.process_hint()
        player.set_command_log(text=text, emoji="🎨")
        await player.invoke_np(force=True)
    except Exception as exc:  # o dado já foi gravado: um player com falha não impede os demais
        logger.warning("Falha ao recarregar a skin do player (%s)", type(exc).__name__)


class SkinsServerForm(SettingsForm):
    key = "skin_servidor"
    category = "aparencia"
    scope_kind = "servidor"
    fields = ("skin", "static_skin")
    markers = fields

    def extract(self, doc: dict) -> dict[str, Any]:
        return {"skin": doc["skin"], "static_skin": doc["static_skin"]}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc.update(new)

    def display(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {
            "skin": jukebox_skin(current["skin"], ctx.pool.default_skin),
            "static_skin": jukebox_skin(current["static_skin"], ctx.pool.default_static_skin),
        }

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return _skin_extras(ctx)

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        shown = self.display(ctx, current)
        echo = {name: first(args, name) or "" for name in self.fields}
        lists = {"skin": allowed_skins(ctx.bot), "static_skin": allowed_static_skins(ctx.bot)}
        errors = {name: MSG_SKIN for name in self.fields if not _pick(echo[name], lists[name], shown[name])}
        return ParseResult({} if errors else dict(echo), errors, echo)

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        global_data = await ctx.bot.get_global_data(ctx.guild_id, db_name=DBModel.guilds)
        if global_data["global_skin"]:
            return  # no modo global os players seguem a skin global
        for player in guild_players(ctx):
            await reload_skin(player, new["skin"], new["static_skin"], "O painel alterou a skin do player.")


class SkinsGlobalForm(SettingsForm):
    key = "skin_global"
    category = "aparencia"
    scope_kind = "global"
    fields = ("global_skin", "player_skin", "player_skin_static")
    markers = ("player_skin", "player_skin_static")

    def extract(self, doc: dict) -> dict[str, Any]:
        return {
            "global_skin": bool(doc["global_skin"]),
            "player_skin": doc["player_skin"],
            "player_skin_static": doc["player_skin_static"],
        }

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc.update(new)

    def display(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {
            "global_skin": current["global_skin"],
            "player_skin": jukebox_choice(current["player_skin"]) or "",
            "player_skin_static": jukebox_choice(current["player_skin_static"]) or "",
        }

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return _skin_extras(ctx)

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        echo = {
            "global_skin": flag(args, "global_skin"),
            "player_skin": first(args, "player_skin"),
            "player_skin_static": first(args, "player_skin_static"),
        }
        lists = {"player_skin": allowed_skins(ctx.bot), "player_skin_static": allowed_static_skins(ctx.bot)}
        errors = {
            name: MSG_SKIN
            for name, allowed in lists.items()
            if echo[name] is None or (echo[name] != "" and not _pick(echo[name], allowed, current[name]))
        }
        echo = {name: ("" if value is None else value) for name, value in echo.items()}
        if errors:
            return ParseResult({}, errors, echo)
        return ParseResult(
            {
                "global_skin": echo["global_skin"],
                "player_skin": echo["player_skin"] or None,
                "player_skin_static": echo["player_skin_static"] or None,
            },
            {},
            echo,
        )

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        players = guild_players(ctx)
        if not players:
            return
        skin, static_skin = await self._effective(ctx, new)
        for player in players:
            await reload_skin(player, skin, static_skin, "O painel alterou a skin do player.")

    @staticmethod
    async def _effective(ctx: FormContext, new: dict[str, Any]) -> tuple[str, str]:
        """Skins que os players devem usar, com a mesma precedência da criação do player (``saved_player_skins``): no modo
        global, a escolha global; sem ela, a do servidor; por fim o padrão do pool."""
        own = await ctx.pool.servers.settings(ctx.guild_id)
        skin, static_skin = saved_player_skins(own["skin"], own["static_skin"], new)
        return (
            jukebox_skin(skin, ctx.pool.default_skin),
            jukebox_skin(static_skin, ctx.pool.default_static_skin),
        )


class DiscMotionForm(SettingsForm):
    """A animação do disco nas skins de imagem (2.3): um booleano no documento global do servidor. Desligada, o cartão sai
    sempre parado (PNG). O render guarda o valor por até 60 s (``np_steps._disc_motion``) e o relê no redesenho; o ``effects``
    (``last_data = {}``) força esse redesenho, então os players mudam no próximo ciclo."""

    key = "disco"
    category = "aparencia"
    scope_kind = "global"
    fields = ("disc_motion",)
    markers = ("disco",)  # o checkbox desmarcado não vai no POST: o formulário se identifica por um campo oculto

    def extract(self, doc: dict) -> dict[str, Any]:
        return {"disc_motion": doc.get("disc_motion")}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc.update(new)

    def display(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {"disc_motion": disc_motion_on(current["disc_motion"])}

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        echo = {"disc_motion": flag(args, "disc_motion")}
        if echo["disc_motion"] == self.display(ctx, current)["disc_motion"]:
            return ParseResult({"disc_motion": current["disc_motion"]}, {}, echo)  # salvar sem trocar não vira escolha
        return ParseResult(dict(echo), {}, echo)

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        for player in guild_players(ctx):
            player.last_data = {}  # o cartão se redesenha no próximo ciclo do updater com o formato novo
            player.update = True
