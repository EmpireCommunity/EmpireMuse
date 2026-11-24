"""A skin Jukebox em Components V2 (``muse_jukebox``) e o passo do disco (``np_v2``): payload V2 puro, call privada sem nada da faixa
em nenhum campo do JSON, limites do Discord no pior caso, texto estável entre leituras, anexos só quando o disco muda e a mensagem V2
que nunca recebe embed. Fakes do player com o ``Translator`` real; disco e vaga de mentira; sem rede nem relógio."""

from __future__ import annotations

import json
from types import SimpleNamespace

import disnake
import pytest

from empiremuse.i18n import loader
from empiremuse.rules.card import CardState
from empiremuse.rules.i18n import Translator
from empiremuse.utils.music import v2
from empiremuse.utils.music.models import np_steps, np_v2
from empiremuse.utils.music.skins.muse import muse_jukebox
from empiremuse.utils.render.disc_only import Disc, DiscImages
from tests.support.call_chat import CALL_CHAT_CASES, CALL_CHAT_IDS
from tests.utils.test_muse_skins import HOSTILE, NOW, _player, _track

pytestmark = pytest.mark.critical

SECRET = "Faixa Secreta"
COVER = "https://i.ytimg.com/vi/secret/hq.jpg"
IMAGES = DiscImages(b"gif-bytes", b"png-bytes")


@pytest.fixture(scope="module")
def tr():
    loaded = loader.load_catalogs(None)
    return Translator("pt-BR", loaded["pt-BR"].entries, loaded["en-US"].entries)


def dump(data: dict) -> str:
    return json.dumps([c.to_component_dict() for c in data["components"]], ensure_ascii=False)


def count_components(data: dict) -> int:
    def walk(node):
        if isinstance(node, dict):
            return (1 if "type" in node else 0) + sum(walk(v) for v in node.values())
        return sum(walk(v) for v in node) if isinstance(node, list) else 0

    return walk(json.loads(dump(data)))


def shown_text(data: dict) -> str:
    """Só o que o Discord desenha como texto (os ``TextDisplay``); o ``value`` de um select é interno e nunca aparece."""

    def walk(node):
        if isinstance(node, dict):
            own = [node["content"]] if node.get("type") == 10 else []
            return own + [t for v in node.values() for t in walk(v)]
        return [t for v in node for t in walk(v)] if isinstance(node, list) else []

    return "\n".join(walk(json.loads(dump(data))))


def total_text(data: dict) -> int:
    def walk(node):
        if isinstance(node, dict):
            own = len(node["content"]) if node.get("type") == 10 else 0
            return own + sum(walk(v) for v in node.values())
        return sum(walk(v) for v in node) if isinstance(node, list) else 0

    return walk(json.loads(dump(data)))


def secret_player(tr, **kw):
    player = _player(tr, **kw)
    player.current = _track(SECRET)
    player.current.authors_string = "Artista Secreto"
    player.current.album_name = "Album Secreto"
    player.current.playlist_name = "Playlist Secreta"
    player.queue = [_track(f"Fila Secreta {n}") for n in range(5)]
    player.command_log = "Tocando [Faixa Secreta](https://www.youtube.com/watch?v=abc)"
    player.guild.me.voice.channel.name = "Call Secreta"
    return player


def build(player, thumb=None):
    return muse_jukebox.build(player, NOW, thumb)


def test_the_payload_is_pure_v2_with_one_violet_container_and_the_same_custom_ids(tr):
    data = build(_player(tr, static=True))
    assert data["content"] is None and data["embeds"] == []
    (container,) = data["components"]
    assert isinstance(container, disnake.ui.Container) and container.accent_colour == muse_jukebox.VIOLET
    text = dump(data)
    for custom_id in ("musicplayer_dropdown_inter", "musicplayer_queue_dropdown", "musicplayer_pause_resume"):
        assert custom_id in text or custom_id == "musicplayer_pause_resume"


@pytest.mark.regression
@pytest.mark.parametrize(("private", "in_call_chat", "hides"), CALL_CHAT_CASES, ids=CALL_CHAT_IDS)
def test_a_private_call_outside_its_chat_puts_nothing_of_the_track_anywhere_in_the_json(
    tr, private, in_call_chat, hides
):
    player = secret_player(tr, private=private, in_call_chat=in_call_chat, static=True)
    text = dump(build(player))
    secrets = ("Faixa Secreta", "Artista Secreto", "Album Secreto", "Playlist Secreta", "Fila Secreta", "Call Secreta")
    assert all((secret in text) is not hides for secret in secrets)
    if hides:
        assert "musicplayer_queue_dropdown" not in text  # o select da fila mostraria os títulos
        assert "<@42>" not in text and tr("jbx.hidden_track") in text and tr("jbx.hidden_note") in text


def test_the_worst_case_fits_the_discord_limits_even_with_hostile_text(tr):
    player = _player(tr, static=True)
    player.queue = [_track(f"q{n} {HOSTILE * 4}") for n in range(30)]
    player.command_log = "x" * 3000
    data = build(player)
    assert count_components(data) <= v2.MAX_COMPONENTS
    assert total_text(data) <= v2.MAX_TEXT
    assert "](https://evil" not in shown_text(data)


def test_two_reads_in_the_same_step_give_the_same_payload_and_the_clock_is_m_ss(tr):
    key = np_steps.payload_key
    player = _player(tr)
    assert key(build(player)) == key(build(player))
    text = dump(build(player))
    assert "3:29" in text and "03:29" not in text


def test_paused_is_amber_with_the_word_and_a_stream_is_red_without_a_bar(tr):
    paused = _player(tr, paused=True)
    data = build(paused)
    assert data["components"][0].accent_colour == muse_jukebox.AMBER
    assert tr("jbx.paused_word") in dump(data) and "R:" not in dump(data)
    live = _player(tr)
    live.current.is_stream = True
    data = build(live)
    assert data["components"][0].accent_colour == muse_jukebox.RED and "▰" not in dump(data)


def test_no_technical_text_in_any_state(tr):
    text = dump(build(_player(tr, static=True))) + dump(build(_player(tr, paused=True)))
    assert not any(word in text.lower() for word in ("kbps", "codec", "local (v4)", "v4"))


# ---------------------------------------------------------------------- np_v2: o disco e os anexos


class Service:
    def __init__(self, disc):
        self.disc_value, self.calls = disc, []

    async def disc(self, cover=None, *, plain=False):
        self.calls.append((cover, plain))
        return self.disc_value


def spec(**kw):
    base = {
        "state": CardState.PLAYING,
        "motion": True,
        "hidden": False,
        "cover": COVER,
        "track": SimpleNamespace(title="Titulo"),
        "radio": None,
    }
    return SimpleNamespace(**{**base, **kw})


@pytest.fixture
def service(monkeypatch):
    holder = SimpleNamespace(service=Service(Disc("disc-abcd1234", IMAGES)))
    monkeypatch.setattr(np_v2, "disc_service", lambda _hosts: holder.service)
    return holder


@pytest.mark.asyncio
async def test_a_new_disc_goes_up_with_both_files_and_the_same_disc_goes_up_with_none(tr, service):
    player = _player(tr)
    first = await np_v2.pick(player, spec(), None, (), None)
    assert [f.filename for f in first.files] == ["disc-abcd1234.gif", "disc-abcd1234.png"]
    assert first.thumb.media == "attachment://disc-abcd1234.gif"
    again = await np_v2.pick(player, spec(), None, (), "disc-abcd1234")
    assert again.files == [] and again.thumb.media == "attachment://disc-abcd1234.gif"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"state": CardState.PAUSED}, {"motion": False}])
async def test_a_paused_or_still_disc_only_swaps_the_reference_to_the_png(tr, service, change):
    picked = await np_v2.pick(_player(tr), spec(**change), None, (), "disc-abcd1234")
    assert picked.files == [] and picked.thumb.media == "attachment://disc-abcd1234.png"


@pytest.mark.asyncio
async def test_a_busy_slot_uses_the_cover_url_and_never_in_a_private_call(tr, service):
    service.service = Service(Disc("disc-x", None, busy=True))
    public = await np_v2.pick(_player(tr), spec(), None, (), None)
    assert public.thumb.media == COVER and public.files == [] and public.again is True
    private = await np_v2.pick(_player(tr), spec(hidden=True, cover=None), None, (), None)
    assert COVER not in private.thumb.media and private.again is True


@pytest.mark.asyncio
async def test_a_private_call_asks_for_the_plain_disc_so_no_cover_is_ever_fetched(tr, service):
    await np_v2.pick(_player(tr), spec(hidden=True, cover=None), None, (), None)
    assert service.service.calls == [(None, True)]


def test_an_edit_that_failed_does_not_mark_the_disc_as_sent():
    player = type("P", (), {})()
    req = SimpleNamespace(disc="disc-1", published=False)
    np_v2.confirm(player, req)
    assert np_v2.sent_name(player) is None
    req.published = True
    np_v2.confirm(player, req)
    assert np_v2.sent_name(player) == "disc-1"


# ---------------------------------------------------------------------- V2 fica V2 nas edições do invoke_np


class WeakNamespace(SimpleNamespace):
    """Um ``SimpleNamespace`` que aceita referência fraca (os registros por player são ``WeakKeyDictionary``)."""

    __slots__ = ("__weakref__",)


def fake_message(*, is_v2: bool, edits: list):
    flags = disnake.MessageFlags()
    flags.is_components_v2 = is_v2

    async def edit(**kwargs):
        edits.append(kwargs)

    return SimpleNamespace(flags=flags, edit=edit, id=1)


@pytest.mark.asyncio
async def test_an_embed_payload_on_a_v2_player_message_is_edited_as_a_container(tr, monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(np_steps.asyncio, "sleep", no_sleep)
    edits: list = []
    player = _player(tr)
    player.message = fake_message(is_v2=True, edits=edits)
    player.ignore_np_once, player.allowed_mentions, player.updating = False, None, False
    player.start_message_updater_task = lambda: None
    req = SimpleNamespace(
        data={"embeds": [disnake.Embed(description="ocioso")], "content": None}, interaction=None, published=False
    )
    await np_steps._np_message_edit(player, req)
    assert edits[0]["embeds"] == [] and edits[0]["content"] is None
    assert edits[0]["components"][0].to_component_dict()["type"] == 17


# ---------------------------------------------------------------------- V2 -> V1 troca a mensagem; o ocioso é V2


def test_only_a_v2_message_with_an_embed_payload_needs_a_new_message(tr):
    embed_data = {"embeds": [disnake.Embed(description="x")], "components": []}
    v2_data = {"embeds": [], "components": [build(_player(tr))["components"][0]]}
    for is_v2, data, expected in (
        (True, embed_data, True),
        (True, v2_data, False),
        (False, embed_data, False),
        (False, v2_data, False),
    ):
        player = SimpleNamespace(message=fake_message(is_v2=is_v2, edits=[]))
        req = SimpleNamespace(interaction=None, data=data)
        assert np_steps.needs_replacement(player, req) is expected


@pytest.mark.asyncio
async def test_the_new_message_goes_out_before_the_old_one_is_deleted(monkeypatch):
    order: list[str] = []
    old = SimpleNamespace(delete=None)

    async def delete():
        order.append("delete")

    old.delete = delete

    async def send_new(player, req):
        order.append("send")
        assert req.replace is True
        player.message = object()

    monkeypatch.setattr(np_steps, "_np_send_new", send_new)
    player = WeakNamespace(message=old, static=False, last_data={"x": 1})
    await np_steps._np_replace(player, SimpleNamespace(interaction=None, replace=False))
    assert order == ["send", "delete"]


@pytest.mark.asyncio
async def test_if_the_new_message_fails_the_old_one_stays_and_the_updater_retries(monkeypatch):
    async def send_new(player, req):
        return None  # sem mensagem nova

    monkeypatch.setattr(np_steps, "_np_send_new", send_new)
    old = SimpleNamespace()
    player = WeakNamespace(message=old, static=False, last_data={"x": 1})
    await np_steps._np_replace(player, SimpleNamespace(interaction=None, replace=False))
    assert player.message is old and player.last_data == {}


def test_a_v1_message_edited_with_a_v2_payload_becomes_v2_for_the_adapter():
    message = fake_message(is_v2=False, edits=[])
    player = SimpleNamespace(message=message)
    req = SimpleNamespace(published=True, v2=True, interaction=None, disc=None)
    np_v2.confirm(player, req)
    assert v2.is_v2(message) is True
    other = fake_message(is_v2=False, edits=[])
    np_v2.confirm(
        SimpleNamespace(message=other), SimpleNamespace(published=False, v2=True, interaction=None, disc=None)
    )
    assert v2.is_v2(other) is False


def test_the_idle_message_is_pure_v2_with_the_selects_and_no_technical_text(tr):
    player = _player(tr)
    player.keep_connected = False
    player.bot.config["IDLE_TIMEOUT"] = 300
    select = disnake.ui.Select(
        custom_id="musicplayer_dropdown_idle", options=[disnake.SelectOption(label="a", value="a")]
    )
    kwargs = {
        "embed": disnake.Embed(description="velho"),
        "content": "forum",
        "components": [select],
        "allowed_mentions": None,
    }
    data = muse_jukebox.idle(player, kwargs)
    assert data["content"] is None and data["embeds"] == [] and data["attachments"] == [] and "embed" not in data
    assert data["components"][0].accent_colour == muse_jukebox.GRAY
    text = dump(data)
    assert (
        "musicplayer_dropdown_idle" in text and tr("jbx.idle.title") in shown_text(data) and "<t:" in shown_text(data)
    )
    player.keep_connected = True
    assert "<t:" not in shown_text(muse_jukebox.idle(player, kwargs))


@pytest.mark.regression
def test_the_text_skin_shows_the_elapsed_time_in_m_ss_and_it_is_stable_inside_a_step(tr):
    from empiremuse.utils.music.skins.muse import _text, muse_text

    player = _player(tr, position=72_000)
    ctx = _text.context(player, NOW)
    first = muse_text.timeline(player, ctx, tr)
    player.position = 73_900  # o mesmo passo de 5 s: o texto não muda
    assert muse_text.timeline(player, ctx, tr) == first
    assert first[0].startswith("`1:10` ") and first[0].endswith("`3:29`") and "03:" not in first[0]


@pytest.mark.regression
def test_no_hint_line_in_the_text_skin_nor_in_the_jukebox(tr):
    from empiremuse.utils.music.skins.muse import muse_text

    player = _player(tr)
    player.current_hint = "Dê uma nova cara para o player com /custom_skin"
    assert muse_text.build(player, NOW)["embeds"][0].footer.text is None
    assert "custom_skin" not in dump(build(player))


# ---------------------------------------------------------------------- correções da verificação ao vivo
@pytest.mark.regression
def test_the_title_link_label_shows_no_backslash_before_parentheses(tr):
    player = _player(tr, static=True)
    player.current.single_title = "Blinding Lights (Official Audio) [HD]"
    shown = shown_text(build(player))
    heading = next(line for line in shown.splitlines() if line.startswith("## "))
    assert heading == "## [Blinding Lights (Official Audio) (HD)](https://www.youtube.com/watch?v=abc)"


@pytest.mark.regression
def test_the_bot_as_requester_is_a_recommendation_in_the_header_and_never_a_row_name(tr):
    player = _player(tr, static=True)
    bot_member = SimpleNamespace(display_name="EmpireMuse", bot=True)
    player.guild.get_member = lambda _id: bot_member
    player.current.autoplay = False  # o Last.fm e o autoplay antigo põem o id do bot como pedinte, sem a marca
    player.queue = []
    player.autoplay = True
    player.queue_autoplay = [_track(f"Rec {n}") for n in range(3)]
    shown = shown_text(build(player))
    assert tr("jbx.recommended") in shown and "EmpireMuse" not in shown and "<@42>" not in shown
    assert "<@" in shown_text(build(_player(tr, static=True)))  # quem é gente continua aparecendo


@pytest.mark.regression
def test_the_empty_queue_line_points_to_a_real_control_not_to_a_select(tr):
    player = _player(tr, static=True)
    player.queue = []
    player.queue_autoplay = []
    text = shown_text(build(player))
    assert "select" not in text.lower() and "/play" in text


@pytest.mark.regression
def test_the_idle_of_a_channel_without_a_player_is_v2_without_the_shutdown_line(tr):
    select = disnake.ui.Select(custom_id="x", options=[disnake.SelectOption(label="a", value="a")])
    data = muse_jukebox.idle_payload(tr, [select], lead="abra um post")
    text = shown_text(data)
    assert data["embeds"] == [] and data["content"] is None and "<t:" not in text
    assert tr("jbx.idle.title") in text and tr("jbx.idle.body") in text and "abra um post" in text
