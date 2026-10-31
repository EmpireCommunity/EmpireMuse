# -*- coding: utf-8 -*-
"""``/playlists importar`` e ``/playlists exportar``: as playlists do servidor (item 17).

Quem pode: dono do servidor, Administrador ou Gerenciar Servidor, conferido NA EXECUÇÃO de todo comando e de todo modal (nunca só pelo
``default_member_permissions``, nunca atalho do dono do bot). O servidor vem sempre de ``inter.guild_id``. Toda resposta é efêmera, sem
menção (``AllowedMentions.none()``) e nunca traz texto de erro de exceção. Origens: arquivo do bot (``data/playlists``), anexo ``.json``,
link (``utils/server_playlists_fetch``, defesa contra SSRF) ou texto colado. O link nunca é repetido na resposta nem vai para o log.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING, Optional, Union

import disnake
from disnake.ext import commands

from empiremuse.rules.errors import (
    FILE_FORMAT,
    FILE_TOO_LARGE,
    FILE_TOO_MANY,
    IMPORT_FETCH_FAILED,
    IMPORT_URL_REFUSED,
    NAME_INVALID,
    RuleError,
)
from empiremuse.rules.limits import PLAYLIST_FILE_MAX_BYTES, PLAYLIST_PASTE_MAX
from empiremuse.rules.playlists import (
    ParsedFile,
    check_attachment,
    check_import_url,
    check_paste,
    format_report,
    may_manage,
    parse_file,
)
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.server_playlists_fetch import fetch_import
from empiremuse.utils.server_playlists_files import IMPORT_DIR, list_import_files, read_import_file

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

NO_MENTIONS = disnake.AllowedMentions.none()
NOT_ALLOWED = "**Só o dono do servidor, quem é Administrador ou quem tem Gerenciar Servidor pode usar isto.**"
ERROR_TEXT = {
    FILE_TOO_LARGE: "**O arquivo é grande demais** (máximo de {limit} bytes).",
    FILE_FORMAT: "**O conteúdo não é um arquivo de playlists válido.**",
    FILE_TOO_MANY: "**O arquivo tem itens demais** (máximo de {limit}).",
    NAME_INVALID: "**Esse arquivo não está disponível para importação.**",
    IMPORT_URL_REFUSED: "**Esse link não é aceito.** Use um link https de raw.githubusercontent.com, gist.githubusercontent.com, "
    "cdn.discordapp.com ou media.discordapp.net.",
    IMPORT_FETCH_FAILED: "**Não consegui baixar o arquivo desse link agora.** Confira o link e tente de novo.",
}


def member_may_manage(member: Optional[disnake.Member], guild: Optional[disnake.Guild]) -> bool:
    """O membro gerencia as playlists? Dono do servidor, Administrador ou Gerenciar Servidor (permissões do SERVIDOR, não do canal)."""
    if member is None or guild is None or not hasattr(member, "guild_permissions"):
        return False
    perms = member.guild_permissions
    return may_manage(guild.owner_id == member.id, perms.administrator, perms.manage_guild)


def ensure_manage(inter: Union[disnake.ApplicationCommandInteraction, disnake.ModalInteraction]) -> None:
    """Levanta ``GenericError`` se quem usou não pode gerenciar as playlists (rechecado em todo comando e modal)."""
    if not member_may_manage(inter.author if isinstance(inter.author, disnake.Member) else None, inter.guild):
        raise GenericError(NOT_ALLOWED)


def _manage_check(inter: disnake.ApplicationCommandInteraction) -> bool:
    """Cheque do comando: roda ANTES do cooldown (quem não pode não gasta o cooldown do servidor dos admins)."""
    ensure_manage(inter)
    return True


def error_message(error: RuleError) -> str:
    return ERROR_TEXT.get(error.code, ERROR_TEXT[FILE_FORMAT]).format(limit=error.limit)


async def _file_autocomplete(inter: disnake.ApplicationCommandInteraction, text: str) -> list[str]:
    wanted = text.lower()
    return [name for name in await asyncio.to_thread(list_import_files, IMPORT_DIR) if wanted in name][:25]


class PasteModal(disnake.ui.Modal):
    """Colar o JSON de playlists. O servidor vem de ``inter.guild_id`` do envio, nunca do que foi guardado antes."""

    def __init__(self, cog: ServerPlaylistsCog) -> None:
        self.cog = cog
        super().__init__(
            title="Importar playlists",
            custom_id="server_playlists_paste",
            timeout=300,
            components=[
                disnake.ui.TextInput(
                    label="Cole o JSON das playlists",
                    custom_id="json_data",
                    style=disnake.TextInputStyle.long,
                    min_length=2,
                    max_length=PLAYLIST_PASTE_MAX,
                )
            ],
        )

    async def on_error(self, error: Exception, inter: disnake.ModalInteraction) -> None:
        """Sem isto o disnake só loga: o usuário ficaria sem resposta (negado, store indisponível, conflito)."""
        text = error.text if isinstance(error, GenericError) else "**Não foi possível importar as playlists agora.**"
        if not isinstance(error, GenericError):
            logger.error("Falha ao importar playlists coladas (%s)", type(error).__name__)
        try:
            if inter.response.is_done():
                await inter.edit_original_message(text, allowed_mentions=NO_MENTIONS)
            else:
                await inter.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
        except disnake.HTTPException:
            pass

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        ensure_manage(inter)
        await inter.response.defer(ephemeral=True)
        text = check_paste(inter.text_values.get("json_data"))
        if isinstance(text, RuleError):
            await inter.edit_original_message(error_message(text), allowed_mentions=NO_MENTIONS)
            return
        await self.cog.run_import(inter, parse_file(text, max_bytes=PLAYLIST_FILE_MAX_BYTES))


class ServerPlaylistsCog(commands.Cog):
    emoji = "📚"
    name = "Playlists do servidor"
    desc_prefix = f"[{emoji} {name}] | "

    cooldown = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.guild)

    def __init__(self, bot: BotCore):
        self.bot = bot

    async def run_import(
        self,
        inter: Union[disnake.ApplicationCommandInteraction, disnake.ModalInteraction],
        parsed: ParsedFile | RuleError,
    ) -> None:
        """Grava o arquivo já lido (só acrescenta) e responde com o relatório."""
        if isinstance(parsed, RuleError):
            await inter.edit_original_message(error_message(parsed), allowed_mentions=NO_MENTIONS)
            return
        report = await self.bot.pool.servers.playlists.import_file(inter.guild_id, parsed)
        await inter.edit_original_message(format_report(report), allowed_mentions=NO_MENTIONS)

    @commands.slash_command(
        name="playlists",
        description=f"{desc_prefix}Playlists do servidor.",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    @commands.contexts(guild=True)
    async def playlists(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @playlists.sub_command(
        name="importar",
        description=f"{desc_prefix}Importar playlists de arquivo, anexo, link ou texto (só acrescenta).",
        cooldown=cooldown,
    )
    @commands.check(_manage_check)
    async def import_(
        self,
        inter: disnake.ApplicationCommandInteraction,
        file: Optional[str] = commands.Param(
            name="arquivo",
            description="Um arquivo de playlists que acompanha o bot",
            default=None,
            autocomplete=_file_autocomplete,
        ),
        attachment: Optional[disnake.Attachment] = commands.Param(
            name="anexo", description="Um arquivo .json de playlists (até 64 KiB)", default=None
        ),
        link: Optional[str] = commands.Param(
            name="link",
            description="Link https de um .json (GitHub, Gist ou Discord)",
            default=None,
            max_length=2000,
        ),
    ) -> None:
        ensure_manage(inter)

        if sum(source is not None for source in (file, attachment, link)) > 1:
            raise GenericError("**Escolha só uma origem: o arquivo, o anexo ou o link.**")

        if file is None and attachment is None and link is None:
            await inter.response.send_modal(PasteModal(self))
            return

        if link is not None:
            refused = check_import_url(link)  # antes de deferir: link ruim não gasta busca nem rede
            if isinstance(refused, RuleError):
                raise GenericError(error_message(refused))

        await inter.response.defer(ephemeral=True)

        if attachment is not None:
            refused = check_attachment(
                attachment.filename, attachment.size
            )  # antes de baixar: o tamanho informado já basta
            if refused is not None:
                await inter.edit_original_message(error_message(refused), allowed_mentions=NO_MENTIONS)
                return
            try:
                data = await attachment.read()
            except disnake.HTTPException:  # inclui NotFound: o anexo sumiu ou o Discord falhou
                await inter.edit_original_message(
                    "**Não consegui baixar o anexo. Tente enviar o arquivo de novo.**", allowed_mentions=NO_MENTIONS
                )
                return
        elif link is not None:
            data = await fetch_import(link)
        else:
            data = await asyncio.to_thread(read_import_file, IMPORT_DIR, file)
        if isinstance(data, RuleError):
            await inter.edit_original_message(error_message(data), allowed_mentions=NO_MENTIONS)
            return

        await self.run_import(inter, parse_file(data, max_bytes=PLAYLIST_FILE_MAX_BYTES))

    @playlists.sub_command(
        name="exportar",
        description=f"{desc_prefix}Exportar as playlists do servidor em um arquivo .json.",
        cooldown=cooldown,
    )
    @commands.check(_manage_check)
    async def export_(self, inter: disnake.ApplicationCommandInteraction) -> None:
        ensure_manage(inter)
        await inter.response.defer(ephemeral=True)
        playlists = self.bot.pool.servers.playlists
        if not await playlists.list(inter.guild_id):
            await inter.edit_original_message("**O servidor ainda não tem playlists.**")
            return
        data = await playlists.export_bytes(inter.guild_id)
        await inter.edit_original_message(
            "**Playlists exportadas.** Você pode importar este arquivo em outro servidor.",
            file=disnake.File(fp=io.BytesIO(data), filename=f"playlists-{inter.guild_id}.json"),
            allowed_mentions=NO_MENTIONS,
        )


def setup(bot: BotCore):
    bot.add_cog(ServerPlaylistsCog(bot))
