"""Os componentes das skins do EmpireMuse (spec §1.2): os MESMOS ``custom_id`` e o mesmo comportamento das Clássicas.

Linha 1: tocar/pausar, voltar, parar, pular e fila. Linha 2: o select ``musicplayer_dropdown_inter`` (11 opções fixas e até 4
condicionais). No canal da Jukebox (``player.static``), o select da fila ``musicplayer_queue_dropdown``. Todo rótulo vem do catálogo
(D9): nenhum texto fixo em português. Rótulo de select não renderiza markdown, então o título da faixa passa por
``external_text(markdown=False)``. Em call privada o select da fila não aparece (ele mostraria os títulos).
"""

from __future__ import annotations

import itertools
from typing import Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.text import external_text
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.converters import get_button_style, time_format
from empiremuse.utils.music.jukebox_ui import autoplay_label
from empiremuse.utils.others import PlayerControls

LABEL_MAX = 100
PLACEHOLDER_MAX = 150
QUEUE_SELECT_MAX = 25
QUEUE_LABEL_MAX = 47
QUEUE_VALUE_TITLE_MAX = 96  # o valor que o tratador do select já lê ("NN.título")


def _option(player: Any, label: str, emoji: str, value: str, description_key: str) -> disnake.SelectOption:
    return disnake.SelectOption(
        label=fit(label, LABEL_MAX),
        emoji=app_emoji(player.bot, emoji),
        value=value,
        description=fit(player.tr(description_key), LABEL_MAX),
    )


def _buttons(player: Any) -> list[disnake.ui.Button]:
    bot = player.bot
    return [
        disnake.ui.Button(
            emoji=app_emoji(bot, "playpause"),
            custom_id=PlayerControls.pause_resume,
            style=get_button_style(player.paused),
        ),
        disnake.ui.Button(emoji=app_emoji(bot, "backward"), custom_id=PlayerControls.back),
        disnake.ui.Button(emoji=app_emoji(bot, "stop"), custom_id=PlayerControls.stop),
        disnake.ui.Button(emoji=app_emoji(bot, "skip"), custom_id=PlayerControls.skip),
        disnake.ui.Button(
            emoji=app_emoji(bot, "music_queue"),
            custom_id=PlayerControls.queue,
            disabled=not (player.queue or player.queue_autoplay),
        ),
    ]


# (chave do rótulo ou None = calculado, emoji, valor, chave da descrição)
_FIXED = (
    ("player.add_song.label", "add_music", PlayerControls.add_song, "player.add_a_song_playlist_to.description"),
    (
        "player.add_to_your_favorites.label",
        "loved",
        PlayerControls.add_favorite,
        "player.add_the_current_song_to.description",
    ),
    (
        "player.play_from_the_start.label",
        "fast_backward",
        PlayerControls.seek_to_start,
        "player.go_back_to_the_start.description",
    ),
    (None, "volume", PlayerControls.volume, "player.adjust_the_volume.description"),
    ("player.shuffle.label", "shuffle", PlayerControls.shuffle, "player.shuffle_the_queue_songs.description"),
    ("player.readd.label", "playlist_add", PlayerControls.readd, "player.add_the_played_songs_back.description"),
    (
        "player.repeat.label",
        "repeat_all",
        PlayerControls.loop_mode,
        "player.enable_disable_repeating_the_song.description",
    ),
    (None, "sound_spectrum", PlayerControls.nightcore, "player.effect_that_raises_the_speed.description"),
    (None, "refresh", PlayerControls.autoplay, "player.automatic_song_adding_system_when.description"),
    (
        "jbx.menu.scrobble",
        "lastfm",
        PlayerControls.lastfm_scrobble,
        "player.enable_disable_scrobbling_logging_of.description",
    ),
    (None, "lock", PlayerControls.restrict_mode, "player.restrict_mode.description"),
)


def _state_label(player: Any, value: str, hidden: bool) -> str:
    tr = player.tr
    if value == PlayerControls.volume:
        return tr("player.volume.label", volume=str(player.volume))
    if value == PlayerControls.nightcore:
        on = player.nightcore
        return tr("player.disable_the_nightcore_effect.label" if on else "player.enable_the_nightcore_effect.label")
    if value == PlayerControls.autoplay:
        if hidden:  # call privada: sem o nome da estação do servidor
            return tr("player.disable_autoplay.label" if player.autoplay else "player.enable_autoplay.label")
        return autoplay_label(player)
    on = player.restrict_mode
    return tr("player.disable_restricted_mode.label" if on else "player.enable_restricted_mode.label")


def _fixed_options(player: Any, hidden: bool) -> list[disnake.SelectOption]:
    return [
        _option(player, player.tr(key) if key else _state_label(player, value, hidden), emoji, value, desc)
        for key, emoji, value, desc in _FIXED
    ]


def _conditional_options(player: Any) -> list[disnake.SelectOption]:
    tr = player.tr
    options = []
    current = player.current
    if current is not None and current.ytid and player.node.lyric_support:
        options.append(
            _option(
                player,
                tr("jbx.menu.lyrics"),
                "lyrics",
                PlayerControls.lyrics,
                "player.get_the_lyrics_of_the.description",
            )
        )
    if player.mini_queue_feature:
        options.append(
            _option(
                player,
                tr("player.player_mini_queue.label"),
                "music_queue",
                PlayerControls.miniqueue,
                "player.enable_disable_the_player_s.description",
            )
        )
    if isinstance(player.last_channel, disnake.VoiceChannel):
        options.append(
            _option(
                player,
                tr("player.automatic_status.label"),
                "speaker",
                PlayerControls.set_voice_status,
                "player.set_up_the_voice_channel.description",
            )
        )
    if not player.static and not player.has_thread:
        options.append(
            _option(
                player,
                tr("jbx.menu.thread"),
                "comment",
                PlayerControls.jukebox_thread,
                "player.create_a_temporary_thread_to.description",
            )
        )
    return options


def _queue_select(player: Any) -> disnake.ui.Select | None:
    queue = player.queue or player.queue_autoplay
    if not queue:
        return None
    options = []
    for n, track in enumerate(itertools.islice(queue, QUEUE_SELECT_MAX)):
        length = player.tr("jbx.state.radio") if track.is_stream else time_format(track.duration)
        options.append(
            disnake.SelectOption(
                label=external_text(f"{n + 1}. {track.single_title}", QUEUE_LABEL_MAX, markdown=False) or f"{n + 1}.",
                description=external_text(f"[{length}] {track.authors_string}", QUEUE_LABEL_MAX, markdown=False)
                or None,
                value=f"{n:02d}.{track.title[:QUEUE_VALUE_TITLE_MAX]}",
            )
        )
    return disnake.ui.Select(
        placeholder=fit(player.tr("player.next_songs.placeholder"), PLACEHOLDER_MAX),
        custom_id="musicplayer_queue_dropdown",
        min_values=0,
        max_values=1,
        required=False,
        options=options,
    )


def components(player: Any, *, hidden: bool) -> list[Any]:
    """Os componentes do player tocando, na ordem das Clássicas (o select de opções vem logo depois dos 5 botões)."""
    menu = disnake.ui.Select(
        placeholder=fit(player.tr("player.more_options.placeholder"), PLACEHOLDER_MAX),
        custom_id="musicplayer_dropdown_inter",
        min_values=0,
        max_values=1,
        required=False,
        options=_fixed_options(player, hidden) + _conditional_options(player),
    )
    items: list[Any] = [*_buttons(player), menu]
    if player.static and not hidden and (queue_select := _queue_select(player)) is not None:
        items.append(queue_select)
    return items
