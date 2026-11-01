# -*- coding: utf-8 -*-
import itertools
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import (
    fix_characters,
    time_format,
    get_button_style,
    music_source_image,
    md_link,
    md_code,
)
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class ClassicStaticSkin:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3] + "_static"
        self.preview = asset_url("skin_static_classic_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = False
        player.controller_mode = True
        player.auto_update = 0
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = True

    def load(self, player: LavalinkPlayer) -> dict:
        data = {"content": None, "embeds": []}

        embed = disnake.Embed(color=player.bot.get_color(player.guild.me), description="")

        queue_txt = ""

        embed.description = (
            f"**{md_link(player.current.title, player.current.uri or player.current.search_uri, code=False)}**\n\n"
        )
        embed.set_image(url=player.current.thumb)
        embed_top = None

        if not player.paused:
            (embed_top or embed).set_author(
                name="Tocando Agora:", icon_url=music_source_image(player.current.info["sourceName"])
            )
        else:
            (embed_top or embed).set_author(name="Em Pausa:", icon_url=asset_url("player_pause_icon.png"))

        if player.current.is_stream:
            duration = f"{app_emoji(player.bot, 'record')} **⠂Livestream**"
        else:
            duration = f"{app_emoji(player.bot, 'history')} **⠂Duração:** `{time_format(player.current.duration)}`"

        txt = f"{duration}\n{app_emoji(player.bot, 'microphone')} **⠂Uploader:** {md_code(player.current.author)}\n"

        if not player.current.autoplay:
            f"{app_emoji(player.bot, 'user')} **⠂Pedido por:** <@{player.current.requester}>\n"
        else:
            try:
                mode = f" {md_link('Recomendação', player.current.info['extra']['related']['uri'])}"
            except Exception:
                mode = "`Recomendação`"
            txt += f"{app_emoji(player.bot, 'like')} **⠂Adicionado via:** {mode}\n"

        if player.current.playlist_name:
            txt += f"{app_emoji(player.bot, 'playlists')} **⠂Playlist:** {md_link(player.current.playlist_name, player.current.playlist_url, limit=20)}\n"

        if qsize := len(player.queue):
            data["content"] = "**Músicas na fila:**\n```ansi\n" + "\n".join(
                f"[0;33m{(n + 1):02}[0m [0;34m[{time_format(t.duration) if not t.is_stream else '🔴 stream'}][0m [0;36m{fix_characters(t.title, 45)}[0m"
                for n, t in enumerate(itertools.islice(player.queue, 15))
            )

            if qsize > 15:
                data["content"] += f"\n\n[0;37mE mais[0m [0;35m{qsize}[0m [0;37mmúsicas{'s'[: qsize ^ 1]}.[0m"

            data["content"] += "```"

        elif len(player.queue_autoplay):
            data["content"] = (
                "**Próximas músicas recomendadas:**\n```ansi\n"
                + "\n".join(
                    f"[0;33m{(n + 1):02}[0m [0;34m[{time_format(t.duration) if not t.is_stream else '🔴 stream'}][0m [0;36m{fix_characters(t.title, 45)}[0m"
                    for n, t in enumerate(itertools.islice(player.queue_autoplay, 15))
                )
                + "```"
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

        if queue := player.queue or player.queue_autoplay:
            data["components"].append(
                disnake.ui.Select(
                    placeholder="Próximas músicas:",
                    custom_id="musicplayer_queue_dropdown",
                    min_values=0,
                    max_values=1,
                    required=False,
                    options=[
                        disnake.SelectOption(
                            label=fix_characters(f"{n + 1}. {t.single_title}", 47),
                            description=fix_characters(
                                f"[{time_format(t.duration) if not t.is_stream else '🔴 Live'}]. {t.authors_string}", 47
                            ),
                            value=f"{n:02d}.{t.title[:96]}",
                        )
                        for n, t in enumerate(itertools.islice(queue, 25))
                    ],
                )
            )

        if player.current.ytid and player.node.lyric_support:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Visualizar letras",
                    emoji=app_emoji(player.bot, "lyrics"),
                    value=PlayerControls.lyrics,
                    description="Obter letra da música atual.",
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

        return data


def load():
    return ClassicStaticSkin()
