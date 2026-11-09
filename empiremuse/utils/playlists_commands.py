"""``/jukebox playlists`` e ``/radio playlists`` (listar, criar, editar, remover): as playlists do servidor de UM escopo cada.

Quem pode, nos dois escopos: dono do servidor, Administrador ou Gerenciar Servidor, conferido NA EXECUÇÃO de todo comando e de todo modal. Não há
checagem de premium aqui (decisão do dono: tocar a rádio é que exige premium). O servidor vem sempre de ``inter.guild_id``; o escopo é o do
comando, nunca um valor vindo do usuário. Toda resposta é efêmera e sem menção.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.cogs.server_playlists import member_may_manage
from empiremuse.rules.errors import (
    NAME_TAKEN,
    PLAYLIST_NOT_FOUND,
    QUOTA_FULL,
    URL_TAKEN,
    RuleError,
)
from empiremuse.rules.i18n import fit, utf16_length
from empiremuse.rules.limits import MAX_SERVER_PLAYLISTS
from empiremuse.rules.playlists import SCOPE_JUKEBOX, SCOPE_RADIO, Playlist, display_name
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

NO_MENTIONS = disnake.AllowedMentions.none()
KNOWN_ERRORS = frozenset({NAME_TAKEN, URL_TAKEN, QUOTA_FULL, PLAYLIST_NOT_FOUND})
LIST_LINE_MAX = 1900
NAME_LIMIT = 100


async def translator(bot: BotCore, inter: Any):
    return await bot.pool.i18n.for_guild(inter.guild)


def ensure_manage(inter: Any, tr: Any) -> None:
    """Levanta ``GenericError`` se quem usou não gerencia as playlists (rechecado em todo comando e modal)."""
    member = inter.author if isinstance(inter.author, disnake.Member) else None
    if not member_may_manage(member, inter.guild):
        raise GenericError(tr("playlists.cmd.denied"))


def refusal(tr: Any, error: RuleError, scope: str) -> str:
    """O texto traduzido da recusa do store ou das regras; erro que não é conhecido vira a mensagem de dado inválido."""
    code = error.code if error.code in KNOWN_ERRORS else "invalid"
    return tr(
        f"playlists.cmd.error.{code}", limit=error.limit or MAX_SERVER_PLAYLISTS, scope=tr(f"playlists.scope.{scope}")
    )


def _line(item: Playlist) -> str:
    icon = f"{item.icon} " if item.icon else ""
    return f"{item.order}. {icon}**{external_text(item.name, NAME_LIMIT)}**"


def chunk_lines(lines: list[str], limit: int) -> list[str]:
    """Junta linhas INTEIRAS em blocos de até ``limit`` unidades UTF-16 (a lista de 100 playlists não cabe numa mensagem de 2000).

    Uma linha maior que ``limit`` é cortada no meio por ``fit``; nenhuma playlist some da lista.
    """
    blocks: list[list[str]] = [[]]
    used = 0
    for line in (fit(line, limit) for line in lines):
        cost = utf16_length(line) + 1
        if blocks[-1] and used + cost > limit:
            blocks.append([])
            used = 0
        blocks[-1].append(line)
        used += cost
    return ["\n".join(block) for block in blocks]


async def names_autocomplete(inter: Any, current: str, scope: str) -> list[str]:
    """Até 25 nomes das playlists DESTE servidor e escopo; quem não gerencia não vê nada."""
    if inter.guild_id is None or not member_may_manage(
        inter.author if isinstance(inter.author, disnake.Member) else None, inter.guild
    ):
        return []
    wanted = current.casefold()
    names = [p.name for p in await inter.bot.pool.servers.playlists.list(inter.guild_id, scope=scope)]
    return [name for name in names if wanted in name.casefold()][:25]


async def jukebox_names(inter: Any, current: str) -> list[str]:
    return await names_autocomplete(inter, current, SCOPE_JUKEBOX)


async def radio_names(inter: Any, current: str) -> list[str]:
    return await names_autocomplete(inter, current, SCOPE_RADIO)


async def list_(bot: BotCore, inter: disnake.ApplicationCommandInteraction, scope: str) -> None:
    tr = await translator(bot, inter)
    ensure_manage(inter, tr)
    await inter.response.defer(ephemeral=True)
    items = await bot.pool.servers.playlists.list(inter.guild_id, scope=scope)
    if not items:
        await inter.edit_original_message(tr(f"playlists.cmd.empty.{scope}"), allowed_mentions=NO_MENTIONS)
        return
    head = tr(
        "playlists.cmd.list.title", scope=tr(f"playlists.scope.{scope}"), count=len(items), max=MAX_SERVER_PLAYLISTS
    )
    first, *rest = chunk_lines([_line(p) for p in items], LIST_LINE_MAX)
    await inter.edit_original_message(f"{head}\n{first}", allowed_mentions=NO_MENTIONS)
    for block in rest:
        await inter.followup.send(block, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def remove(bot: BotCore, inter: disnake.ApplicationCommandInteraction, scope: str, name: str) -> None:
    tr = await translator(bot, inter)
    ensure_manage(inter, tr)
    await inter.response.defer(ephemeral=True)
    result = await bot.pool.servers.playlists.remove(inter.guild_id, name, scope=scope)
    if isinstance(result, RuleError):
        raise GenericError(refusal(tr, result, scope))
    await inter.edit_original_message(
        tr("playlists.cmd.removed", name=external_text(result.name, NAME_LIMIT)), allowed_mentions=NO_MENTIONS
    )


async def create(bot: BotCore, inter: disnake.ApplicationCommandInteraction, scope: str) -> None:
    tr = await translator(bot, inter)
    ensure_manage(inter, tr)
    await inter.response.send_modal(PlaylistModal(bot, tr, scope))


async def edit(bot: BotCore, inter: disnake.ApplicationCommandInteraction, scope: str, name: str) -> None:
    tr = await translator(bot, inter)
    ensure_manage(inter, tr)
    current = await bot.pool.servers.playlists.find(inter.guild_id, name, scope=scope)
    if current is None:
        raise GenericError(refusal(tr, RuleError(PLAYLIST_NOT_FOUND, key=name), scope))
    await inter.response.send_modal(PlaylistModal(bot, tr, scope, current))


class PlaylistModal(disnake.ui.Modal):
    """Criar ou editar. O escopo e o nome original ficam no ``custom_id`` do modal só como estado; o servidor vem do envio e tudo é revalidado."""

    def __init__(self, bot: BotCore, tr: Any, scope: str, current: Optional[Playlist] = None) -> None:
        self.bot, self.tr, self.scope, self.current = bot, tr, scope, current
        text = disnake.TextInputStyle
        fields = [
            ("name", "playlists.cmd.field.name", text.short, current.name if current else "", True, 100),
            ("url", "playlists.cmd.field.url", text.short, current.url if current else "", True, 2000),
            (
                "description",
                "playlists.cmd.field.description",
                text.paragraph,
                current.description if current else "",
                False,
                200,
            ),
            (
                "genres",
                "playlists.cmd.field.genres",
                text.short,
                ", ".join(current.genres) if current else "",
                False,
                200,
            ),
            ("icon", "playlists.cmd.field.icon", text.short, (current.icon or "") if current else "", False, 8),
        ]
        super().__init__(
            title=fit(tr("playlists.cmd.modal.edit" if current else "playlists.cmd.modal.create"), 45),
            custom_id=f"server_playlist_{scope}_{'edit' if current else 'create'}",
            timeout=300,
            components=[
                disnake.ui.TextInput(
                    label=fit(tr(label), 45),
                    custom_id=key,
                    style=style,
                    value=value or None,
                    required=required,
                    max_length=limit,
                )
                for key, label, style, value, required, limit in fields
            ],
        )

    async def on_error(self, error: Exception, inter: disnake.ModalInteraction) -> None:
        text = error.text if isinstance(error, GenericError) else self.tr("playlists.cmd.failed")
        if not isinstance(error, GenericError):
            logger.error("Falha ao salvar a playlist do servidor (%s)", type(error).__name__)
        try:
            if inter.response.is_done():
                await inter.edit_original_message(text, allowed_mentions=NO_MENTIONS)
            else:
                await inter.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except disnake.HTTPException:
            pass

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        ensure_manage(inter, self.tr)  # a permissão pode ter mudado desde que o modal abriu
        await inter.response.defer(ephemeral=True)
        values = inter.text_values
        genres = [part.strip() for part in str(values.get("genres") or "").split(",") if part.strip()]
        item = Playlist(
            self.current.order if self.current else 0,
            str(values.get("name") or ""),
            str(values.get("description") or ""),
            tuple(genres),
            str(values.get("url") or ""),
            str(values.get("icon") or "").strip() or None,
            self.scope,
        )
        playlists = self.bot.pool.servers.playlists
        if self.current is None:
            result = await playlists.add(inter.guild_id, item, scope=self.scope)
        else:
            result = await playlists.edit(inter.guild_id, self.current.name, item, scope=self.scope)
        if isinstance(result, RuleError):
            raise GenericError(refusal(self.tr, result, self.scope))
        key = "playlists.cmd.saved" if self.current else "playlists.cmd.created"
        await inter.edit_original_message(
            self.tr(
                key,
                name=external_text(display_name(result), NAME_LIMIT),
                scope=self.tr(f"playlists.scope.{self.scope}"),
            ),
            allowed_mentions=NO_MENTIONS,
        )
