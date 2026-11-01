# -*- coding: utf-8 -*-
"""Botões do controlador do player: pedir música, last.fm, favoritar a faixa atual e letras."""

import asyncio
from typing import Union, Optional

import disnake
from disnake.ext import commands

import empiremuse.wavelink as wavelink
from empiremuse.rules.i18n import fit
from empiremuse.rules.errors import RuleError
from empiremuse.rules.favorites import save_track
from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    is_dj,
    can_connect,
    check_player_perm,
    resolve_player,
    update_attr,
)
from empiremuse.utils.music.converters import fix_characters, URL_REG, md_link
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.user_limits import quota_for, track_fav_error_text
from empiremuse.utils.music.interactions import (
    SelectInteraction,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    send_idle_embed,
    PlayerControls,
    music_source_emoji_url,
)


def _pc_modal_fav_options(self, req):
    """Monta o seletor de favoritos do modal de pedido."""
    modal_components = req.modal_components
    user_data = req.user_data

    if user_data["fav_links"]:
        fav_opts = []

        for k, v in list(user_data["fav_links"].items())[:25]:
            emoji, platform = music_source_emoji_url(v, self.bot)
            fav_opts.append(
                disnake.SelectOption(
                    label=fix_characters(k, 35),
                    value=f"> fav: {k}",
                    description=f"⭐ -> {platform}",
                    emoji=emoji,
                )
            )

        modal_components.append(
            disnake.ui.Label(
                text="⭐⠂Favoritos:",
                component=disnake.ui.StringSelect(
                    options=fav_opts, required=False, min_values=0, custom_id="fav_links"
                ),
            ),
        )


async def _pc_modal_integration_options(self, req):
    """Monta o seletor de integrações do modal de pedido."""
    interaction = req.interaction
    modal_components = req.modal_components
    user_data = req.user_data

    if user_data["integration_links"]:
        integration_opts = []

        update = False

        for k, v in list(user_data["integration_links"].items())[:25]:
            if not isinstance(v, dict):
                v = {"url": v, "avatar": None}
                user_data["integration_links"][k] = v
                update = True

            emoji, platform = music_source_emoji_url(v["url"], self.bot)

            integration_opts.append(
                disnake.SelectOption(
                    label=fix_characters(k[6:], 35),
                    value=f"> itg: {k}",
                    description=f"💠 -> {platform}",
                    emoji=emoji,
                )
            )

        modal_components.append(
            disnake.ui.Label(
                text=(await self.bot.pool.i18n.for_guild(req.interaction.guild))("player.integrations"),
                component=disnake.ui.StringSelect(
                    options=integration_opts, required=False, min_values=0, custom_id="integration_links"
                ),
            )
        )

        if update:
            await self.bot.update_global_data(interaction.author.id, user_data, db_name=DBModel.users)


async def _pc_modal_send(self, req):
    """Envia o modal de pedido de música."""
    interaction = req.interaction
    modal_components = req.modal_components
    tr = await self.bot.pool.i18n.for_guild(interaction.guild)  # uma só vez, antes de responder à interação

    modal_components = [
        disnake.ui.Label(
            text=fit(tr("player.name_or_link_from_youtube"), 45),
            component=disnake.ui.TextInput(custom_id="song_input", max_length=150, required=not modal_components),
        ),
        disnake.ui.Label(
            text=fit(tr("player.queue_position_number"), 45),
            component=disnake.ui.TextInput(custom_id="song_position", max_length=3, required=False),
        ),
    ] + modal_components

    await interaction.response.send_modal(
        title=fit(tr("player.request_a_song"), 45),
        custom_id="modal_add_song" + (f"_{interaction.message.id}" if interaction.message.thread else ""),
        components=modal_components,
    )

    req.modal_components = modal_components


async def _pc_lastfm(self, req):
    """Liga ou desliga o scrobble do last.fm do usuário."""
    cmd = req.cmd
    interaction = req.interaction

    await interaction.response.defer(ephemeral=True, with_message=True)
    user_data = await self.bot.get_global_data(interaction.author.id, db_name=DBModel.users)

    if not user_data["lastfm"]["sessionkey"]:
        try:
            cmd = (
                f"</lastfm:"
                + str(
                    self.bot.get_global_command_named("lastfm", cmd_type=disnake.ApplicationCommandType.chat_input).id
                )
                + ">"
            )
        except AttributeError:
            cmd = "/lastfm"

        await interaction.edit_original_message(
            content=(await self.bot.pool.i18n.for_guild(req.interaction.guild))(
                "player.you_do_not_have_a", cmd=str(cmd)
            )
        )
        return True

    user_data["lastfm"]["scrobble"] = not user_data["lastfm"]["scrobble"]
    self.bot.pool.lastfm_sessions[interaction.author.id] = user_data["lastfm"]
    await self.bot.update_global_data(interaction.author.id, user_data, db_name=DBModel.users)
    lastfm = user_data["lastfm"]
    account = md_link(lastfm["username"], f"https://www.last.fm/user/{lastfm['username']}", code=False)
    tr = await self.bot.pool.i18n.for_guild(interaction.guild)
    await interaction.edit_original_message(
        embed=disnake.Embed(
            description=(
                tr("player.scrobble_enabled", account=account)
                if lastfm["scrobble"]
                else tr("player.scrobble_disabled", account=account)
            ),
            color=self.bot.get_color(),
        )
    )

    req.cmd = cmd
    req.user_data = user_data
    return False


async def _pc_route_panel_click(self, req) -> bool:
    """Clique no painel do canal único: age no player da call de quem apertou (``resolve_player``, S-V8), nunca no do bot que recebeu.

    O painel pertence ao bot do painel, então o clique chega a ele; se o player da call de quem apertou é de outro bot, o clique é
    entregue ao ``Music`` desse bot. Sem call, ou numa call sem player, nada muda e a pessoa é avisada. ``True`` = tratado aqui.
    """
    interaction = req.interaction
    pool = self.bot.pool
    try:
        request = pool.servers.request
        config = await request.get(interaction.guild_id)
        if not config.message_id or interaction.message.id != config.message_id:
            return False
        if not await request.is_panel_bot(interaction.guild_id, self.bot):
            return False
    except GenericError:
        return False

    if req.control == PlayerControls.help_button:
        await _pc_help_button(self, req)  # só texto: não precisa de call nem de player
        return True

    resolved = resolve_player(pool, interaction.guild_id, interaction.author)
    if resolved is None:
        await interaction.send(
            (await self.bot.pool.i18n.for_guild(interaction.guild))("player.panel.join_a_call"), ephemeral=True
        )
        return True
    bot, _ = resolved
    if bot is self.bot:
        interaction.panel_click = (
            True  # o player da call é o deste bot, mesmo que não seja o estático da mensagem do painel
        )
        return False
    update_attr(interaction, bot, bot.get_guild(interaction.guild_id))
    interaction.panel_click = (
        True  # a mensagem é a do painel, não a do player: o outro bot não descarta o clique por isso
    )
    await bot.get_cog("Music").player_controller(interaction, req.control)
    return True


async def _pc_player_checks(self, req):
    """Localiza o player ativo e confere cooldown e canal de voz."""
    control = req.control
    interaction = req.interaction

    if await _pc_route_panel_click(self, req):
        return True

    try:
        player: LavalinkPlayer = self.bot.music.players[interaction.guild_id]
    except KeyError:
        tr = await self.bot.pool.i18n.ack_for_guild(interaction)  # confirma antes de ler o idioma (3 s)
        await interaction.send(tr("player.there_is_no_active_player"), ephemeral=True)
        await send_idle_embed(interaction.message, bot=self.bot)
        return True

    if interaction.message != player.message and not getattr(interaction, "panel_click", False):
        if control != PlayerControls.queue:
            return True

    if player.interaction_cooldown:
        raise GenericError(player.tr("player.the_player_is_on_cooldown"))

    try:
        vc = player.guild.me.voice.channel
    except AttributeError:
        await player.destroy(force=True)
        return True

    req.player = player
    req.vc = vc
    return False


async def _pc_help_button(self, req):
    """Mostra a ajuda dos botões do player."""
    interaction = req.interaction

    embed = disnake.Embed(
        description=(await self.bot.pool.i18n.for_guild(req.interaction.guild))(
            "player.information_about_the_buttons_pause"
        ),
        color=self.bot.get_color(interaction.guild.me),
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

    req.embed = embed


async def _pc_favorite_choices(self, req):
    """Monta os itens da faixa atual que podem ser favoritados."""
    interaction = req.interaction
    player = req.player

    if not player.current:
        await interaction.send(player.tr("player.no_song_is_playing_right"), ephemeral=True)
        return True

    choices = {}
    msg = ""

    if player.current.uri:
        choices["Track"] = {"name": player.current.title, "url": player.current.uri, "emoji": "🎵"}
        msg += player.tr("player.song", md_link=str(md_link(player.current.title, player.current.uri)))

    if player.current.album_url:
        choices["Album"] = {
            "name": player.current.album_name,
            "url": player.current.album_url,
            "emoji": "💽",
        }
        msg += f"**Album:** {md_link(player.current.album_name, player.current.album_url)}\n"

    if player.current.playlist_url:
        choices["Playlist"] = {
            "name": player.current.playlist_name,
            "url": player.current.playlist_url,
            "emoji": "📑",
        }
        msg += f"**Playlist:** {md_link(player.current.playlist_name, player.current.playlist_url)}\n"

    if not choices:
        try:
            await self.player_interaction_concurrency.release(interaction)
        except Exception:
            pass
        await interaction.send(
            embed=disnake.Embed(
                color=self.bot.get_color(interaction.guild.me),
                description=fit(player.tr("player.there_are_no_items_to.description"), 4096),
            ),
            ephemeral=True,
        )
        return True

    req.choices = choices
    req.msg = msg
    return False


async def _pc_favorite_select(self, req):
    """Pede ao usuário que escolha o item a favoritar."""
    choices = req.choices
    interaction = req.interaction
    msg = req.msg

    if len(choices) == 1:
        select_type, info = list(choices.items())[0]

    else:
        view = SelectInteraction(
            user=interaction.author,
            timeout=30,
            opts=[
                disnake.SelectOption(label=k, description=v["name"][:50], emoji=v["emoji"]) for k, v in choices.items()
            ],
        )

        await interaction.send(
            embed=disnake.Embed(
                color=self.bot.get_color(interaction.guild.me),
                description=(await self.bot.pool.i18n.for_guild(req.interaction.guild))(
                    "player.select_an_item_from_the", msg=str(msg)
                ),
            ),
            view=view,
            ephemeral=True,
        )

        await view.wait()

        select_interaction = view.inter

        if not select_interaction or view.selected is False:
            try:
                await self.player_interaction_concurrency.release(interaction)
            except Exception:
                pass
            await interaction.edit_original_message(
                embed=disnake.Embed(
                    color=self.bot.get_color(interaction.guild.me),
                    description=(await self.bot.pool.i18n.for_guild(req.interaction.guild))(
                        "player.operation_cancelled.description"
                    ),
                ),
                view=None,
            )
            return True

        interaction = select_interaction

        select_type = view.selected
        info = choices[select_type]

    req.info = info
    req.interaction = interaction
    req.select_type = select_type
    return False


async def _pc_favorite_save(self, req):
    """Salva o item escolhido nos favoritos e responde."""
    info = req.info
    interaction = req.interaction
    select_type = req.select_type

    await interaction.response.defer()

    user_data = await self.bot.get_global_data(interaction.author.id, db_name=DBModel.users)

    limit = await quota_for(self.bot, interaction.author, "MAX_USER_FAVS")

    saved = save_track(user_data["fav_links"], info["name"], info["url"], limit=limit)

    if isinstance(saved, RuleError):
        await interaction.edit_original_message(
            embed=disnake.Embed(
                color=self.bot.get_color(interaction.guild.me), description=track_fav_error_text(saved)
            ),
            view=None,
        )
        return True

    user_data["fav_links"] = saved.entries

    await self.bot.update_global_data(interaction.author.id, user_data, db_name=DBModel.users)

    self.bot.dispatch("fav_add", interaction.user, user_data, md_link(info["name"], info["url"]))

    global_data = await self.bot.get_global_data(interaction.author.id, db_name=DBModel.guilds)

    try:
        slashcmd = (
            f"</play:"
            + str(self.bot.get_global_command_named("play", cmd_type=disnake.ApplicationCommandType.chat_input).id)
            + ">"
        )
    except AttributeError:
        slashcmd = "/play"

    await interaction.edit_original_response(
        embed=disnake.Embed(
            color=self.bot.get_color(interaction.guild.me),
            description=(await self.bot.pool.i18n.for_guild(req.interaction.guild))(
                "player.item_added_edited_successfully_in",
                select_type=str(select_type),
                md_link=str(md_link(info["name"], info["url"])),
                slashcmd=str(slashcmd),
                v=str(global_data["prefix"] or self.bot.default_prefix),
                name=str(self.play_legacy.name),
            ),
        ),
        view=None,
    )

    try:
        await self.player_interaction_concurrency.release(interaction)
    except Exception:
        pass

    req.user_data = user_data
    return False


async def _pc_lyrics(self, req):
    """Busca e mostra a letra da música atual."""
    interaction = req.interaction
    player = req.player

    if not player.current:
        try:
            await self.player_interaction_concurrency.release(interaction)
        except Exception:
            pass
        await interaction.send(player.tr("player.i_m_not_playing_anything"), ephemeral=True)
        return True

    if not player.current.ytid:
        try:
            await self.player_interaction_concurrency.release(interaction)
        except Exception:
            pass
        await interaction.send(player.tr("player.only_youtube_songs_are_supported"), ephemeral=True)
        return True

    not_found_msg = player.tr("player.there_are_no_lyrics_available")

    await interaction.response.defer(ephemeral=True, with_message=True)

    if player.current.info["extra"].get("lyrics") is None:
        lyrics_data = await player.node.fetch_ytm_lyrics(player.current.ytid)
        player.current.info["extra"]["lyrics"] = {} if lyrics_data.get("track") is None else lyrics_data

    elif not player.current.info["extra"]["lyrics"]:
        try:
            await self.player_interaction_concurrency.release(interaction)
        except Exception:
            pass
        await interaction.edit_original_message(f"**{not_found_msg}**")
        return True

    if not player.current.info["extra"]["lyrics"]:
        try:
            await self.player_interaction_concurrency.release(interaction)
        except Exception:
            pass
        await interaction.edit_original_message(f"**{not_found_msg}**")
        return True

    player.current.info["extra"]["lyrics"]["track"]["albumArt"] = player.current.info["extra"]["lyrics"]["track"][
        "albumArt"
    ][:-1]

    try:
        lyrics_string = "\n".join([d["line"] for d in player.current.info["extra"]["lyrics"]["lines"]])
    except KeyError:
        lyrics_string = player.current.info["extra"]["lyrics"]["text"]

    try:
        await self.player_interaction_concurrency.release(interaction)
    except Exception:
        pass

    await interaction.edit_original_message(
        embed=disnake.Embed(
            description=fit(
                player.tr(
                    "player.song_lyrics.body",
                    md_link=str(md_link(player.current.title, player.current.uri, code=False)),
                    lyrics_string=str(lyrics_string),
                ),
                4096,
            ),
            color=self.bot.get_color(player.guild.me),
        )
    )
    return False


def _pc_command_args(self, req):
    """Traduz o botão no comando e nos argumentos que o executam."""
    cmd = req.cmd
    cmd_kwargs = req.cmd_kwargs
    control = req.control
    player = req.player

    if control == PlayerControls.volume:
        cmd_kwargs = {"value": None}

    elif control == PlayerControls.queue:
        cmd = self.bot.get_slash_command("queue").children.get("display")

    elif control == PlayerControls.shuffle:
        cmd = self.bot.get_slash_command("queue").children.get("shuffle")

    elif control == PlayerControls.seek_to_start:
        cmd = self.bot.get_slash_command("seek")
        cmd_kwargs = {"position": "0"}

    elif control == PlayerControls.pause_resume:
        control = PlayerControls.pause if not player.paused else PlayerControls.resume

    elif control == PlayerControls.loop_mode:
        if player.loop == "current":
            cmd_kwargs["mode"] = "queue"
        elif player.loop == "queue":
            cmd_kwargs["mode"] = "off"
        else:
            cmd_kwargs["mode"] = "current"

    elif control == PlayerControls.skip:
        cmd_kwargs = {"query": None, "play_only": "no"}

    if not cmd:
        cmd = self.bot.get_slash_command(control[12:])

    req.cmd = cmd
    req.cmd_kwargs = cmd_kwargs
    req.control = control
