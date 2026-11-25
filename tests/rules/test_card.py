"""Regras puras do ``CardSpec`` das skins de imagem: texto de fora limpo e no teto, call privada, formato e anexo."""

import pytest

from empiremuse.rules.card import (
    ARTISTS_MAX,
    CODEC_MAX,
    DEGRADE_AT,
    NAME_MAX,
    ORIGIN_MAX,
    QUEUE_FACTS_MAX,
    RENDER_VERSION,
    SESSION_PLAYED_MAX,
    STATION_MAX,
    TITLE_MAX,
    WHOLE_MAX,
    By,
    CardSpec,
    CardState,
    make_item,
    make_radio,
    make_station,
    make_track,
    motion_allowed,
    seal,
)

pytestmark = pytest.mark.critical

HOSTILE = [
    "abc‮def",
    "@everyone ouçam",
    "<@&123> cargo",
    "discord.gg/x convite",
    "veja https://evil.example/a",
    "[clique](https://evil.example)",
    "linha\nnova\ttab",
    "privado",
    "z" + "́" * 300,
    "x" * 100_000,
    "🎵 emoji",
    "ß" * 8,  # ``upper`` dobra: o teto vale depois dele
]


def utf16(text: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def spec(**changes) -> CardSpec:
    base = CardSpec(
        skin="muse_expanded_black",
        theme="black",
        scale=1,
        render_version=RENDER_VERSION,
        locale="pt-BR",
        state=CardState.PLAYING,
        hidden=False,
        track=make_track(title="Evidências", artists="Chitãozinho & Xororó", requester="Ana", source="yt"),
        cover="https://i.ytimg.com/vi/x/hqdefault.jpg?sig=1",
        paused_at_s=None,
        queue_total=2,
        queue=(make_item(title="Faixa 2", artists="B", requester="Bia"),),
        queue_kind="queue",
        session_played=0,
        flags=frozenset(),
        repeat_left=0,
        volume=100,
        bot_name="Muse",
        voice_name="Sala de música",
        radio=None,
        station=None,
        motion=True,
    )
    return base._replace(**changes)


def assert_clean(text: str, limit: int) -> None:
    assert utf16(text) <= limit
    for bad in ("‮", "\n", "\t", "", "@everyone", "<@&", "://", "discord.gg", "](", "́" * 5):
        assert bad not in text


@pytest.mark.parametrize("hostile", HOSTILE)
def test_external_text_reaches_the_card_clean_and_capped(hostile):
    track = make_track(title=hostile, artists=hostile, origin=hostile, requester=hostile)
    item = make_item(title=hostile, artists=hostile, requester=hostile)
    radio = make_radio(name=hostile, title=hostile + r" \*escapado\*", country=hostile, place=hostile, codec=hostile)
    station = make_station(name=hostile, kind=hostile)
    sealed = seal(spec(bot_name=hostile, voice_name=hostile))

    for text, limit in (
        (track.title, TITLE_MAX),
        (track.artists, ARTISTS_MAX),
        (track.origin, ORIGIN_MAX),
        (track.requester, NAME_MAX),
        (item.title, TITLE_MAX),
        (item.artists, ARTISTS_MAX),
        (item.requester, NAME_MAX),
        (radio.title, TITLE_MAX),
        (radio.name, STATION_MAX),
        (radio.country, NAME_MAX),
        (radio.place, NAME_MAX),
        (radio.codec, CODEC_MAX),
        (station.name, STATION_MAX),
        (station.kind, NAME_MAX),
        (sealed.bot_name, NAME_MAX),
        (sealed.voice_name, NAME_MAX),
    ):
        assert_clean(text, limit)
    assert "\\" not in track.title, "a imagem não leva escape de markdown"


def test_source_is_a_fixed_code_and_requester_only_for_member_requests():
    assert make_track(title="t", artists="a", source="sp").source == "SP"
    assert make_track(title="t", artists="a", source="https://evil.example").source == "WEB"
    assert make_track(title="t", artists="a", requester="Ana", by=By.RECOMMENDED).requester == ""
    assert make_track(title="t", artists="a", duration_s="abc").duration_s is None
    assert make_track(title="t", artists="a", duration_s=1e308).duration_s == WHOLE_MAX
    assert make_radio(name="r", bitrate="9" * 400, vote=(10**20, -1)).bitrate == 0
    assert make_radio(name="r", vote=(10**20, -1)).vote == (WHOLE_MAX, 0)


@pytest.mark.parametrize("state", list(CardState))
def test_private_call_hides_track_cover_voice_queue_titles_radio_and_station(state):
    radio = make_radio(name="Rádio X", title="Artista - Música", country="Brasil", place="SP", logo_key="u-1")
    sealed = seal(
        spec(hidden=True, state=state, queue_total=5, radio=radio, station=make_station(name="Estação Y", kind="genre"))
    )

    assert sealed.track is None and sealed.cover is None and sealed.voice_name == ""
    assert sealed.queue == () and sealed.queue_total == 5
    assert sealed.radio is None and sealed.station is None, "nem a estação nem o título ICY numa call privada"


def test_only_a_pause_carries_a_position_and_caps_hold():
    queue = tuple(make_item(title=f"t{i}", artists="a") for i in range(20))
    playing = seal(spec(paused_at_s=42, queue=queue, queue_total=0, session_played=99))
    paused = seal(spec(state=CardState.PAUSED, paused_at_s=42))

    assert playing.paused_at_s is None and paused.paused_at_s == 42
    assert len(playing.queue) == QUEUE_FACTS_MAX and playing.queue_total == 20
    assert playing.session_played == SESSION_PLAYED_MAX


@pytest.mark.parametrize(
    ("disc_motion", "state", "expected"),
    [
        (None, CardState.PLAYING, True),
        (True, CardState.STREAM, True),
        (False, CardState.PLAYING, False),
        (True, CardState.PAUSED, False),
        (True, CardState.RADIO_LIVE, False),
        (None, CardState.IDLE, False),
    ],
)
def test_motion_needs_the_setting_and_a_playing_state(disc_motion, state, expected):
    assert motion_allowed(disc_motion, state) is expected


NOW = 100.0
