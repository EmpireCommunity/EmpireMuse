"""Skins do EmpireMuse (S5, parte das skins): texto de fora sem menção nem link, call privada sem faixa, texto estável entre duas
leituras (a barra em texto só edita quando muda) e o acordo com o render (``card``, ``theme``, ``project``, embed sem imagem).

Fakes do player com o ``Translator`` real e os catálogos reais (pt-BR sobre en-US): uma chave sem texto apareceria crua.
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

import disnake
import pytest

from empiremuse.i18n import loader
from empiremuse.rules.i18n import Translator
from empiremuse.utils.music.models import np_steps
from empiremuse.utils.music.skins.muse import _card, muse_text

pytestmark = pytest.mark.critical

HOSTILE = "@everyone <@123> [x](https://evil.example) discord.gg/abc\nlinha"
RAW_FORBIDDEN = ("@everyone", "<@123>", "](https://evil", "discord.gg/abc", "\nlinha")
REQUESTER_ID = 42
NOW = 1_800_000_000.0
IMAGE_IDS = ("muse_expanded_black", "muse_slim_white", "muse_radio_black")


@pytest.fixture(scope="module")
def tr():
    loaded = loader.load_catalogs(None)
    return Translator("pt-BR", loaded["pt-BR"].entries, loaded["en-US"].entries)


def _voice(*, private: bool):
    guild = SimpleNamespace(default_role=object())
    perms = SimpleNamespace(view_channel=not private, connect=True)
    return SimpleNamespace(name=HOSTILE, guild=guild, permissions_for=lambda role: perms)


def _track(title: str = HOSTILE, *, duration: int = 209_000):
    return SimpleNamespace(
        single_title=title,
        title=title,
        authors_string=HOSTILE,
        album_name=HOSTILE,
        playlist_name=HOSTILE,
        uri="https://www.youtube.com/watch?v=abc",
        search_uri="",
        duration=duration,
        is_stream=False,
        requester=REQUESTER_ID,
        autoplay=False,
        track_loops=0,
        thumb="https://i.ytimg.com/vi/abc/hq.jpg",
        info={"sourceName": "youtube"},
        ytid="abc",
    )


class _FakePlayer(SimpleNamespace):
    def __str__(self) -> str:  # o rodapé sem dica: servidor de música e versão, como o player real
        return "LOCAL (v4)"


def _player(tr, *, private: bool = False, static: bool = False, paused: bool = False, position: int = 72_000):
    voice = _voice(private=private)
    member = SimpleNamespace(display_name=HOSTILE)
    me = SimpleNamespace(display_name="Muse 2", voice=SimpleNamespace(channel=voice))
    avatar = SimpleNamespace(replace=lambda **kw: SimpleNamespace(url="https://cdn.discordapp.com/avatar.png"))
    bot = SimpleNamespace(
        get_color=lambda me: 0x8B5CF6,
        config={"HINT_RATE": 4},
        user=SimpleNamespace(display_avatar=avatar),
        pool=SimpleNamespace(radio=SimpleNamespace(jukebox=None)),
        music=SimpleNamespace(players={}),
    )
    return _FakePlayer(
        bot=bot,
        guild=SimpleNamespace(me=me, get_member=lambda member_id: member),
        tr=tr,
        current=_track(),
        queue=[_track(f"q{n} {HOSTILE}") for n in range(10)],
        queue_autoplay=[],
        paused=paused,
        position=position,
        autoplay=True,
        loop="queue",
        nightcore=True,
        keep_connected=False,
        restrict_mode=True,
        volume=80,
        static=static,
        mini_queue_enabled=True,
        mini_queue_feature=True,
        has_thread=False,
        last_channel=None,
        node=SimpleNamespace(lyric_support=True),
        command_log="",
        command_log_emoji="",
        current_hint="",
    )


def _texts(data: dict) -> str:
    """Todo texto que o Discord mostra no payload: embeds (autor, título, descrição, rodapé) e rótulos dos selects."""
    out = []
    for embed in data["embeds"]:
        d = embed.to_dict()
        out += [
            d.get("title", ""),
            d.get("description", ""),
            d.get("author", {}).get("name", ""),
            d.get("footer", {}).get("text", ""),
        ]
    for item in data["components"]:
        if isinstance(item, disnake.ui.Select):
            out.append(item.placeholder or "")
            out += [f"{o.label} {o.description or ''}" for o in item.options]
    return "\n".join(out)


def _image(skin_id: str):
    return import_module(f"empiremuse.utils.music.skins.muse.{skin_id}").load()


def _image_build(skin_id: str):
    def build(player, now):
        return _image(skin_id).build(player, now)

    build.__name__ = skin_id
    return build


BUILDS = [muse_text.build, *(_image_build(i) for i in IMAGE_IDS)]


@pytest.mark.parametrize("static", [False, True])
@pytest.mark.parametrize("build", BUILDS, ids=lambda b: b.__module__.rsplit(".", 1)[-1] + "." + b.__name__)
def test_external_text_never_becomes_mention_or_link(tr, build, static):
    text = _texts(build(_player(tr, static=static), NOW))
    assert not any(raw in text for raw in RAW_FORBIDDEN)
    assert "jbx." not in text and "skin." not in text  # nenhuma chave sem texto no catálogo
    assert {part.split(">")[0] for part in text.split("<@")[1:]} <= {str(REQUESTER_ID)}


@pytest.mark.parametrize("static", [False, True])
@pytest.mark.parametrize("build", BUILDS, ids=lambda b: b.__module__.rsplit(".", 1)[-1] + "." + b.__name__)
def test_private_call_hides_track_requester_and_queue(tr, build, static):
    player = _player(tr, private=True, static=static)
    for attr in ("single_title", "authors_string", "album_name", "playlist_name"):
        setattr(player.current, attr, "SEGREDO")
    player.queue = [_track("SEGREDO")]
    data = build(player, NOW)
    text = _texts(data)
    embed = data["embeds"][0].to_dict()
    assert "SEGREDO" not in text
    assert f"<@{REQUESTER_ID}>" not in text
    assert "url" not in embed and "url" not in embed.get("author", {})
    assert all(getattr(c, "custom_id", "") != "musicplayer_queue_dropdown" for c in data["components"])


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("build", BUILDS, ids=lambda b: b.__module__.rsplit(".", 1)[-1] + "." + b.__name__)
def test_text_is_stable_between_two_reads(tr, build, paused):
    """Um segundo depois (com a posição andando junto), o payload inteiro tem a mesma chave estável (embeds E componentes):
    o ``auto_update`` de 15 s da barra em texto não edita a mensagem só pelo relógio."""
    first = build(_player(tr, paused=paused, position=72_000), NOW)
    later = build(_player(tr, paused=paused, position=72_000 if paused else 73_000), NOW + 1)
    assert np_steps.payload_key(first) == np_steps.payload_key(later)


def test_text_bar_moves_with_the_segment(tr):
    early = muse_text.build(_player(tr, position=0), NOW)
    late = muse_text.build(_player(tr, position=150_000), NOW + 150)
    assert early["embeds"][0].description != late["embeds"][0].description


@pytest.mark.parametrize(
    ("skin_id", "card", "theme"),
    [
        ("muse_expanded_black", "expanded", "black"),
        ("muse_expanded_white", "expanded", "white"),
        ("muse_slim_black", "slim", "black"),
        ("muse_slim_white", "slim", "white"),
        ("muse_radio_black", "visor_jukebox", "black"),
        ("muse_radio_white", "visor_jukebox", "white"),
    ],
)
def test_image_skin_contract_with_the_renderer(tr, skin_id, card, theme):
    skin = _image(skin_id)
    renderer = import_module(f"empiremuse.utils.render.cards.{card}")
    assert (skin.name, skin.card, skin.theme) == (skin_id, card, theme)
    assert skin.project is renderer.project
    assert skin_id in renderer.SKINS
    embed = skin.build(_player(tr), NOW)["embeds"][0].to_dict()
    assert "image" not in embed and "thumbnail" not in embed
    assert embed["author"]["url"].startswith("https://")


@pytest.mark.parametrize("skin_id", ["muse_radio_portrait_black", "muse_expanded_red", "../x", "muse_text"])
def test_unknown_image_skin_id_is_refused(skin_id):
    with pytest.raises(ValueError):
        _card.parse_id(skin_id)


def test_text_skin_has_no_render_family():
    skin = muse_text.load()
    assert skin.card is None and skin.project is None


@pytest.mark.parametrize("build", BUILDS, ids=lambda b: b.__module__.rsplit(".", 1)[-1] + "." + b.__name__)
def test_components_keep_the_classic_custom_ids(tr, build):
    data = build(_player(tr, static=True), NOW)
    assert [getattr(c, "custom_id", None) for c in data["components"]] == [
        "musicplayer_playpause",
        "musicplayer_back",
        "musicplayer_stop",
        "musicplayer_skip",
        "musicplayer_queue",
        "musicplayer_dropdown_inter",
        "musicplayer_queue_dropdown",
    ]


def _radio_jukebox(*, on_air: bool = True):
    vote = SimpleNamespace(votes=1, needed=3)
    view = SimpleNamespace(
        name=HOSTILE,
        title=HOSTILE,
        country=HOSTILE,
        on_air=on_air,
        since=NOW - 2700,
        attempt=0 if on_air else 2,
        retry_at=None if on_air else NOW + 30,
        vote=vote,
    )
    return SimpleNamespace(view=lambda player: view, station_view=lambda player: None)


@pytest.mark.parametrize("on_air", [True, False])
@pytest.mark.parametrize("build", BUILDS, ids=lambda b: b.__module__.rsplit(".", 1)[-1] + "." + b.__name__)
def test_radio_mode_text_is_neutralized_and_hidden_in_private_call(tr, build, on_air):
    player = _player(tr)
    player.bot.pool.radio.jukebox = _radio_jukebox(on_air=on_air)
    text = _texts(build(player, NOW))
    assert not any(raw in text for raw in RAW_FORBIDDEN)
    assert "jbx." not in text

    private = _player(tr, private=True)
    private.bot.pool.radio.jukebox = _radio_jukebox(on_air=on_air)
    private_text = _texts(build(private, NOW))
    assert "everyone" not in private_text  # nem o nome da estação nem o título ICY
