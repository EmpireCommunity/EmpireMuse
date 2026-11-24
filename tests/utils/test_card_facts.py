"""Os fatos do cartão (``utils/music/card_facts.py``): o ``CardSpec`` montado do player (call privada, estados, R, fila,
texto de fora) e as regras puras da barra de texto de 15 s. Player de mentira, sem rede nem relógio."""

from collections import deque
from types import SimpleNamespace

import pytest

from empiremuse.rules.card import QUEUE_FACTS_MAX, By, CardState
from empiremuse.utils.music import card_facts
from empiremuse.utils.music.card_facts import (
    FLAG_AUTOPLAY,
    FLAG_REPEAT_QUEUE,
    CardSettings,
    build,
    text_bar_interval_s,
    text_bar_segment,
)

pytestmark = pytest.mark.critical

SETTINGS = CardSettings(skin="muse_expanded_black", theme="black", locale="pt-BR")
COVER = "https://i.scdn.co/image/abc"
SECRET = "Segredo da Call"


class Track(SimpleNamespace):
    def __init__(self, title="Noite em Órbita", *, stream=False, autoplay=False, source="spotify", requester=7, **kw):
        fields = dict(
            single_title=title,
            authors_string="Lua Violeta",
            album_name="Órbita Lenta",
            playlist_name="",
            info={"sourceName": source},
            is_stream=stream,
            duration=209_000,
            requester=requester,
            autoplay=autoplay,
            track_loops=0,
            thumb=COVER,
        )
        fields.update(kw)
        super().__init__(**fields)


def channel(private=False):
    perms = SimpleNamespace(view_channel=not private, connect=True)
    return SimpleNamespace(
        name=SECRET, guild=SimpleNamespace(default_role="everyone"), permissions_for=lambda _r: perms
    )


def player(*, current=None, voice="public", view=None, station=None, **kw):
    voice_channel = {"public": channel(), "private": channel(private=True), "none": None}[voice]
    members = {7: SimpleNamespace(display_name="Alex"), 8: SimpleNamespace(display_name="Bia")}
    guild = SimpleNamespace(
        me=SimpleNamespace(display_name="Muse 2", voice=SimpleNamespace(channel=voice_channel)),
        get_member=members.get,
    )
    jukebox = SimpleNamespace(view=lambda _p: view, station_view=lambda _p: station)
    bot = SimpleNamespace(pool=SimpleNamespace(radio=SimpleNamespace(jukebox=jukebox, locations=None)))
    base = dict(guild=guild, bot=bot, current=current, paused=False, position=72_400, queue=deque(), played=deque())
    base.update(volume=80, queue_autoplay=deque(), autoplay=False, nightcore=False, keep_connected=False)
    base.update(restrict_mode=False, loop=False)
    base.update(kw)
    return SimpleNamespace(**base)


def radio_view(on_air=True, title="Faixa ICY @everyone"):
    return SimpleNamespace(
        uuid="u-1",
        name="Boa FM",
        title=title,
        country="BR",
        place_node=None,
        codec="mp3",
        bitrate=128,
        attempt=0 if on_air else 2,
        on_air=on_air,
        vote=None,
    )


# ---------------------------------------------------------------------- call privada


@pytest.mark.parametrize("voice", ["private", "none"])
def test_a_private_or_unknown_call_shows_nothing_of_what_plays(voice):
    queue = deque([Track("Próxima", requester=8)])
    spec = build(player(current=Track(), voice=voice, queue=queue), SETTINGS)

    assert spec.hidden and spec.track is None and spec.cover is None and spec.voice_name == ""
    assert spec.queue == () and spec.queue_total == 1 and spec.radio is None
    assert SECRET not in repr(spec) and "Alex" not in repr(spec) and "Próxima" not in repr(spec)


@pytest.mark.regression
def test_a_private_call_card_shows_the_track_only_in_the_call_own_chat():
    in_chat = player(current=Track(), voice="private")
    in_chat.guild.me.voice.channel.id = 700
    elsewhere = player(current=Track(), voice="private", text_channel=SimpleNamespace(id=701))
    elsewhere.guild.me.voice.channel.id = 700
    in_chat.text_channel = SimpleNamespace(id=700)

    assert build(in_chat, SETTINGS).hidden is False and build(elsewhere, SETTINGS).hidden is True


def test_a_private_call_in_radio_mode_hides_the_icy_title():
    spec = build(player(voice="private", view=radio_view()), SETTINGS)

    assert spec.hidden and spec.radio is None and spec.state is CardState.RADIO_LIVE


# ---------------------------------------------------------------------- estados


@pytest.mark.parametrize(
    ("kw", "state", "paused_at", "duration"),
    [
        ({}, CardState.IDLE, None, None),
        ({"current": Track()}, CardState.PLAYING, None, 209),  # tocando: a posição envelhece, não entra
        ({"current": Track(), "paused": True}, CardState.PAUSED, 72, 209),  # pausada: a posição exata
        ({"current": Track(stream=True)}, CardState.STREAM, None, None),
        ({"current": Track(stream=True), "paused": True}, CardState.PAUSED, None, None),
    ],
)
def test_the_state_and_only_the_paused_position(kw, state, paused_at, duration):
    spec = build(player(**kw), SETTINGS)

    assert spec.state is state and spec.paused_at_s == paused_at
    assert (spec.track.duration_s if spec.track else None) == duration
    assert spec.cover == (COVER if "current" in kw else None)


@pytest.mark.parametrize(("on_air", "state"), [(True, CardState.RADIO_LIVE), (False, CardState.RADIO_RETRY)])
def test_radio_mode_reads_the_view_without_track_or_cover(on_air, state):
    spec = build(player(current=Track(), view=radio_view(on_air)), SETTINGS)

    assert spec.state is state and spec.track is None and spec.cover is None
    assert spec.radio.name == "Boa FM" and spec.radio.logo_key == "u-1" and "@everyone" not in spec.radio.title
    assert spec.radio.attempt == (0 if on_air else 2) and spec.motion is False


def test_hostile_track_text_reaches_the_card_clean():
    spec = build(player(current=Track("Hit\n@everyone discord.gg/x")), SETTINGS)

    assert "\n" not in spec.track.title and "@everyone" not in spec.track.title and "discord.gg" not in spec.track.title
    assert "\\" not in spec.track.title  # a imagem não leva escape de markdown


@pytest.mark.parametrize(("from_station", "by", "requester"), [(True, By.STATION, ""), (False, By.MEMBER, "Alex")])
def test_station_mode_names_the_station_and_its_tracks_have_no_requester(from_station, by, requester):
    station = SimpleNamespace(ref="s-1", name="Lo-fi @here da Madrugada", kind="genre", since=0.0, playing=from_station)
    spec = build(player(current=Track(), station=station), SETTINGS)

    assert (
        spec.state is CardState.PLAYING and spec.station.name.startswith("Lo-fi") and "@here" not in spec.station.name
    )
    assert (spec.track.by, spec.track.requester) == (by, requester)


def test_a_private_call_in_station_mode_hides_the_station():
    station = SimpleNamespace(ref="s-1", name="Lo-fi", kind="genre", since=0.0, playing=True)

    assert build(player(current=Track(), voice="private", station=station), SETTINGS).station is None


# ---------------------------------------------------------------------- fila, origem e chips


def test_the_queue_is_capped_and_named_and_recommended_tracks_have_no_requester():
    queued = build(player(current=Track(), queue=deque(Track(f"t{n}", requester=8) for n in range(12))), SETTINGS)
    recommended = build(
        player(current=Track(), autoplay=True, queue_autoplay=deque([Track("rec", autoplay=True)])), SETTINGS
    )
    empty = build(player(current=Track()), SETTINGS)

    assert (queued.queue_kind, queued.queue_total, len(queued.queue)) == ("queue", 12, QUEUE_FACTS_MAX)
    assert queued.queue[0].requester == "Bia"
    assert (recommended.queue_kind, recommended.queue[0].requester) == ("recommended", "")
    assert FLAG_AUTOPLAY in recommended.flags
    assert (empty.queue_kind, empty.queue_total) == ("empty", 0)


class CountingQueue(deque):
    """Uma fila enorme que conta quantas faixas foram lidas dela."""

    def __init__(self, tracks):
        super().__init__(tracks)
        self.read = 0

    def __iter__(self):
        for track in super().__iter__():
            self.read += 1
            yield track


@pytest.mark.regression
def test_a_huge_queue_is_read_only_up_to_the_card_cap():
    queue = CountingQueue(Track(f"t{n}", requester=8) for n in range(5_000))

    spec = build(player(current=Track(), queue=queue), SETTINGS)

    assert (spec.queue_total, len(spec.queue)) == (5_000, QUEUE_FACTS_MAX) and queue.read <= QUEUE_FACTS_MAX


@pytest.mark.parametrize(
    ("source", "code", "autoplay", "requester", "by"),
    [
        ("spotify", "SP", False, "Alex", By.MEMBER),
        ("youtube", "YT", True, "", By.RECOMMENDED),
        ("https://evil.example", "WEB", False, "Alex", By.MEMBER),  # fonte desconhecida: sigla fixa
    ],
)
def test_the_source_is_a_fixed_code_and_requester_only_for_members(source, code, autoplay, requester, by):
    track = build(player(current=Track(source=source, autoplay=autoplay)), SETTINGS).track

    assert (track.source, track.requester, track.by, track.origin) == (code, requester, by, "Órbita Lenta")


def test_flags_session_and_volume():
    spec = build(player(current=Track(), loop="queue", played=deque(range(9)), volume=120), SETTINGS)

    assert FLAG_REPEAT_QUEUE in spec.flags and spec.session_played == 5 and spec.volume == 120


@pytest.mark.parametrize(
    ("saved", "paused", "motion"), [(None, False, True), (False, False, False), (True, True, False)]
)
def test_the_disc_moves_only_if_the_server_allows_and_it_is_playing(saved, paused, motion):
    spec = build(player(current=Track(), paused=paused), SETTINGS._replace(disc_motion=saved))

    assert spec.motion is motion


def test_no_cover_url_outside_https_reaches_the_spec():
    assert build(player(current=Track(thumb="http://10.0.0.1/x.png")), SETTINGS).cover is None


# ---------------------------------------------------------------------- barra em texto de 15 s (opção C)


@pytest.mark.parametrize(
    ("position_ms", "duration_ms", "segment"),
    [
        (0, 209_000, 0),
        (17_000, 209_000, 0),
        (18_000, 209_000, 1),  # cada segmento vale cerca de 17,4 s numa faixa de 3:29
        (209_000, 209_000, 12),
        (400_000, 209_000, 12),  # passou do fim: barra cheia
        (10_000, 0, None),  # duração desconhecida
        (None, 209_000, None),
        (-1, 209_000, None),
    ],
)
def test_the_text_bar_has_12_segments(position_ms, duration_ms, segment):
    assert text_bar_segment(position_ms, duration_ms) == segment


def test_a_track_edits_the_text_bar_at_most_12_times():
    segments = {text_bar_segment(ms, 209_000) for ms in range(0, 209_001, 15_000)}

    assert len(segments) <= 12


@pytest.mark.parametrize(("playing", "interval"), [(0, 15), (75, 15), (76, 16), (375, 75), (True, 15), ("x", 15)])
def test_the_text_bar_interval_grows_with_the_players_on_the_bot(playing, interval):
    assert text_bar_interval_s(playing) == interval
