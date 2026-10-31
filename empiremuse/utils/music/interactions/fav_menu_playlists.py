# -*- coding: utf-8 -*-
"""Playlists do servidor no ``/fav_manager``: permissão, modais e textos, tudo pelo store (``pool.servers.playlists``).

O menu nunca grava no doc por bot. O servidor vem sempre de ``inter.guild_id`` e a permissão é rechecada em cada
callback de escrita (``may_manage`` sobre o membro da interação), nunca de ``custom_id``, valor de select ou campo.
Texto de terceiros (nome, URL) só sai em markdown via ``md_link``/``md_code`` e com ``AllowedMentions.none()``.
"""

from __future__ import annotations

import json
import re
from io import BytesIO
from typing import Optional

import disnake

from empiremuse.rules import limits
from empiremuse.rules.errors import (
    DESCRIPTION_INVALID,
    DESCRIPTION_TOO_LONG,
    GENRES_INVALID,
    NAME_INVALID,
    NAME_TAKEN,
    NAME_TOO_LONG,
    ORDER_INVALID,
    PLAYLIST_NOT_FOUND,
    QUOTA_FULL,
    URL_INVALID,
    URL_TAKEN,
    URL_TOO_LONG,
    RuleError,
)
from empiremuse.rules.emoji import split_leading_icon
from empiremuse.rules.playlists import Playlist, display_name, format_report, may_manage, parse_file, to_export
from empiremuse.utils.music.converters import md_link
from empiremuse.utils.music.errors import GenericError

MSG_DENIED = "**Você precisa ser o dono do servidor ou ter a permissão Administrador ou Gerenciar Servidor.**"
MSG_PASTE_BIG = f"**O conteúdo colado passa de {limits.PLAYLIST_PASTE_MAX} caracteres. Use o comando com um anexo.**"
MSG_PASTE_BAD = "**O conteúdo precisa ser um JSON válido, no formato exportado pelo bot.**"
MSG_UNKNOWN = "**Não foi possível salvar: confira os campos.**"

GENRE_SPLIT = re.compile(r"[,;]")

_TEXTS = {
    NAME_TAKEN: "**Já existe uma playlist com esse nome.**",
    URL_TAKEN: "**Já existe uma playlist com esse link.**",
    PLAYLIST_NOT_FOUND: "**Playlist não encontrada.**",
    QUOTA_FULL: f"**A lista já tem o máximo de {limits.MAX_SERVER_PLAYLISTS} playlists; remova alguma antes.**",
    NAME_INVALID: f"**O nome deve ter de {limits.PLAYLIST_NAME_MIN} a {limits.PLAYLIST_NAME_MAX} caracteres.**",
    NAME_TOO_LONG: f"**O nome passa de {limits.PLAYLIST_NAME_MAX} caracteres.**",
    URL_INVALID: "**Informe um endereço http(s) válido.**",
    URL_TOO_LONG: f"**O link passa de {limits.PLAYLIST_URL_MAX} caracteres.**",
    DESCRIPTION_INVALID: (
        f"**A descrição deve ficar vazia ou ter de {limits.PLAYLIST_DESCRIPTION_MIN} a "
        f"{limits.PLAYLIST_DESCRIPTION_MAX} caracteres.**"
    ),
    DESCRIPTION_TOO_LONG: f"**A descrição passa de {limits.PLAYLIST_DESCRIPTION_MAX} caracteres.**",
    GENRES_INVALID: (
        f"**Até {limits.PLAYLIST_GENRES_MAX} gêneros de {limits.PLAYLIST_GENRE_MIN} a {limits.PLAYLIST_GENRE_MAX} "
        "caracteres, separados por vírgula, sem repetir.**"
    ),
    ORDER_INVALID: f"**A ordem deve ser um número de 1 a {limits.PLAYLIST_ORDER_MAX}.**",
}


def can_manage(inter: disnake.Interaction) -> bool:
    """Dono do servidor, Administrador ou Gerenciar Servidor, pelo membro da própria interação (nega se faltar dado)."""
    member = inter.author
    guild = inter.guild
    if guild is None or not isinstance(member, disnake.Member):
        return False
    perms = member.guild_permissions
    return may_manage(guild.owner_id == member.id, perms.administrator, perms.manage_guild)


def error_text(error: RuleError) -> str:
    return _TEXTS.get(error.code, MSG_UNKNOWN)


def split_genres(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in GENRE_SPLIT.split(raw) if part.strip())


def find_by_name(playlists: tuple[Playlist, ...], name: str) -> Optional[Playlist]:
    key = name.casefold()
    return next((item for item in playlists if item.name.casefold() == key), None)


def parse_order(raw: str, default: int) -> Optional[int]:
    """Ordem digitada: vazio = ``default``; senão um inteiro de 1 a ``PLAYLIST_ORDER_MAX``; ``None`` se inválida."""
    text = raw.strip()
    if not text:
        return default
    if not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if 1 <= value <= limits.PLAYLIST_ORDER_MAX else None


class PlaylistModal(disnake.ui.Modal):
    """Adicionar (``current`` vazio) ou editar uma playlist do servidor. A validação é do store (mesmas regras do painel)."""

    def __init__(self, view, current: Optional[Playlist] = None):
        self.view = view
        self.current = current
        super().__init__(
            title="Adicionar/Editar playlist do servidor",
            custom_id="guild_playlist_edit",
            timeout=180,
            components=[
                disnake.ui.TextInput(
                    label="Nome (um emoji no início vira o ícone):",
                    custom_id="pl_name",
                    min_length=limits.PLAYLIST_NAME_MIN,
                    max_length=limits.PLAYLIST_NAME_MAX + limits.PLAYLIST_ICON_MAX + 1,
                    value=display_name(current) if current else None,
                ),
                disnake.ui.TextInput(
                    label="Descrição (opcional):",
                    custom_id="pl_description",
                    min_length=limits.PLAYLIST_DESCRIPTION_MIN,
                    max_length=limits.PLAYLIST_DESCRIPTION_MAX,
                    value=(current.description or None) if current else None,
                    required=False,
                ),
                disnake.ui.TextInput(
                    label="Link/Url:",
                    custom_id="pl_url",
                    min_length=limits.PLAYLIST_URL_MIN,
                    max_length=limits.PLAYLIST_URL_MAX,
                    value=current.url if current else None,
                ),
                disnake.ui.TextInput(
                    label="Gêneros, separados por vírgula (opcional):",
                    custom_id="pl_genres",
                    max_length=limits.PLAYLIST_GENRES_MAX * (limits.PLAYLIST_GENRE_MAX + 1),
                    value=(", ".join(current.genres) or None) if current else None,
                    required=False,
                ),
                disnake.ui.TextInput(
                    label="Ordem (opcional, 1 = primeira):",
                    custom_id="pl_order",
                    max_length=len(str(limits.PLAYLIST_ORDER_MAX)),
                    value=str(current.order) if current else None,
                    required=False,
                ),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction, /) -> None:
        if not can_manage(inter):
            await inter.send(MSG_DENIED, ephemeral=True)
            return
        values = inter.text_values
        order = parse_order(values["pl_order"], self.current.order if self.current else 0)  # 0 = no fim da lista
        if order is None:
            await inter.send(_TEXTS[ORDER_INVALID], ephemeral=True)
            return
        icon, name = split_leading_icon(values["pl_name"])
        item = Playlist(
            order=order,
            name=name,
            icon=icon,
            description=values["pl_description"].strip(),
            genres=split_genres(values["pl_genres"]),
            url=values["pl_url"].strip(),
        )
        await inter.response.defer(ephemeral=True)
        store = self.view.bot.pool.servers.playlists
        try:
            saved = (
                await store.edit(inter.guild_id, self.current.name, item)
                if self.current
                else await store.add(inter.guild_id, item)
            )
        except GenericError as exc:
            await inter.edit_original_message(exc.text)
            return
        if isinstance(saved, RuleError):
            await inter.edit_original_message(error_text(saved))
            return
        await self.view.finish_write(
            inter, f"Playlist {md_link(display_name(saved), saved.url)} salva na lista do servidor."
        )


class PlaylistImportModal(disnake.ui.Modal):
    """Importar colando JSON: só acrescenta; o relatório mostra o que entrou e o que foi pulado."""

    def __init__(self, view):
        self.view = view
        super().__init__(
            title="Importar playlists para o servidor",
            custom_id="guild_playlist_import",
            timeout=180,
            components=[
                disnake.ui.TextInput(
                    style=disnake.TextInputStyle.long,
                    label="Inserir dados (em formato json)",
                    custom_id="json_data",
                    min_length=20,
                    max_length=limits.PLAYLIST_PASTE_MAX,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction, /) -> None:
        if not can_manage(inter):
            await inter.send(MSG_DENIED, ephemeral=True)
            return
        raw = inter.text_values["json_data"]
        if len(raw) > limits.PLAYLIST_PASTE_MAX:  # o limite do campo também é conferido aqui, no servidor
            await inter.send(MSG_PASTE_BIG, ephemeral=True)
            return
        cog = self.view.bot.get_cog("Music")
        if retry_after := cog.fav_import_export_cd.get_bucket(inter).update_rate_limit():
            await inter.send(f"**Aguarde {int(max(retry_after, 1))}s para importar de novo.**", ephemeral=True)
            return
        parsed = parse_file(raw.encode("utf-8"), max_bytes=limits.PLAYLIST_FILE_MAX_BYTES)
        if isinstance(parsed, RuleError):
            await inter.send(MSG_PASTE_BAD, ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        try:
            report = await self.view.bot.pool.servers.playlists.import_file(inter.guild_id, parsed)
        except GenericError as exc:
            await inter.edit_original_message(exc.text)
            return
        await self.view.finish_write(inter, format_report(report))


async def export_file(view, inter: disnake.MessageInteraction) -> None:
    """Exporta no formato novo, numa resposta efêmera à parte; só quem gerencia o servidor (mesma regra das escritas)."""
    if not can_manage(inter):
        await inter.send(MSG_DENIED, ephemeral=True)
        return
    await inter.response.defer(with_message=True, ephemeral=True)
    try:
        content = await view.bot.pool.servers.playlists.export_bytes(inter.guild_id)
    except GenericError as exc:
        await inter.edit_original_message(exc.text)
        return
    await inter.edit_original_message(
        content="**As playlists do servidor estão no arquivo; importe-o pelo botão de importar.**",
        file=disnake.File(BytesIO(content), filename=f"playlists-{inter.guild_id}.json"),
    )


async def clear_all(view, inter: disnake.MessageInteraction) -> bool:
    """Entrega o backup (mensagem efêmera à parte) ANTES de limpar a lista; ``False`` se nada foi limpo.

    A limpeza só vale para a versão da lista que virou backup: se mudou no meio (outra importação, outro admin), nada é apagado.
    """
    if not can_manage(inter):
        await inter.send(MSG_DENIED, ephemeral=True)
        return False
    await inter.response.defer(with_message=True, ephemeral=True)
    store = view.bot.pool.servers.playlists
    try:
        items, rev = await store.snapshot(inter.guild_id)
    except GenericError as exc:
        await inter.edit_original_message(exc.text)
        return False
    if not items:
        await inter.edit_original_message("**Não há playlists fixadas no servidor.**")
        return False

    backup = json.dumps(to_export(items), ensure_ascii=False, indent=2).encode(
        "utf-8"
    )  # a mesma lista que será apagada
    try:
        await inter.followup.send(
            "**Backup das playlists do servidor.** Para reverter, importe o conteúdo dele.",
            file=disnake.File(BytesIO(backup), filename=f"playlists-{inter.guild_id}.json"),
            ephemeral=True,
        )
    except disnake.HTTPException:
        await inter.edit_original_message("**Não consegui enviar o backup, então não apaguei nada.**")
        return False

    try:
        await store.clear(inter.guild_id, expected_rev=rev)
    except GenericError as exc:
        await inter.edit_original_message(f"{exc.text}\n`O backup já foi enviado.`")
        return False
    await inter.edit_original_message(content="### As playlists do servidor foram excluídas.")
    return True
