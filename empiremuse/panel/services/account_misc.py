"""Seções da conta: prefixo pessoal (U-01), scrobble do Last.fm (U-04) e token do app de RPC (U-05).

Espelham ``legacy_cmds.setuserprefix/resetuserprefix``, ``cogs/lastfm.py`` e ``cogs/music_settings.py``
(``rpc``). O painel é mais estrito onde o briefing manda: o prefixo recusa espaços e caracteres de controle, e o token
é gerado com a mesma fonte criptográfica usada pelo comando. ``sessionkey`` e o token nunca são lidos para a tela nem logados.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any, Optional

from empiremuse.panel.services.account_base import AccountContext, AccountSection, Parsed, Refusal
from empiremuse.panel.services.base import Args, first, flag
from empiremuse.rules import limits
from empiremuse.rules.errors import PREFIX_LENGTH, RuleError
from empiremuse.rules.rpc import generate_rpc_token
from empiremuse.rules.settings import validate_prefix

logger = logging.getLogger(__name__)

MSG_PREFIX_LENGTH = f"O prefixo deve ter de 1 a {limits.PREFIX_MAX_LENGTH} caracteres."
MSG_PREFIX_CHARS = "O prefixo não pode conter espaços nem caracteres de controle."
MSG_LASTFM_UNLINKED = "Nenhuma conta do Last.fm está vinculada. Vincule pelo Discord e tente novamente."
MSG_RPC_UNAVAILABLE = "O token do app de RPC não está disponível neste bot."


class PrefixSection(AccountSection):
    key = "prefixo"
    actions = ("prefixo",)

    def values(self, doc: dict) -> dict[str, Any]:
        return {"custom_prefix": doc.get("custom_prefix") or ""}

    def defaults(self, doc: dict) -> dict[str, Any]:
        return dict(self.values(doc))

    async def parse(self, ctx: AccountContext, doc: dict, action: str, args: Args) -> Parsed:
        sent = first(args, "custom_prefix") or ""
        echo = {"custom_prefix": sent}
        if not sent.strip():
            return Parsed({"custom_prefix": ""}, {}, echo)  # vazio = remover (como resetuserprefix)
        prefix = validate_prefix(sent)
        if isinstance(prefix, RuleError):
            message = MSG_PREFIX_LENGTH if prefix.code == PREFIX_LENGTH else MSG_PREFIX_CHARS
            return Parsed({}, {"custom_prefix": message}, echo)
        return Parsed({"custom_prefix": prefix}, {}, echo)

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["custom_prefix"] = new["custom_prefix"]

    async def effects(self, ctx: AccountContext, new: dict[str, Any]) -> None:
        ctx.pool.user_prefix_cache[ctx.user_id] = new["custom_prefix"]  # chave int, como o comando


def _lastfm(doc: dict) -> dict:
    return doc.get("lastfm") or {}


class LastFmSection(AccountSection):
    key = "lastfm"
    actions = ("lastfm_scrobble",)

    @staticmethod
    def linked(doc: dict) -> bool:
        """Vinculada = ``sessionkey`` não vazia (o botão do comando só existe com ela); o valor nunca sai daqui."""
        return bool(_lastfm(doc).get("sessionkey"))

    def values(self, doc: dict) -> dict[str, Any]:
        return {"scrobble": bool(_lastfm(doc).get("scrobble"))}

    def version_fields(self, doc: dict) -> dict[str, Any]:
        return {"username": str(_lastfm(doc).get("username") or ""), **self.values(doc)}

    def defaults(self, doc: dict) -> dict[str, Any]:
        return dict(self.values(doc))

    async def extras(self, ctx: AccountContext, doc: dict) -> dict[str, Any]:
        linked = self.linked(doc)
        return {"vinculada": linked, "usuario": str(_lastfm(doc).get("username") or "") if linked else ""}

    async def guard(self, ctx: AccountContext, doc: dict, action: str) -> Optional[Refusal]:
        return None if self.linked(doc) else Refusal(422, MSG_LASTFM_UNLINKED)

    async def parse(self, ctx: AccountContext, doc: dict, action: str, args: Args) -> Parsed:
        scrobble = flag(args, "scrobble")
        return Parsed({"scrobble": scrobble}, {}, {"scrobble": scrobble})

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["lastfm"]["scrobble"] = new["scrobble"]  # sessionkey e username ficam intactos


async def rpc_available(ctx: AccountContext) -> bool:
    """Mesma condição em que o comando mostra os botões de token (``ENABLE_RPC_AUTH`` + URL + comando/dono)."""
    config = ctx.bot.config
    if not config["ENABLE_RPC_AUTH"] or not (config["RPC_PUBLIC_URL"] or config["RPC_SERVER"]):
        return False
    if config["ENABLE_RPC_COMMAND"]:
        return True
    for bot in ctx.pool.bots:
        if bot.is_ready() and await bot.is_owner(ctx.user):
            return True
    return False


class RpcTokenSection(AccountSection):
    key = "rpc"
    actions = ("rpc_token_gerar",)

    def values(self, doc: dict) -> dict[str, Any]:
        return {"token": doc.get("token") or ""}  # só para comparar; nunca exibido, logado nem usado na versão

    def version_fields(self, doc: dict) -> dict[str, Any]:
        return {"configurado": bool(doc.get("token"))}

    async def extras(self, ctx: AccountContext, doc: dict) -> dict[str, Any]:
        return {"disponivel": await rpc_available(ctx), "configurado": bool(doc.get("token")), "token_novo": None}

    async def guard(self, ctx: AccountContext, doc: dict, action: str) -> Optional[Refusal]:
        return None if await rpc_available(ctx) else Refusal(403, MSG_RPC_UNAVAILABLE)

    async def parse(self, ctx: AccountContext, doc: dict, action: str, args: Args) -> Parsed:
        token = generate_rpc_token()
        return Parsed({"token": token}, {}, {}, {"token_novo": token})

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["token"] = new["token"]

    async def before_store(self, ctx: AccountContext, doc: dict) -> None:
        """Como o comando: trocar um token existente primeiro fecha a presença RPC do usuário."""
        if doc.get("token"):
            await close_presence(ctx)


async def close_presence(ctx: AccountContext) -> None:
    for bot in ctx.pool.bots:
        if not bot.is_ready():
            continue
        for player in list(bot.music.players.values()):
            await _close_player(ctx, bot, player)


async def _close_player(ctx: AccountContext, bot: Any, player: Any) -> None:
    try:
        if ctx.user_id not in player.guild.me.voice.channel.voice_states:
            return
    except AttributeError:
        return
    stats = {
        "op": "close",
        "bot_id": bot.user.id,
        "bot_name": str(bot.user),
        "thumb": bot.user.display_avatar.replace(size=512, static_format="png").url,
    }
    try:
        await player._send_rpc_data([ctx.user_id], stats)
    except Exception as exc:  # um player com problema não impede o novo token; só o tipo vai ao log
        logger.warning("Falha ao fechar a presença RPC (%s)", type(exc).__name__)
