# -*- coding: utf-8 -*-
import itertools
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import (
    time_format,
    fix_characters,
    get_button_style,
    music_source_image,
    md_bracketed,
    md_link,
)
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class MiniSkin:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_mini_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = True
        player.controller_mode = True
        player.auto_update = 0
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = False

    def load(self, player: LavalinkPlayer) -> dict:
        data = {
            "content": None,
            "embeds": [],
        }

        embed_color = player.bot.get_color(player.guild.me)

        embed = disnake.Embed(
            color=embed_color,
            description=f"-# {md_link(player.current.single_title, player.current.uri or player.current.search_uri)}",
        )
        embed_queue = None
        queue_size = len(player.queue)

        if not player.paused:
            embed.set_author(
                name="Tocando Agora:",
                icon_url=music_source_image(player.current.info["sourceName"]),
            )

        else:
            embed.set_author(name="Em Pausa:", icon_url=asset_url("player_pause_icon.png"))

        if player.current.track_loops:
            embed.description += f" `[🔂 {player.current.track_loops}]`"

        elif player.loop:
            if player.loop == "current":
                embed.description += " `[🔂 música atual]`"
            else:
                embed.description += " `[🔁 fila]`"

        if not player.current.autoplay:
            embed.description += f" {md_bracketed(f'<@{player.current.requester}>')}"
        else:
            try:
                embed.description += (
                    f" {md_bracketed(md_link('Recomendada', player.current.info['extra']['related']['uri']))}"
                )
            except Exception:
                embed.description += "` [Recomendada]`"

        duration = "🔴 Livestream" if player.current.is_stream else time_format(player.current.duration)

        embed.add_field(
            name=f"{app_emoji(player.bot, 'history')} **⠂Duração:**", value=f"```ansi\n[34;1m{duration}[0m\n```"
        )
        embed.add_field(
            name=f"{app_emoji(player.bot, 'microphone')} **⠂Uploader/Artista:**",
            value=f"```ansi\n[34;1m{fix_characters(player.current.author, 18)}[0m\n```",
        )

        if player.command_log:
            embed.add_field(
                name=f"{player.command_log_emoji} **⠂Última Interação:**", value=f"{player.command_log}", inline=False
            )

        if queue_size:
            embed.description += f" `({queue_size})`"

            if player.mini_queue_enabled:
                embed_queue = disnake.Embed(
                    color=embed_color,
                    description="\n".join(
                        f"`{(n + 1):02}) [{time_format(t.duration) if not t.is_stream else '🔴 Livestream'}]` {md_link(t.title, t.uri, limit=38)}"
                        for n, t in (enumerate(itertools.islice(player.queue, 5)))
                    ),
                )
                embed_queue.set_image(url=asset_url("ui_rainbow_bar_2.gif"))

        embed.set_thumbnail(url=player.current.thumb)
        embed.set_image(url=asset_url("ui_rainbow_bar_2.gif"))

        if player.current_hint:
            embed.set_footer(text=f"💡 Dica: {player.current_hint}")

        data["embeds"] = [embed_queue, embed] if embed_queue else [embed]

        data["components"] = [
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "playpause"),
                custom_id=PlayerControls.pause_resume,
                style=get_button_style(player.paused),
            ),
            disnake.ui.Button(emoji=app_emoji(player.bot, "backward"), custom_id=PlayerControls.back),
            disnake.ui.Button(emoji=app_emoji(player.bot, "stop"), custom_id=PlayerControls.stop),
            disnake.ui.Button(emoji=app_emoji(player.bot, "skip"), custom_id=PlayerControls.skip),
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "music_queue"),
                custom_id=PlayerControls.queue,
                disabled=not (player.queue or player.queue_autoplay),
            ),
            disnake.ui.Select(
                placeholder="Mais opções:",
                custom_id="musicplayer_dropdown_inter",
                min_values=0,
                max_values=1,
                required=False,
                options=[
                    disnake.SelectOption(
                        label="Adicionar música",
                        emoji=app_emoji(player.bot, "add_music"),
                        value=PlayerControls.add_song,
                        description="Adicionar uma música/playlist na fila.",
                    ),
                    disnake.SelectOption(
                        label="Adicionar nos seus favoritos",
                        emoji=app_emoji(player.bot, "loved"),
                        value=PlayerControls.add_favorite,
                        description="Adicionar a música atual nos seus favoritos.",
                    ),
                    disnake.SelectOption(
                        label="Tocar do inicio",
                        emoji=app_emoji(player.bot, "fast_backward"),
                        value=PlayerControls.seek_to_start,
                        description="Voltar o tempo da música atual para o inicio.",
                    ),
                    disnake.SelectOption(
                        label=f"Volume: {player.volume}%",
                        emoji=app_emoji(player.bot, "volume"),
                        value=PlayerControls.volume,
                        description="Ajustar volume.",
                    ),
                    disnake.SelectOption(
                        label="Misturar",
                        emoji=app_emoji(player.bot, "shuffle"),
                        value=PlayerControls.shuffle,
                        description="Misturar as músicas da fila.",
                    ),
                    disnake.SelectOption(
                        label="Readicionar",
                        emoji=app_emoji(player.bot, "playlist_add"),
                        value=PlayerControls.readd,
                        description="Readicionar as músicas tocadas de volta na fila.",
                    ),
                    disnake.SelectOption(
                        label="Repetição",
                        emoji=app_emoji(player.bot, "repeat_all"),
                        value=PlayerControls.loop_mode,
                        description="Ativar/Desativar repetição da música/fila.",
                    ),
                    disnake.SelectOption(
                        label=("Desativar" if player.nightcore else "Ativar") + " o efeito nightcore",
                        emoji=app_emoji(player.bot, "sound_spectrum"),
                        value=PlayerControls.nightcore,
                        description="Efeito que aumenta velocidade e tom da música.",
                    ),
                    disnake.SelectOption(
                        label=("Desativar" if player.autoplay else "Ativar") + " a reprodução automática",
                        emoji=app_emoji(player.bot, "refresh"),
                        value=PlayerControls.autoplay,
                        description="Sistema de adição de música automática quando a fila estiver vazia.",
                    ),
                    disnake.SelectOption(
                        label="Last.fm scrobble",
                        emoji=app_emoji(player.bot, "lastfm"),
                        value=PlayerControls.lastfm_scrobble,
                        description="Ativar/desativar o scrobble/registro de músicas na sua conta do last.fm.",
                    ),
                    disnake.SelectOption(
                        label=("Desativar" if player.restrict_mode else "Ativar") + " o modo restrito",
                        emoji=app_emoji(player.bot, "lock"),
                        value=PlayerControls.restrict_mode,
                        description="Apenas DJ's/Staff's podem usar comandos restritos.",
                    ),
                ],
            ),
        ]

        if player.current.ytid and player.node.lyric_support:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Visualizar letras",
                    emoji=app_emoji(player.bot, "lyrics"),
                    value=PlayerControls.lyrics,
                    description="Obter letra da música atual.",
                )
            )

        if player.mini_queue_feature:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Mini-fila do player",
                    emoji=app_emoji(player.bot, "music_queue"),
                    value=PlayerControls.miniqueue,
                    description="Ativar/Desativar a mini-fila do player.",
                )
            )

        if isinstance(player.last_channel, disnake.VoiceChannel):
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Status automático",
                    emoji=app_emoji(player.bot, "speaker"),
                    value=PlayerControls.set_voice_status,
                    description="Configurar o status automático do canal de voz.",
                )
            )

        if not player.has_thread:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Song-Request Thread",
                    emoji=app_emoji(player.bot, "comment"),
                    value=PlayerControls.song_request_thread,
                    description="Criar uma thread/conversa temporária para pedir músicas usando apenas o nome/link.",
                )
            )

        return data


def load():
    return MiniSkin()
