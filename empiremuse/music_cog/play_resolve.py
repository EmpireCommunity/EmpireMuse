# -*- coding: utf-8 -*-
"""Resolução da busca do `/play`: perfis, links e serviço de busca."""

import datetime
import logging
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse, parse_qs

import disnake

import empiremuse.wavelink as wavelink
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.audio_sources.deezer import deezer_regex
from empiremuse.utils.music.audio_sources.spotify import spotify_regex_w_user
from empiremuse.utils.music.converters import YOUTUBE_VIDEO_REG, music_source_image, md_link
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.interactions import (
    SelectInteraction,
    youtube_regex,
    ButtonInteraction,
)
from empiremuse.utils.others import (
    music_source_emoji_url,
    song_request_buttons,
    update_inter,
    music_source_emoji,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    sc_profile_regex,
    is_discord_attachment_url,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from empiremuse.music_cog.play_state import PlayState


class PlayResolveMixin:
    async def _play_resolve_prefix(self, req: "PlayState") -> None:
        """Traduz os prefixos de seleção (pin, recente, favorito e integração) em links."""
        bot = req.bot
        guild_data = req.guild_data
        inter = req.inter
        is_pin = req.is_pin
        query = req.query
        user_data = req.user_data

        loadtype = None
        tracks = []

        source = None

        if query.startswith("> pin: "):
            if is_pin is None:
                is_pin = True
            if not guild_data:
                guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)
            # o nome vem do valor de um select: é re-resolvido no store (casefold), nunca confiado; sumiu = erro amigável
            playlist = await bot.pool.servers.playlists.find(inter.guild_id, query[7:])
            if playlist is None:
                raise GenericError("**A playlist do servidor não foi encontrada (talvez tenha sido removida).**")
            query = playlist.url
            source = False

        elif query.startswith("> lst: "):
            query = user_data["last_tracks"][int(query[7:])]["url"]
            source = False

        if not user_data:
            user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        yt_match = None
        sc_match = None
        profile_avatar = None
        info = {"entries": []}
        node: Optional[wavelink.Node] = None

        if query.startswith("> fav:"):
            query = user_data["fav_links"][query[7:]]

        elif query.startswith("> itg:"):
            integration_data = user_data["integration_links"][query[7:]]

            if not isinstance(integration_data, dict):
                integration_data = {"url": integration_data, "avatar": None}
                user_data["integration_links"][query[7:]] = integration_data
                await self.bot.update_global_data(inter.author.id, user_data, db_name=DBModel.users)

            query = integration_data["url"]

            profile_avatar = integration_data.get("avatar")

        req.guild_data = guild_data
        req.info = info
        req.is_pin = is_pin
        req.loadtype = loadtype
        req.node = node
        req.profile_avatar = profile_avatar
        req.query = query
        req.sc_match = sc_match
        req.source = source
        req.tracks = tracks
        req.user_data = user_data

    async def _play_profile_playlists(self, req: "PlayState") -> None:
        """Busca as playlists públicas de perfis do Spotify, Deezer, YouTube e SoundCloud."""
        bot = req.bot
        info = req.info
        inter = req.inter
        profile_avatar = req.profile_avatar
        query = req.query
        sc_match = req.sc_match
        selected_title = req.selected_title

        if matches := spotify_regex_w_user.match(query):
            if not self.bot.spotify:
                raise GenericError("**O suporte ao spotify não está disponível no momento...**")

            url_type, user_id = matches.groups()

            if url_type == "user":
                try:
                    await inter.response.defer(ephemeral=True)
                except Exception:
                    pass

                cache_key = f"partial:spotify:{url_type}:{user_id}"

                if not (info := self.bot.pool.integration_cache.get(cache_key)):
                    result = await self.bot.spotify.get_user_playlists(user_id)
                    info = {
                        "entries": [
                            {"title": t["name"], "url": f"{t['external_urls']['spotify']}"} for t in result["items"]
                        ]
                    }
                    self.bot.pool.integration_cache[cache_key] = info

        elif matches := deezer_regex.match(query):
            url_type, user_id = matches.groups()[-2:]

            if url_type == "profile":
                try:
                    await inter.response.defer(ephemeral=True)
                except Exception:
                    pass

                cache_key = f"partial:deezer:{url_type}:{user_id}"

                if not (info := self.bot.pool.integration_cache.get(cache_key)):
                    result = await bot.deezer.get_user_playlists(user_id)
                    info = {"entries": [{"title": t["title"], "url": f"{t['link']}"} for t in result]}
                    self.bot.pool.integration_cache[cache_key] = info

        elif matches := (yt_match := youtube_regex.search(query)) or (sc_match := sc_profile_regex.match(query)):
            if yt_match:
                query = f"{yt_match.group()}/playlists"
                remove_chars = 12
            else:
                query = f"{sc_match.group(0).strip('<>')}/sets"
                remove_chars = 6

            try:
                await inter.response.defer(ephemeral=True)
            except Exception:
                pass

            if not (info := self.bot.pool.integration_cache.get(query)):
                info = await self.bot.loop.run_in_executor(
                    None, lambda: self.bot.pool.ytdl.extract_info(query.split("\n")[0], download=False)
                )

                try:
                    if not info["entries"]:
                        logger.debug("Info da busca: %s", info)
                        raise GenericError(f"**Conteúdo indisponível (ou privado):**\n{query}")
                except KeyError:
                    raise GenericError("**Ocorreu um erro ao tentar obter resultados para a opção selecionada...**")

                self.bot.pool.integration_cache[query] = info

            try:
                profile_avatar = [a["url"] for a in info["thumbnails"] if a["id"] == "avatar_uncropped"][0]
            except KeyError, IndexError:
                pass

            try:
                selected_title = info["channel"]
            except KeyError:
                selected_title = info["title"][:-remove_chars]

            info = {
                "entries": [{"title": t["title"], "url": f"{t['url']}"} for t in info["entries"]],
                "thumbnails": info.get("thumbnails"),
            }

        req.info = info
        req.matches = matches
        req.profile_avatar = profile_avatar
        req.query = query
        req.sc_match = sc_match
        req.selected_title = selected_title

    async def _play_profile_select(self, req: "PlayState") -> bool:
        """Pede a escolha de uma das playlists do perfil."""
        embed = req.embed
        emoji = req.emoji
        guild = req.guild
        info = req.info
        inter = req.inter
        matches = req.matches
        msg = req.msg
        profile_avatar = req.profile_avatar
        query = req.query
        selected_title = req.selected_title
        source = req.source

        if matches and info["entries"]:
            if len(info["entries"]) == 1:
                query = info["entries"][0]["url"]

            else:
                emoji, platform = music_source_emoji_url(query, self.bot)

                view = SelectInteraction(
                    user=inter.author,
                    max_itens=15,
                    opts=[
                        disnake.SelectOption(
                            label=e["title"][:90], value=f"entrie_select_{c}", emoji=emoji, description=platform
                        )
                        for c, e in enumerate(info["entries"])
                    ],
                    timeout=120,
                )

                embed_title = (
                    f"do canal: {(info.get('title') or selected_title)}"
                    if platform == "youtube"
                    else f"do usuário: {info.get('title') or selected_title}"
                )

                embeds = []

                for page_index, page in enumerate(disnake.utils.as_chunks(info["entries"], 15)):
                    embed = disnake.Embed(
                        description="\n".join(
                            f"-# ` {(15 * page_index) + n + 1}. `{md_link(i['title'], i['url'])}"
                            for n, i in enumerate(page)
                        )
                        + "\n\n**Selecione uma playlist abaixo:**\n"
                        f"-# Essa solicitação será cancelada automaticamente <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=120)).timestamp())}:R> caso não seja selecionado uma opção abaixo.",
                        color=self.bot.get_color(guild.me),
                    ).set_author(
                        name=f"Tocar playlist pública {embed_title}", icon_url=music_source_image(platform), url=query
                    )

                    if profile_avatar:
                        embed.set_thumbnail(profile_avatar)
                        try:
                            if len(info["thumbnails"]) > 2:
                                embed.set_image(info["thumbnails"][0]["url"])
                        except Exception:
                            pass

                    embeds.append(embed)

                kwargs = {}

                view.embeds = embeds

                try:
                    func = msg.edit
                except AttributeError:
                    try:
                        func = inter.edit_original_message
                    except AttributeError:
                        kwargs["ephemeral"] = True
                        try:
                            func = inter.followup.send
                        except AttributeError:
                            func = inter.send

                msg = await func(embed=embeds[0], view=view, **kwargs)

                await view.wait()

                if not view.inter or view.selected is False:
                    try:
                        func = msg.edit
                    except Exception:
                        func = view.inter.response.edit_message

                    try:
                        embed = view.embeds[view.current_page]
                    except Exception:
                        embed = embeds[0]

                    embed.description = "\n".join(embed.description.split("\n")[:-3])
                    embed.set_footer(
                        text="⚠️ Tempo esgotado!" if not view.selected is False else "⚠️ Cancelado pelo usuário."
                    )

                    try:
                        await func(
                            embed=embed,
                            components=song_request_buttons(self.bot, await self.bot.pool.i18n.for_guild(inter.guild)),
                        )
                    except Exception:
                        logger.exception("Falha ao atualizar a mensagem de pedido de música")
                    return True

                query = info["entries"][int(view.selected[14:])]["url"]

                if not isinstance(inter, disnake.ModalInteraction):
                    inter.token = view.inter.token
                    inter.id = view.inter.id
                    inter.response = view.inter.response
                else:
                    inter = view.inter

            source = False

        req.embed = embed
        req.emoji = emoji
        req.inter = inter
        req.msg = msg
        req.query = query
        req.source = source
        return False

    async def _play_url_query(self, req: "PlayState") -> bool:
        """Trata a busca que contém link (resultados do YouTube e playlist)."""
        bot = req.bot
        embed = req.embed
        ephemeral = req.ephemeral
        guild = req.guild
        inter = req.inter
        manual_selection = req.manual_selection
        msg = req.msg
        urls = req.urls

        query = urls[0]
        source = False

        if not self.bot.config["ENABLE_DISCORD_URLS_PLAYBACK"] and is_discord_attachment_url(query):
            raise GenericError("**O suporte a links do discord está desativado.**")

        if query.startswith("https://www.youtube.com/results"):
            try:
                query = f"ytsearch:{parse_qs(urlparse(query).query)['search_query'][0]}"
            except Exception:
                raise GenericError(f"**Não há suporte para o link informado:** {query}")
            manual_selection = True

        elif "&list=" in query and (link_re := YOUTUBE_VIDEO_REG.search(query)):
            view = ButtonInteraction(
                user=inter.author,
                timeout=45,
                buttons=[
                    disnake.ui.Button(
                        label="Carregar apenas a música", emoji=app_emoji(self.bot, "music_note"), custom_id="music"
                    ),
                    disnake.ui.Button(
                        label="Carregar playlist", emoji=app_emoji(self.bot, "playlist_play"), custom_id="playlist"
                    ),
                ],
            )

            embed = disnake.Embed(
                description="**O link contém vídeo com playlist.**\n"
                f"Selecione uma opção em até <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=45)).timestamp())}:R> para prosseguir.\n"
                f"-# Nota: Caso o link seja de uma playlist privada, apenas o vídeo do link atual será carregado.",
                color=self.bot.get_color(guild.me),
            )

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(
                        text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                    )
            except AttributeError:
                pass

            try:
                func = inter.edit_original_message
                kwargs = {}
            except AttributeError:
                func = inter.send
                kwargs = {"ephemeral": ephemeral}

            msg = await func(embed=embed, view=view, **kwargs)

            await view.wait()

            if not view.inter or view.selected is False:
                try:
                    func = inter.edit_original_message
                except AttributeError:
                    func = msg.edit

                embed.description = "\n".join(embed.description.split("\n")[:-3])
                embed.set_footer(
                    text="⚠️ Tempo esgotado!" if not view.selected is False else "⚠️ Cancelado pelo usuário."
                )

                try:
                    await func(
                        embed=embed,
                        components=song_request_buttons(self.bot, await self.bot.pool.i18n.for_guild(inter.guild)),
                    )
                except Exception:
                    logger.exception("Falha ao atualizar a mensagem de pedido de música")
                return True

            if view.selected == "music":
                query = link_re.group()

            try:
                inter.store_message = msg
            except AttributeError:
                pass

            if not isinstance(inter, disnake.ModalInteraction):
                inter.token = view.inter.token
                inter.id = view.inter.id
                inter.response = view.inter.response
            else:
                inter = view.inter

        req.embed = embed
        req.inter = inter
        req.manual_selection = manual_selection
        req.msg = msg
        req.query = query
        req.source = source
        return False

    async def _play_ask_source(self, req: "PlayState") -> bool:
        """Pergunta qual serviço priorizar na busca por texto."""
        guild = req.guild
        inter = req.inter
        msg = req.msg
        source = req.source

        music_sources = {"deezer", "spotify"}

        for b in self.bot.pool.get_guild_bots(inter.guild_id):
            if not b.get_guild(inter.guild_id):
                continue

            for n in b.music.nodes.values():
                for s in n.info["sourceManagers"]:
                    if s in self.providers_info:
                        music_sources.add(s)

        view = ButtonInteraction(
            user=inter.author,
            timeout=45,
            buttons=[
                disnake.ui.Button(label=ms.title(), custom_id=ms, emoji=music_source_emoji(ms, self.bot))
                for ms in sorted(music_sources)
            ],
        )

        embed = disnake.Embed(
            color=inter.bot.get_color(guild.me),
            description="**Selecione o serviço você deseja priorizar a busca da música?**\n"
            "-# Nota: Caso não retorne a musica desejada no serviço escolhido será usado outro selecionado automaticamente.\n"
            f"-# Nota 2: Caso não escolha alguma opção abaixo <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=45)).timestamp())}:R> será usado o serviço padrão automaticamente.",
        )

        if inter.response.is_done():
            await inter.edit_original_message(embed=embed, view=view)
        else:
            msg = await inter.send(embed=embed, view=view)

        await view.wait()

        if view.selected:
            inter = view.inter
            source = self.providers_info[view.selected]

        elif view.selected is False:
            for c in view.children:
                c.disabled = True
            embed.description = "\n".join(embed.description.split("\n")[:-1])
            embed.set_footer(text="Interação cancelada.")
            func = view.inter.response.edit_message
            await func(view=view, embed=embed)
            return True

        update_inter(inter, view.inter)

        req.embed = embed
        req.inter = inter
        req.msg = msg
        req.source = source
        return False
