# -*- coding: utf-8 -*-
import itertools
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import time_format, get_button_style, music_source_image, md_link, md_code
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class ClassicSkin:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_classic_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = True
        player.controller_mode = True
        player.auto_update = 0
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = False

    def load(self, player: LavalinkPlayer) -> dict:
        data = {"content": None, "embeds": []}

        color = player.bot.get_color(player.guild.me)

        embed = disnake.Embed(color=color, description="")

        queue_txt = ""

        bar = asset_url("ui_rainbow_bar_3.gif")

        embed_top = disnake.Embed(
            color=color,
            description=f"### {md_link(player.current.title, player.current.uri or player.current.search_uri, code=False)}",
        )
        embed.set_image(url=bar)

        embed_top.set_image(url=bar)

        embed_top.set_thumbnail(url=player.current.thumb)

        if not player.paused:
            (embed_top or embed).set_author(
                name="Tocando Agora:", icon_url=music_source_image(player.current.info["sourceName"])
            )
        else:
            (embed_top or embed).set_author(name="Em Pausa:", icon_url=asset_url("player_pause_icon.png"))

        if player.current.is_stream:
            duration = f"{app_emoji(player.bot, 'record')} **⠂ `Livestream`"
        else:
            duration = f"{app_emoji(player.bot, 'history')} **⠂** `{time_format(player.current.duration)}`"

        txt = f"{duration}\n{app_emoji(player.bot, 'microphone')} **⠂** {md_code(player.current.author)}\n"

        if not player.current.autoplay:
            txt += f"{app_emoji(player.bot, 'user')} **⠂** <@{player.current.requester}>\n"
        else:
            try:
                mode = f" {md_link('Recomendada', player.current.info['extra']['related']['uri'])}"
            except Exception:
                mode = "`Recomendada`"
            txt += f"> {app_emoji(player.bot, 'like')} **⠂** {mode}\n"

        if player.current.playlist_name:
            txt += f"{app_emoji(player.bot, 'playlists')} **⠂** {md_link(player.current.playlist_name, player.current.playlist_url, limit=19)}\n"

        if qsize := len(player.queue):
            if not player.mini_queue_enabled:
                txt += f"{app_emoji(player.bot, 'music_notes')} **⠂** `{qsize} música{'s'[: qsize ^ 1]} na fila`\n"
            else:
                queue_txt += "```ansi\n[0;33mPróximas Músicas:[0m```" + "\n".join(
                    f"`{(n + 1):02}) [{time_format(t.duration) if t.duration else '🔴 Livestream'}]` "
                    f"{md_link(t.title, t.uri, limit=29)}"
                    for n, t in enumerate(itertools.islice(player.queue, 3))
                )

                if qsize > 3:
                    queue_txt += f"\n`╚══════ E mais {(t := qsize - 3)} música{'s'[: t ^ 1]} ══════╝`"

        elif len(player.queue_autoplay):
            queue_txt += "```ansi\n[0;33mPróximas Músicas:[0m```" + "\n".join(
                f"{app_emoji(player.bot, 'like')} `{(n + 1):02}) [{time_format(t.duration) if t.duration else '🔴 Livestream'}]` "
                f"{md_link(t.title, t.uri, limit=29)}"
                for n, t in enumerate(itertools.islice(player.queue_autoplay, 3))
            )

        if player.command_log:
            txt += f"{player.command_log_emoji} **⠂Última Interação:** {player.command_log}\n"

        embed.description += txt + queue_txt

        if player.current_hint:
            embed.set_footer(text=f"💡 Dica: {player.current_hint}")
        else:
            embed.set_footer(text=str(player), icon_url=asset_url("player_loading_neon_circle.gif"))

        data["embeds"] = [embed_top, embed] if embed_top else [embed]

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
    return ClassicSkin()
