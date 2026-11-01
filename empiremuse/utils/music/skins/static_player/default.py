# -*- coding: utf-8 -*-
import datetime
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
    md_bracketed,
)
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class DefaultStaticSkin:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3] + "_static"
        self.preview = asset_url("skin_static_default_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = False
        player.controller_mode = True
        player.auto_update = 0
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = True

    def load(self, player: LavalinkPlayer) -> dict:
        data = {"content": None, "embeds": []}

        embed = disnake.Embed(color=player.bot.get_color(player.guild.me))
        embed_queue = None
        vc_txt = ""

        if not player.paused:
            embed.set_author(name="Tocando Agora:", icon_url=music_source_image(player.current.info["sourceName"]))

        else:
            embed.set_author(name="Em Pausa:", icon_url=asset_url("player_pause_icon.png"))

        if player.current_hint:
            embed.set_footer(text=f"💡 Dica: {player.current_hint}")
        else:
            embed.set_footer(text=str(player), icon_url=asset_url("player_loading_neon_circle.gif"))

        queue_img = ""

        current_time = disnake.utils.utcnow() - datetime.timedelta(milliseconds=player.position)

        duration = (
            f"> -# {app_emoji(player.bot, 'record')} **⠂Transmitindo:** <t:{int(current_time.timestamp())}:R>\n"
            if player.current.is_stream
            else (
                f"> -# {app_emoji(player.bot, 'history')} **⠂Duração:** {md_bracketed(f'<t:{int(current_time.timestamp())}:R>', prefix=f'{time_format(player.current.duration)} ')}\n"
                if not player.paused
                else ""
            )
        )

        txt = (
            f"-# {md_link(player.current.single_title, player.current.uri or player.current.search_uri)}\n\n"
            f"{duration}"
            f"> -# {app_emoji(player.bot, 'microphone')} **⠂Por:** {player.current.authors_md}"
        )

        if not player.current.autoplay:
            txt += f"\n> -# {app_emoji(player.bot, 'user')} **⠂Pedido por:** <@{player.current.requester}>"
        else:
            try:
                mode = f" {md_link('Recomendação', player.current.info['extra']['related']['uri'])}"
            except Exception:
                mode = "`Recomendação`"
            txt += f"\n> -# {app_emoji(player.bot, 'like')} **⠂Adicionado via:** {mode}"

        try:
            vc_txt = (
                f"\n> -# {app_emoji(player.bot, 'speaker')} **⠂Canal de voz:** {player.guild.me.voice.channel.mention}"
            )
        except AttributeError:
            pass

        if player.current.track_loops:
            txt += f"\n> -# {app_emoji(player.bot, 'repeat')} **⠂Repetições restante:** `{player.current.track_loops}`"

        if player.loop:
            if player.loop == "current":
                e = app_emoji(player.bot, "repeat")
                m = "Música atual"
            else:
                e = app_emoji(player.bot, "repeat_all")
                m = "Fila"
            txt += f"\n> -# {e} **⠂Modo de repetição:** `{m}`"

        if player.current.album_name:
            txt += f"\n> -# {app_emoji(player.bot, 'disc')} **⠂Álbum:** {md_link(player.current.album_name, player.current.album_url, limit=20)}"

        if player.current.playlist_name:
            txt += f"\n> -# {app_emoji(player.bot, 'playlists')} **⠂Playlist:** {md_link(player.current.playlist_name, player.current.playlist_url, limit=20)}"

        if player.keep_connected:
            txt += "\n> -# ♾️ **⠂Modo 24/7:** `Ativado`"

        txt += f"{vc_txt}\n"

        if player.command_log:
            txt += f"```ansi\n [34;1mÚltima Interação:[0m```**┕ {player.command_log_emoji} ⠂**{player.command_log}\n"

        if qlenght := len(player.queue):
            queue_txt = ""

            has_stream = False

            current_time += datetime.timedelta(milliseconds=player.current.duration)

            queue_duration = 0

            for n, t in enumerate(player.queue):
                if t.is_stream:
                    has_stream = True

                elif n != 0:
                    queue_duration += t.duration

                if n > 7:
                    if has_stream:
                        break
                    continue

                if has_stream:
                    duration = time_format(t.duration) if not t.is_stream else "🔴 Ao vivo"

                    queue_txt += (
                        f"-# `┌ {n + 1})` {md_link(t.title, t.uri, limit=34)}\n"
                        f"-# `└` {app_emoji(player.bot, 'history')} `{duration}`"
                        + (f" - `Repetições: {t.track_loops}`" if t.track_loops else "")
                        + f" **|** {app_emoji(player.bot, 'user')} <@{t.requester}>\n"
                    )

                else:
                    duration = (
                        f"<t:{int((current_time + datetime.timedelta(milliseconds=queue_duration)).timestamp())}:R>"
                    )

                    queue_txt += (
                        f"-# `┌ {n + 1})` {md_link(t.title, t.uri, limit=34)}\n"
                        f"-# `└` {app_emoji(player.bot, 'history')} {duration}"
                        + (f" - `Repetições: {t.track_loops}`" if t.track_loops else "")
                        + f" **|** {app_emoji(player.bot, 'user')} <@{t.requester}>\n"
                    )

            embed_queue = disnake.Embed(
                title=f"Músicas na fila: {qlenght}",
                color=player.bot.get_color(player.guild.me),
                description=f"\n{queue_txt}",
            )

            if (
                not has_stream
                and not player.loop
                and not player.keep_connected
                and not player.paused
                and not player.current.is_stream
            ):
                embed_queue.description += f"\n`[` {app_emoji(player.bot, 'history')} `As músicas acabam` <t:{int((current_time + datetime.timedelta(milliseconds=queue_duration + player.current.duration)).timestamp())}:R> {app_emoji(player.bot, 'history')} `]`"

            embed_queue.set_image(url=queue_img)

        elif len(player.queue_autoplay):
            queue_txt = ""

            has_stream = False

            current_time += datetime.timedelta(milliseconds=player.current.duration)

            queue_duration = 0

            for n, t in enumerate(player.queue_autoplay):
                if t.is_stream:
                    has_stream = True

                elif n != 0:
                    queue_duration += t.duration

                if n > 7:
                    if has_stream:
                        break
                    continue

                if has_stream:
                    duration = time_format(t.duration) if not t.is_stream else "🔴 Ao vivo"

                    queue_txt += (
                        f"-# `┌ {n + 1})` {md_link(t.title, t.uri, limit=34)}\n"
                        f"-# `└` {app_emoji(player.bot, 'history')} `{duration}`"
                        + (f" - `Repetições: {t.track_loops}`" if t.track_loops else "")
                        + f" **|** {app_emoji(player.bot, 'like')} `Recomendada`\n"
                    )

                else:
                    duration = (
                        f"<t:{int((current_time + datetime.timedelta(milliseconds=queue_duration)).timestamp())}:R>"
                    )

                    queue_txt += (
                        f"-# `┌ {n + 1})` {md_link(t.title, t.uri, limit=34)}\n"
                        f"-# `└` {app_emoji(player.bot, 'history')} {duration}"
                        + (f" - `Repetições: {t.track_loops}`" if t.track_loops else "")
                        + f" **|** {app_emoji(player.bot, 'like')} `Recomendada`\n"
                    )

            embed_queue = disnake.Embed(
                title="Próximas músicas recomendadas:",
                color=player.bot.get_color(player.guild.me),
                description=f"\n{queue_txt}",
            )

            embed_queue.set_image(url=queue_img)

        embed.description = txt

        embed.set_image(url=player.current.thumb or asset_url("player_equalizer_2.gif"))

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
    return DefaultStaticSkin()
