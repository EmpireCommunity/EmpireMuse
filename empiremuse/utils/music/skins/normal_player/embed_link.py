# -*- coding: utf-8 -*-
import datetime
import re
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import time_format, get_button_style, md_bracketed, md_link, md_code
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class EmbedLinkSkin:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_embed_link_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = False
        player.controller_mode = True
        player.auto_update = 0
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = False

    def load(self, player: LavalinkPlayer) -> dict:
        data = {"content": None, "embeds": []}

        txt = ""

        if player.current_hint:
            txt += f"> -# {app_emoji(player.bot, 'notification')} **⠂Dica:** `{player.current_hint}`"

        if player.current.is_stream:
            duration_txt = f"\n> -# {app_emoji(player.bot, 'record')} **⠂Duração:** `Livestream`"
        else:
            duration_txt = (
                f"\n> -# {app_emoji(player.bot, 'history')} **⠂Duração:** `{time_format(player.current.duration)}`"
            )

        title = (
            md_code(player.current.title)
            if not player.current.uri
            else md_link(player.current.title, player.current.uri, limit=40)
        )

        if player.paused:
            txt += f"\n> -# {app_emoji(player.bot, 'pause')} **⠂Em Pausa:** {title}{duration_txt}"

        else:
            txt += f"\n> -# {app_emoji(player.bot, 'play')} **⠂Tocando Agora:** {title}{duration_txt}"
            if not player.current.is_stream:
                txt += (
                    f" {md_bracketed(f'<t:{int((disnake.utils.utcnow() + datetime.timedelta(milliseconds=player.current.duration - player.position)).timestamp())}:R>')}"
                    if not player.paused
                    else ""
                )

        if q := len(player.queue):
            txt += f" `[Na fila: {q}]`"

        if not player.current.autoplay:
            txt += f" <@{player.current.requester}>\n"
        else:
            try:
                txt += f" {md_bracketed(md_link('Música Recomendada', player.current.info['extra']['related']['uri']))}"
            except Exception:
                txt += " `[Música Recomendada]`"

        if player.command_log:
            log = re.sub(
                r"\[(.+)]\(.+\)", r"\1", player.command_log.replace("`", "")
            )  # remover links do command_log p/ evitar gerar mais de uma preview.

            txt += f"> -# {player.command_log_emoji} **⠂Última Interação:** {log}\n"

        data["content"] = txt

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
    return EmbedLinkSkin()
