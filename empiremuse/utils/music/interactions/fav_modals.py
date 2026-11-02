# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Optional

import disnake

from empiremuse.rules.entries import merge_import, save_entry
from empiremuse.rules.errors import (
    AVATAR_INVALID,
    QUOTA_FULL,
    RuleError,
)
from empiremuse.rules.favorites import check_import_name, name_limit
from empiremuse.rules.integrations import import_entry as integration_entry, save_external
from empiremuse.rules.limits import (
    FAV_NAME_MAX,
    FAV_NAME_MIN,
    FAV_URL_MAX,
    ITG_TITLE_MAX,
    ITG_URL_MAX,
    PLAYLIST_FILE_MAX_BYTES,
    PLAYLIST_PASTE_MAX,
)
from empiremuse.rules.playlists import ParsedFile, check_attachment, check_paste, parse_file
from empiremuse.rules.text import clean_import_name
from empiremuse.rules.urls import check_url, clean_url
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.audio_sources.deezer import deezer_regex
from empiremuse.utils.music.audio_sources.spotify import spotify_regex_w_user
from empiremuse.utils.music.converters import fix_characters, md_link
from empiremuse.utils.music.errors import GenericError, error_code_text, log_unexpected_error
from empiremuse.utils.music.user_limits import quota_for
from empiremuse.utils.others import (
    CustomContext,
)

from empiremuse.utils.music.interactions.common import (
    ViewMode,
    logger,
    soundcloud_regex,
    youtube_regex,
)
from empiremuse.utils.music.interactions.favorites_texts import (
    _NO_ROOM_INTEGRATION_TEXT,
    _echo,
    _import_quota_text,
    _invalid_link_text,
    _invalid_name_text,
    _name_error_text,
    _profile_saved,
    _profile_source,
    _retry_text,
    _save_error_text,
    _spotify_profile,
    _url_error_text,
    _wait_text,
)


FAV_FILE_FIELD = "json_file"
FAV_TEXT_FIELD = "json_data"
_FAV_BOTH_SOURCES = "**Envie o arquivo OU cole o conteúdo, não os dois.**"
_FAV_NO_SOURCE = "**Envie um arquivo .json ou cole o conteúdo exportado.**"
_FAV_DOWNLOAD_FAILED = "**Não consegui baixar o anexo. Tente enviar o arquivo de novo.**"
_FAV_NOTHING_VALID = "**O arquivo não tem nenhum favorito válido** (cada item precisa de nome e link https)."


def playlists_error_text(error: RuleError) -> str:
    """O texto do ``/playlists import`` para um arquivo recusado (mesmos limites e mensagens de ``rules/playlists``)."""
    from empiremuse.cogs.server_playlists import error_message  # tarde: o cog importa o resto do bot

    return error_message(error)


class FavModalImport(disnake.ui.Modal):
    """Importar favoritos do membro (anexo ``.json`` ou texto colado, lidos por ``rules/playlists``) ou integrações (JSON colado).

    Favoritos: o arquivo pode ser o exportado por este menu (``{nome: url}``), pelo painel ou pelo ``/playlists export``
    (formato de playlists; só nome e link contam). Os limites por usuário (cota, nome e link) são os de sempre e nada
    aqui toca as playlists do servidor.
    """

    def __init__(self, view):
        self.view = view

        if self.view.mode == ViewMode.fav_manager:
            super().__init__(
                title="Importar favoritos",
                components=[
                    disnake.ui.Label(
                        "Arquivo .json (opcional)",
                        disnake.ui.FileUpload(custom_id=FAV_FILE_FIELD, required=False, max_values=1),
                        description=f"Exportado por este menu, pelo painel ou pelo bot (até {PLAYLIST_FILE_MAX_BYTES // 1024} KiB).",
                    ),
                    disnake.ui.Label(
                        "Ou cole o conteúdo (JSON)",
                        disnake.ui.TextInput(
                            style=disnake.TextInputStyle.long,
                            custom_id=FAV_TEXT_FIELD,
                            required=False,
                            max_length=PLAYLIST_PASTE_MAX,
                        ),
                    ),
                ],
            )
            return

        if self.view.mode == ViewMode.integrations_manager:
            super().__init__(
                title="Importar integração",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label="Inserir dados (em formato json)",
                        custom_id="json_data",
                        min_length=20,
                        required=True,
                    )
                ],
            )
            return

        raise GenericError(f"Modo atual ainda não implementado: {self.view.mode} | {type(self.view.mode)}")

    async def _reject_rate_limited(self, inter: disnake.ModalInteraction, retry_after: float) -> None:
        await inter.send(_wait_text(retry_after), ephemeral=True)

    async def callback(self, inter: disnake.ModalInteraction, /) -> None:
        if self.view.mode == ViewMode.fav_manager:
            if await self._import_favorites_file(inter):
                await self._redraw()
            return

        try:
            json_data = json.loads(inter.text_values["json_data"])
        except ValueError, RecursionError:
            logger.exception("Importação de favoritos/links/integrações: JSON inválido")
            await inter.send(
                "**Ocorreu um erro ao analisar os dados ou foi enviado dados inválidos/não-formatado "
                "em formato json.**",
                ephemeral=True,
            )
            return

        if not isinstance(json_data, dict) or not json_data:
            await inter.send(
                "**O arquivo precisa conter um objeto json com pelo menos um item.**",
                ephemeral=True,
            )
            return

        if self.view.mode != ViewMode.integrations_manager:
            raise GenericError(f"**Modo ainda não implementado: {self.view.mode} | {type(self.view.mode)}**")

        if retry_after := self._rate_limited(inter):
            await self._reject_rate_limited(inter, retry_after)
            return

        if not await self._import_integrations(inter, json_data):
            return

        await self._redraw()

    def _rate_limited(self, inter: disnake.ModalInteraction) -> float:
        """Segundos até poder importar de novo (``0`` = pode agora); a chamada já conta esta tentativa."""
        return self.view.bot.get_cog("Music").fav_import_export_cd.get_bucket(inter).update_rate_limit() or 0.0

    async def _redraw(self) -> None:
        if not isinstance(self.view.ctx, CustomContext):
            await self.view.ctx.edit_original_message(content=self.view.build_txt(), view=self.view)
        elif self.view.message:
            await self.view.message.edit(content=self.view.build_txt(), view=self.view)

    # ------------------------------------------------------------------ favoritos (anexo ou texto, por rules/playlists)

    async def _read_favorites_file(self, inter: disnake.ModalInteraction) -> ParsedFile | None:
        """O arquivo lido (anexo ou texto colado), já validado pelas regras das playlists; ``None`` = já respondeu o motivo."""
        files = tuple(inter.resolved_values.get(FAV_FILE_FIELD) or ())
        text = inter.text_values.get(FAV_TEXT_FIELD) or ""
        if files and text.strip():
            await inter.send(_FAV_BOTH_SOURCES, ephemeral=True)
            return None
        if not files and not text.strip():
            await inter.send(_FAV_NO_SOURCE, ephemeral=True)
            return None
        if files:
            attachment = files[0]
            refused = check_attachment(attachment.filename, attachment.size)  # antes de baixar
            if refused is not None:
                await inter.send(playlists_error_text(refused), ephemeral=True)
                return None
        else:
            pasted = check_paste(text)
            if isinstance(pasted, RuleError):
                await inter.send(playlists_error_text(pasted), ephemeral=True)
                return None
        if retry_after := self._rate_limited(inter):
            await self._reject_rate_limited(inter, retry_after)
            return None
        await inter.response.defer(ephemeral=True)
        if files:
            try:
                data: str | bytes = await files[0].read()
            except disnake.HTTPException:  # inclui NotFound: o anexo sumiu ou o Discord falhou
                await inter.edit_original_message(_FAV_DOWNLOAD_FAILED)
                return None
        else:
            data = pasted
        parsed = parse_file(data, max_bytes=PLAYLIST_FILE_MAX_BYTES)
        if isinstance(parsed, RuleError):
            await inter.edit_original_message(playlists_error_text(parsed))
            return None
        if not parsed.items:
            await inter.edit_original_message(_FAV_NOTHING_VALID)
            return None
        return parsed

    async def _import_favorites_file(self, inter: disnake.ModalInteraction) -> bool:
        parsed = await self._read_favorites_file(inter)
        if parsed is None:
            return False

        clean_data = {}
        for item in parsed.items:  # só nome e link contam; descrição, gêneros e ícone do arquivo são ignorados
            url = check_url(item.url, self.view.bot.config["USER_FAV_MAX_URL_LENGTH"])
            if isinstance(url, RuleError):
                await inter.edit_original_message(_url_error_text(url))
                return False
            clean_name = check_import_name(item.name, clean_data, self.view.bot.config["USER_FAV_MAX_NAME_LENGTH"])
            if isinstance(clean_name, RuleError):
                await inter.edit_original_message(_name_error_text(clean_name, FAV_NAME_MIN, FAV_NAME_MAX))
                return False
            clean_data[clean_name] = url

        work = deepcopy(await self.view.bot.get_global_data(inter.author.id, db_name=DBModel.users))

        limit = await quota_for(self.view.bot, inter.author, "MAX_USER_FAVS")

        merged = merge_import(work["fav_links"], clean_data, limit)

        if isinstance(merged, RuleError):
            await inter.edit_original_message(_import_quota_text(merged, "favoritos"))
            return False

        work["fav_links"] = merged
        self.view.data = work  # só depois de todas as checagens: uma recusa não altera a tela

        await self.view.bot.update_global_data(inter.author.id, self.view.data, db_name=DBModel.users)

        skipped = (
            f"\n`{len(parsed.invalid)} item(ns) do arquivo ignorado(s) por não ter nome e link válidos.`"
            if parsed.invalid
            else ""
        )
        await inter.edit_original_message(content=f"**Favoritos importados com sucesso!**{skipped}")

        if (s := len(clean_data)) > 1:
            self.view.log = f"{s} favoritos foram importados com sucesso."
        else:
            name = next(iter(clean_data))
            self.view.log = f"O favorito {md_link(name, clean_data[name])} foi importado com sucesso."

        return True

    async def _import_integrations(self, inter: disnake.ModalInteraction, json_data: dict) -> bool:
        clean_data = {}

        for name, value in json_data.items():
            entry = integration_entry(value)

            if isinstance(entry, RuleError):
                text = _invalid_link_text(entry.value) if entry.code == AVATAR_INVALID else _url_error_text(entry)
                await inter.send(text, ephemeral=True)
                return False

            if (clean_name := clean_import_name(name, 1, ITG_TITLE_MAX, clean_data)) is None:
                await inter.send(_invalid_name_text(name, 1, ITG_TITLE_MAX), ephemeral=True)
                return False

            clean_data[clean_name] = entry

        await inter.response.defer(ephemeral=True)

        work = deepcopy(await self.view.bot.get_global_data(inter.author.id, db_name=DBModel.users))

        limit = await quota_for(self.view.bot, inter.author, "MAX_USER_INTEGRATIONS")

        merged = merge_import(work["integration_links"], clean_data, limit)

        if isinstance(merged, RuleError):
            await inter.edit_original_message(_import_quota_text(merged, "integrações"))
            return False

        work["integration_links"] = merged
        self.view.data = work  # só depois de todas as checagens: uma recusa não altera a tela

        await self.view.bot.update_global_data(inter.author.id, self.view.data, db_name=DBModel.users)

        await inter.edit_original_message(content="**Integrações importadas com sucesso!**")

        if (s := len(clean_data)) > 1:
            self.view.log = f"{s} integrações foram importadas com sucesso."
        else:
            name = next(iter(clean_data))
            self.view.log = f"A integração {md_link(name, clean_data[name]['url'])} foi importada com sucesso."

        return True


class FavModalAdd(disnake.ui.Modal):
    def __init__(self, name: Optional[str], url: Optional[str], view):
        self.view = view
        self.name = name

        if self.view.mode == ViewMode.fav_manager:
            super().__init__(
                title="Adicionar/Editar playlist/favorito",
                custom_id="user_fav_edit",
                timeout=180,
                components=[
                    disnake.ui.TextInput(
                        label="Nome da playlist/favorito:",
                        custom_id="user_fav_name",
                        min_length=2,
                        max_length=25,
                        value=name or None,
                    ),
                    disnake.ui.TextInput(
                        label="Link/Url:", custom_id="user_fav_url", min_length=10, max_length=200, value=url or None
                    ),
                ],
            )
            return

        if self.view.mode == ViewMode.integrations_manager:
            super().__init__(
                title="Adicionar integração",
                custom_id="user_integration_add",
                timeout=180,
                components=[
                    disnake.ui.TextInput(
                        label="Link/Url:",
                        custom_id="user_integration_url",
                        min_length=10,
                        max_length=200,
                        value=url or None,
                    ),
                ],
            )
            return

        raise GenericError(f"**Modo ainda não implementado: {self.view.mode} | {type(self.view.mode)}**")

    async def callback(self, inter: disnake.ModalInteraction):
        if self.view.mode == ViewMode.fav_manager:
            url = inter.text_values["user_fav_url"].strip()

            if (valid_url := clean_url(url, FAV_URL_MAX)) is None:
                await inter.send(
                    embed=disnake.Embed(
                        description=f"**Nenhum link válido encontrado:** {_echo(url)}", color=disnake.Color.red()
                    ),
                    ephemeral=True,
                )
                return

            await inter.response.defer(ephemeral=True)

            work = deepcopy(await self.view.bot.get_global_data(inter.author.id, db_name=DBModel.users))

            name_max = name_limit(self.view.bot.config["USER_FAV_MAX_NAME_LENGTH"])
            limit = await quota_for(self.view.bot, inter.author, "MAX_USER_FAVS")

            saved = save_entry(
                work["fav_links"],
                self.name,
                inter.text_values["user_fav_name"],
                valid_url,
                name_min=FAV_NAME_MIN,
                name_max=name_max,
                limit=limit,
            )

            if isinstance(saved, RuleError):
                await inter.edit_original_message(
                    _save_error_text(saved, FAV_NAME_MIN, name_max, "Você já tem um favorito com esse nome.")
                )
                return

            work["fav_links"] = saved
            self.view.data = work  # só depois de todas as checagens: uma recusa não altera a tela

            await self.view.bot.update_global_data(inter.author.id, self.view.data, db_name=DBModel.users)

            try:
                me = (inter.guild or self.view.bot.get_guild(inter.guild_id)).me
            except AttributeError:
                me = None

            await inter.edit_original_message(
                embed=disnake.Embed(
                    description="**Link salvo/atualizado com sucesso nos seus favoritos!\n"
                    "Ele vai aparecer nas seguintes ocasições:** ```\n"
                    "- Ao usar o comando /play (selecionando no preenchimento automático da busca)\n"
                    "- Ao clicar no botão de tocar favorito do player.\n"
                    "- Ao usar o comando play (prefixed) sem nome ou link.```",
                    color=self.view.bot.get_color(me),
                )
            )

        elif self.view.mode == ViewMode.integrations_manager:
            if not await self._add_integration(inter):
                return

        if not isinstance(self.view.ctx, CustomContext):
            await self.view.ctx.edit_original_message(content=self.view.build_txt(), view=self.view)
        elif self.view.message:
            await self.view.message.edit(content=self.view.build_txt(), view=self.view)

    async def _add_integration(self, inter: disnake.ModalInteraction) -> bool:
        """Adiciona/edita uma integração a partir do link de um perfil; ``False`` se nada foi gravado."""
        try:
            await inter.response.defer(ephemeral=True, with_message=True)
        except disnake.HTTPException, disnake.InteractionResponded:
            pass

        work = deepcopy(await self.view.bot.get_global_data(inter.author.id, db_name=DBModel.users))

        typed_url = inter.text_values["user_integration_url"].strip()

        if (url := clean_url(typed_url, ITG_URL_MAX)) is None:
            await inter.edit_original_message(
                embed=disnake.Embed(
                    description=f"**Nenhum link válido encontrado:** {_echo(typed_url)}", color=disnake.Color.red()
                )
            )
            return False

        if (source := _profile_source(url)) is None:
            await inter.edit_original_message(
                embed=disnake.Embed(
                    description=f"**Link informado não é suportado:** {_echo(url)}", color=disnake.Color.red()
                )
            )
            return False

        # antes de qualquer consulta externa: o cooldown por membro e a lista cheia com um perfil que ainda não está salvo
        if retry_after := self.view.bot.get_cog("Music").fav_import_export_cd.get_bucket(inter).update_rate_limit():
            await inter.edit_original_message(_retry_text(retry_after))
            return False

        limit = await quota_for(self.view.bot, inter.author, "MAX_USER_INTEGRATIONS")

        if limit and len(work["integration_links"]) >= limit and not _profile_saved(work["integration_links"], url):
            await inter.edit_original_message(_NO_ROOM_INTEGRATION_TEXT)
            return False

        if (data := await self._lookup_profile(inter, url, source)) is None:
            return False

        saved = save_external(
            work["integration_links"],
            fix_characters(data["title"], 80),
            data.get("url"),
            data.get("avatar"),
            limit=limit,
        )

        if isinstance(saved, RuleError):
            await inter.edit_original_message(
                _NO_ROOM_INTEGRATION_TEXT
                if saved.code == QUOTA_FULL
                else "**Não foi possível usar os dados devolvidos pelo serviço. Tente novamente mais tarde.**"
            )
            return False

        work["integration_links"] = saved.entries
        self.view.data = work  # só depois de todas as checagens: uma recusa não altera a tela

        await self.view.bot.update_global_data(inter.author.id, self.view.data, db_name=DBModel.users)

        try:
            me = (inter.guild or self.view.bot.get_guild(inter.guild_id)).me
        except AttributeError:
            me = None

        await inter.edit_original_message(
            embed=disnake.Embed(
                description=f"**Integração adicionada/editada com sucesso:** {md_link(saved.name, saved.url)}\n"
                "**Ela vai aparecer nas seguintes ocasições:** ```\n"
                "- Ao usar o comando /play (selecionando a integração no preenchimento automático da busca)\n"
                "- Ao clicar no botão de tocar favorito do player.\n"
                "- Ao usar o comando play (prefixed) sem nome ou link.```",
                color=self.view.bot.get_color(me),
            ),
            view=None,
        )

        self.view.log = f"{md_link(saved.name, saved.url)} foi adicionado nas suas integrações."

        return True

    async def _lookup_profile(self, inter: disnake.ModalInteraction, url: str, source: str) -> Optional[dict]:
        """Consulta o perfil no Spotify, Deezer ou yt-dlp (``source``); ``None`` (com a mensagem já enviada) se falhar."""
        if source == "spotify":
            return await self._lookup_spotify(inter, url)

        if source == "deezer":
            return await self._lookup_deezer(inter, url)

        return await self._lookup_ytdl(inter, url, source)

    async def _lookup_spotify(self, inter: disnake.ModalInteraction, url: str) -> Optional[dict]:
        if not self.view.bot.spotify:
            await inter.edit_original_message(
                embed=disnake.Embed(
                    description="**O suporte ao spotify não está disponível no momento...**",
                    color=disnake.Color.red(),
                )
            )
            return None

        url_type, user_id = spotify_regex_w_user.match(url).groups()

        if url_type != "user":
            await inter.edit_original_message(
                embed=disnake.Embed(
                    description=f"**Você deve usar link de um perfil de usuário do spotify.** {_echo(url)}",
                    color=disnake.Color.red(),
                )
            )
            return None

        try:
            result = await self.view.bot.spotify.get_user_info(user_id)
            profile = _spotify_profile(result, user_id, url) if result else None
        except Exception as e:  # inclui resposta que não é um dict: mensagem genérica, o detalhe só vai para o log
            code = log_unexpected_error(logger, e, "integração: consulta do perfil (spotify)")
            await self._profile_error(inter, code)
            return None

        if not profile:
            await inter.edit_original_message(
                embed=disnake.Embed(
                    description="**O usuário do link informado não possui playlists públicas...**",
                    color=self.view.bot.get_color(),
                )
            )
            return None

        return profile

    async def _lookup_deezer(self, inter: disnake.ModalInteraction, url: str) -> Optional[dict]:
        url_type, url_id = deezer_regex.match(url).groups()[-2:]

        if url_type != "profile":
            await inter.edit_original_message(
                embed=disnake.Embed(
                    description=f"**Você deve usar link de um perfil de usuário do deezer.** {_echo(url)}",
                    color=disnake.Color.red(),
                )
            )
            return None

        try:
            result = await self.view.bot.deezer.get_user_info(int(url_id))
            return {"title": f"[DZ]: {result['name'][:90]}", "url": result["link"], "avatar": result["picture"]}
        except Exception as e:  # inclui resposta sem name/link/picture ou que não é um dict (ex.: erro da API)
            code = log_unexpected_error(logger, e, "integração: consulta do perfil (deezer)")
            await self._profile_error(inter, code)
            return None

    async def _lookup_ytdl(self, inter: disnake.ModalInteraction, url: str, source: str) -> Optional[dict]:
        if source == "youtube":
            base_url, label = f"{youtube_regex.search(url).group(0)}/playlists", "[YT]:"
        else:
            base_url, label = f"https://soundcloud.com/{soundcloud_regex.search(url).group(1)}/sets", "[SC]:"

        loop = self.view.bot.loop or asyncio.get_running_loop()

        try:
            info = await loop.run_in_executor(
                None, lambda: self.view.bot.pool.ytdl.extract_info(base_url, download=False)
            )
        except Exception as e:
            code = log_unexpected_error(logger, e, "integração: leitura da URL com o yt-dlp")
            await inter.edit_original_message(
                "**Ocorreu um erro ao obter informação da url. Tente novamente mais tarde.**" + error_code_text(code)
            )
            return None

        if not info:
            msg = f"**O usuário/canal do link informado não existe:**\n{_echo(url)}"

            if label == "[YT]:":
                msg += "\n\n`Nota: Confira se no link contém usuário com @, ex: @ytchannel`"

            await inter.edit_original_message(embed=disnake.Embed(description=msg, color=disnake.Color.red()))
            return None

        if not info["entries"]:
            await inter.edit_original_message(
                embed=disnake.Embed(
                    description="**O usuário/canal do link informado não possui playlists públicas...**",
                    color=disnake.Color.red(),
                )
            )
            return None

        return {"title": f"{label} {info['title']}", "url": info["original_url"]}

    async def _profile_error(self, inter: disnake.ModalInteraction, error_code: str = "") -> None:
        await inter.edit_original_message(
            embed=disnake.Embed(
                description="**Ocorreu um erro ao obter as informações do perfil. Tente novamente mais tarde.**"
                + error_code_text(error_code),
                color=self.view.bot.get_color(),
            )
        )
