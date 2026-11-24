"""A skin Jukebox em Components V2 (``muse_jukebox``) e o passo do disco (``np_v2``): payload V2 puro, call privada sem nada da faixa
em nenhum campo do JSON, limites do Discord no pior caso, texto estável entre leituras, anexos só quando o disco muda e a mensagem V2
que nunca recebe embed. Fakes do player com o ``Translator`` real; disco e vaga de mentira; sem rede nem relógio."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import disnake
import pytest

from empiremuse.i18n import loader
from empiremuse.rules.card import CardState
from empiremuse.rules.i18n import Translator
from empiremuse.utils.music import v2
from empiremuse.utils.music.models import np_steps, np_v2
from empiremuse.utils.music.skins.muse import muse_jukebox
from empiremuse.utils.others import PlayerControls
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
    for custom_id in ("musicplayer_dropdown_inter", "musicplayer_queue_dropdown", PlayerControls.pause_resume):
        assert custom_id in text


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
    player = WeakNamespace()
    req = SimpleNamespace(disc="disc-1", published=False, v2=True, interaction=None)
    np_v2.confirm(player, req)
    assert np_v2.sent_name(player) == np_v2.UNKNOWN  # incerto: a próxima edição limpa o anexo e reenvia o disco
    req.published = True
    np_v2.confirm(player, req)
    assert np_v2.sent_name(player) == "disc-1"


# ---------------------------------------------------------------------- V2 fica V2 nas edições do invoke_np


class WeakNamespace(SimpleNamespace):
    """Um ``SimpleNamespace`` que aceita referência fraca (os registros por player são ``WeakKeyDictionary``)."""

    __slots__ = ("__weakref__",)
    __hash__ = object.__hash__  # o ``SimpleNamespace`` compara por valor e por isso não é chave de dicionário


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


@pytest.mark.parametrize(
    ("is_v2", "payload", "expected"),
    [(True, "embed", True), (True, "v2", False), (False, "embed", False), (False, "v2", False)],
    ids=["v2+embed", "v2+v2", "v1+embed", "v1+v2"],
)
def test_only_a_v2_message_with_an_embed_payload_needs_a_new_message(tr, is_v2, payload, expected):
    data = (
        {"embeds": [disnake.Embed(description="x")], "components": []}
        if payload == "embed"
        else {"embeds": [], "components": [build(_player(tr))["components"][0]]}
    )
    player = SimpleNamespace(message=fake_message(is_v2=is_v2, edits=[]))
    assert np_steps.needs_replacement(player, SimpleNamespace(interaction=None, data=data)) is expected


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
    player = WeakNamespace(message=message)
    req = SimpleNamespace(published=True, v2=True, interaction=None, disc=None)
    np_v2.confirm(player, req)
    assert v2.is_v2(message) is True
    other = fake_message(is_v2=False, edits=[])
    np_v2.confirm(WeakNamespace(message=other), SimpleNamespace(published=False, v2=True, interaction=None, disc=None))
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


@pytest.mark.regression
def test_a_queue_row_fits_one_line_and_drops_the_artist_the_title_already_has(tr):
    player = _player(tr, static=True)
    long = _track("Justin Timberlake - CAN'T STOP THE FEELING! (from DreamWorks Animation's TROLLS)")
    long.authors_string = "Justin Timberlake"
    other = _track("Sia - Cheap Thrills (Performance Edit)")
    other.authors_string = "Maroon Five"
    row = muse_jukebox.queue_row(player, 1, long, NOW + 25)
    assert "· Justin Timberlake" not in row and row.endswith(f"· <t:{muse_jukebox.tx.stamp(NOW + 25)}:R>")
    visible = row.replace("\\", "")
    visible = visible.replace(f"<t:{muse_jukebox.tx.stamp(NOW + 25)}:R>", "x" * muse_jukebox.TIME_WIDTH)
    assert len(visible.replace("**", "")) <= muse_jukebox.ROW_WIDTH + 4
    assert "· Maroon Five" in muse_jukebox.queue_row(player, 2, other, None)  # artista diferente do título: fica


@pytest.mark.regression
def test_a_private_call_has_no_timeline_and_the_idle_options_carry_no_track_title(tr):
    from empiremuse.utils.others import PlayerControls

    player = secret_player(tr, private=True, in_call_chat=False, static=True)
    assert "3:29" not in shown_text(build(player))  # nem a duração da faixa
    player.keep_connected = True
    options = [
        disnake.SelectOption(label="v", value=PlayerControls.back, description="Faixa Secreta"),
        disnake.SelectOption(label="r", value=PlayerControls.autoplay, description="Faixa Secreta"),
    ]
    select = disnake.ui.Select(custom_id="musicplayer_dropdown_idle", options=options)
    data = muse_jukebox.idle(player, {"components": [select], "content": "post do fórum"})
    assert "Faixa Secreta" not in dump(data) and "post do fórum" in shown_text(data)


@pytest.mark.regression
def test_a_huge_url_never_eats_the_text_limit_and_short_links_stay(tr):
    player = _player(tr, static=True)
    player.current.uri = "https://www.youtube.com/watch?v=" + "a" * 4000
    data = build(player)
    assert total_text(data) <= v2.MAX_TEXT and "](" not in shown_text(data).split("\n")[1]
    assert "](https://www.youtube.com/watch?v=abc)" in shown_text(build(_player(tr, static=True)))


@pytest.mark.regression
def test_closing_disables_every_button_and_select_inside_the_container(tr):
    (tree,) = build(_player(tr, static=True))["components"]
    v2.disable_all(tree)

    def interactive(node):
        out = []
        for child in getattr(node, "children", None) or ():
            out += interactive(child)
        if isinstance(node, (disnake.ui.Button, disnake.ui.BaseSelect)):
            out.append(node)
        return out

    found = interactive(tree)
    assert found and all(item.disabled for item in found)


@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_track_changing_while_the_disc_is_drawn_never_publishes_the_new_text_with_the_old_disc(
    tr, monkeypatch
):
    player = WeakNamespace(
        tr=tr, bot=SimpleNamespace(config={"COVER_HOSTS": ""}), last_data={}, update=False, message=None, static=True
    )
    seen = iter(["a", "b"])  # o spec antes do disco e o de depois são diferentes
    monkeypatch.setattr(np_v2.card_facts, "build", lambda *_: next(seen))
    monkeypatch.setattr(np_v2.card_facts, "CardSettings", lambda **_: None)

    async def pick(*_a, **_k):
        return np_v2.Picked(None, [], None, False)

    monkeypatch.setattr(np_v2, "pick", pick)

    async def motion(*_a, **_k):
        return True

    async def defer(_i):
        return None

    skin = SimpleNamespace(name="muse_jukebox", build=lambda *a: pytest.fail("não monta com o estado novo"))
    req = SimpleNamespace(interaction=None)
    done = await np_v2.render(
        player,
        req,
        skin,
        redraw=False,
        new_message=False,
        key_of=str,
        defer=defer,
        disc_motion=motion,
        station_logo=None,
    )
    assert done is True and player.update is True


# ---------------------------------------------------------------------- correções da revisão (PR 289)
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_busy_slot_never_shows_a_cover_the_cover_service_did_not_return(tr, service):
    service.service = Service(Disc("disc-x", None, busy=True, plain=True))  # capa recusada ou ainda sem pixels
    picked = await np_v2.pick(_player(tr), spec(), None, (), None)
    assert COVER not in picked.thumb.media and picked.files == []


@pytest.mark.regression
def test_leaving_a_public_disc_for_a_state_without_one_drops_the_old_attachment_and_the_record(tr):
    player = WeakNamespace()
    np_v2._SENT[player] = "disc-old"
    req = SimpleNamespace(disc=None, published=True, v2=True, interaction=None)
    np_v2.confirm(player, req)
    assert np_v2.sent_name(player) is None


def _render_player(tr, *, hidden):
    player = WeakNamespace(tr=tr, bot=SimpleNamespace(config={"COVER_HOSTS": ""}), last_data={}, update=False)
    return player


async def _render(player, skin, monkeypatch, *, hidden, spec_values, pick_fn=None):
    values = iter(spec_values)

    def build(*_):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(np_v2.card_facts, "build", build)
    monkeypatch.setattr(np_v2.card_facts, "CardSettings", lambda **_: None)
    monkeypatch.setattr(np_v2, "_hides_details", lambda _p: hidden)

    async def pick(*_a, **_k):
        return np_v2.Picked(muse_jukebox.Thumb("attachment://disc-n.png", "alt"), [], "disc-n", False)

    monkeypatch.setattr(np_v2, "pick", pick_fn or pick)

    async def motion(*_a, **_k):
        return True

    async def defer(_i):
        return None

    req = SimpleNamespace(interaction=None, superseded=False, published=False, disc=None, disc_key=None, v2=False)
    done = await np_v2.render(
        player,
        req,
        skin,
        redraw=False,
        new_message=False,
        key_of=str,
        defer=defer,
        disc_motion=motion,
        station_logo=None,
    )
    return done, req


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "stale"])
async def test_a_render_that_publishes_nothing_is_marked_superseded_so_privacy_is_not_recorded(
    tr, monkeypatch, failure
):
    player = _render_player(tr, hidden=False)
    values = [RuntimeError("x")] if failure == "raises" else ["a", "b"]
    skin = SimpleNamespace(name="muse_jukebox", build=lambda *a: pytest.fail("não monta"), idle=None)
    done, req = await _render(player, skin, monkeypatch, hidden=False, spec_values=values)
    assert done is True and req.superseded is True and player.update is True


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("second_build_fails", [False, True], ids=["fallback-thumb", "idle"])
async def test_a_build_that_fails_while_details_are_hidden_still_publishes_a_payload_without_secrets(
    tr, monkeypatch, second_build_fails
):
    player = _player(tr, private=True, in_call_chat=False, static=True)
    secret = secret_player(tr, private=True, in_call_chat=False, static=True)
    player = WeakNamespace(**secret.__dict__)
    player.bot.config["COVER_HOSTS"] = ""
    player.keep_connected = True
    player.update = False
    player.last_data = {}
    calls = []

    def build(plyr, now, thumb):
        calls.append(thumb)
        if second_build_fails:
            raise RuntimeError("de novo")
        return muse_jukebox.build(plyr, now, thumb)

    skin = SimpleNamespace(name="muse_jukebox", build=build, idle=lambda plyr, kw: muse_jukebox.idle(plyr, kw))
    done, req = await _render(player, skin, monkeypatch, hidden=True, spec_values=[RuntimeError("disco")])
    assert done is False and req.superseded is False and req.v2 is True
    assert "Faixa Secreta" not in dump(req.data) and "Artista Secreto" not in dump(req.data)
    assert player.update is True  # o updater tenta o desenho normal depois


@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_state_without_a_disc_sends_no_attachments_to_drop_the_old_one(tr, monkeypatch):
    player = _render_player(tr, hidden=True)
    np_v2._SENT[player] = "disc-old"
    skin = SimpleNamespace(name="muse_jukebox", build=lambda *a: {"components": ["x"]}, idle=None)

    async def no_disc(*_a, **_k):  # o estado novo termina sem imagem anexada (R escondido / plano B)
        return np_v2.Picked(muse_jukebox.Thumb("https://exemplo.invalid/global.png", "alt"), [], None, False)

    done, req = await _render(player, skin, monkeypatch, hidden=True, spec_values=["s", "s"], pick_fn=no_disc)
    assert done is False and req.data["attachments"] == [] and req.disc is None and "files" not in req.data


@pytest.mark.regression
def test_the_disc_cache_is_keyed_by_the_full_digest_not_by_the_short_attachment_name():
    from empiremuse.utils.render import disc_only

    digest_a, digest_b = disc_only.label_digest(b"a"), disc_only.label_digest(b"b")
    assert len(disc_only.disc_key(digest_a)) == 64 and disc_only.disc_key(digest_a) != disc_only.disc_key(digest_b)
    assert disc_only.disc_name(digest_a) == f"disc-{disc_only.disc_key(digest_a)[:8]}"
    cache = disc_only.DiscCache()
    cache.put(disc_only.disc_key(digest_a), IMAGES)
    assert cache.get(disc_only.disc_key(digest_a)) is IMAGES and cache.get(disc_only.disc_name(digest_a)) is None


@pytest.mark.regression
@pytest.mark.parametrize("expected", [True, False])
def test_the_adapter_warns_only_for_the_path_that_forgot_the_v2_payload(caplog, expected):
    message = fake_message(is_v2=True, edits=[])
    with caplog.at_level("WARNING"):
        data = v2.adapt_for(message, {"embed": disnake.Embed(description="x")}, where="x", expected=expected)
    assert data["embeds"] == [] and bool(caplog.records) is (not expected)


@pytest.mark.regression
@pytest.mark.parametrize(("has_thread", "expected"), [(False, True), (True, False)], ids=["channel", "thread-or-forum"])
def test_a_swap_to_v1_never_deletes_the_parent_message_of_a_thread(tr, has_thread, expected):
    player = SimpleNamespace(message=fake_message(is_v2=True, edits=[]), has_thread=has_thread)
    req = SimpleNamespace(interaction=None, data={"embeds": [disnake.Embed(description="x")], "components": []})
    assert np_steps.needs_replacement(player, req) is expected


@pytest.mark.regression
def test_the_title_label_has_no_markdown_and_a_legacy_author_is_not_turned_into_markdown(tr):
    player = _player(tr, static=True)
    player.current.single_title = "**b** ||sp|| `c` _i_ ~~s~~"
    heading = next(line for line in shown_text(build(player)).splitlines() if line.startswith("## "))
    label = heading[4 : heading.index("](")]
    assert not any(mark in label for mark in "*_~|`\\")
    embed = disnake.Embed(description="x")
    embed.set_author(name="[x](https://exemplo.invalid)")
    data = v2.legacy_to_v2({"embed": embed})
    assert "](" not in shown_text(data)


# ---------------------------------------------------------------------- segunda rodada da revisão (objetos reais)
@pytest.mark.regression
@pytest.mark.asyncio
async def test_when_the_privacy_rule_itself_raises_the_last_resort_still_publishes_a_payload_without_secrets(
    tr, monkeypatch
):
    import empiremuse.utils.jukebox_panel as panel
    from empiremuse.utils.music.skins.muse import _text

    secret = secret_player(tr, private=True, in_call_chat=False, static=True)
    player = WeakNamespace(**secret.__dict__)
    player.bot.config["COVER_HOSTS"] = ""
    player.keep_connected = False
    player.update, player.last_data = False, {}

    def broken(*_a, **_k):
        raise RuntimeError("regra quebrada")

    for module in (panel, _text):
        monkeypatch.setattr(module, "player_hides_details", broken)
    monkeypatch.setattr(np_v2.card_facts, "build", broken)

    async def motion(*_a, **_k):
        return True

    async def defer(_i):
        return None

    req = SimpleNamespace(interaction=None, superseded=False, published=False, disc=None, disc_key=None, v2=False)
    done = await np_v2.render(
        player,
        req,
        muse_jukebox.load(),
        redraw=False,
        new_message=False,
        key_of=str,
        defer=defer,
        disc_motion=motion,
        station_logo=None,
    )
    assert done is False and req.v2 is True and req.superseded is False
    text = dump(req.data)
    assert "Faixa Secreta" not in text and "Artista Secreto" not in text and "Call Secreta" not in text
    assert "<t:" not in shown_text(req.data)  # sem a linha falsa de "desliga" enquanto toca


@pytest.mark.regression
@pytest.mark.asyncio
async def test_after_a_failed_edit_the_retry_clears_the_old_attachment_and_resends_the_disc(tr, monkeypatch):
    player = _render_player(tr, hidden=True)
    np_v2._SENT[player] = "disc-public"
    failed = SimpleNamespace(disc=None, published=False, v2=True, interaction=None)
    np_v2.confirm(player, failed)  # a primeira edição (ex.: limite de taxa) não saiu
    skin = SimpleNamespace(name="muse_jukebox", build=lambda *a: {"components": ["x"]}, idle=None)

    async def same_disc(
        _player, _spec, _logo, _hosts, shown
    ):  # como o real: sem arquivo só se a mensagem já tem esse disco
        files = [] if shown == "disc-same" else [np_v2._file(b"png", "disc-same.png", "alt")]
        return np_v2.Picked(muse_jukebox.Thumb("attachment://disc-same.png", "alt"), files, "disc-same", False)

    done, req = await _render(player, skin, monkeypatch, hidden=True, spec_values=["s", "s"], pick_fn=same_disc)
    assert done is False and req.data["attachments"] == []
    assert [file.filename for file in req.data["files"]] == ["disc-same.png"]  # o disco é reenviado


@pytest.mark.regression
def test_the_idle_never_changes_the_options_the_caller_passed(tr):
    from empiremuse.utils.others import PlayerControls

    player = secret_player(tr, private=True, in_call_chat=False, static=True)
    player.keep_connected = True
    option = disnake.SelectOption(label="v", value=PlayerControls.back, description="Faixa Secreta")
    select = disnake.ui.Select(custom_id="musicplayer_dropdown_idle", options=[option])
    data = muse_jukebox.idle(player, {"components": [select]})
    assert option.description == "Faixa Secreta" and "Faixa Secreta" not in dump(data)


@pytest.mark.regression
def test_a_resumed_custom_skin_takes_only_the_current_format_definition_from_the_global():
    from empiremuse.cogs.player_resume import _custom_definition

    choice = "> custom_skin: X"
    assert _custom_definition({}, {"X": "texto antigo"}, choice) == {}
    assert _custom_definition({}, {"X": {"a": 1}}, choice) == {"X": {"a": 1}}


@pytest.mark.regression
@pytest.mark.parametrize("where", ["thread", "forum-post", "plain-channel"])
def test_a_swap_to_v1_never_deletes_a_message_inside_a_thread_or_forum_post(where):
    channel = (
        MagicMock(spec=disnake.TextChannel)
        if where == "plain-channel"
        else MagicMock(
            spec=disnake.Thread,
            parent=MagicMock(spec=disnake.ForumChannel if where == "forum-post" else disnake.TextChannel),
        )
    )
    message = fake_message(is_v2=True, edits=[])
    message.channel = channel
    player = SimpleNamespace(message=message, has_thread=None)  # num post de fórum o player devolve None
    req = SimpleNamespace(interaction=None, data={"embeds": [disnake.Embed(description="x")], "components": []})
    assert np_steps.needs_replacement(player, req) is (where == "plain-channel")


@pytest.mark.regression
def test_the_logo_bytes_are_forgotten_with_the_message():
    player = WeakNamespace()
    np_v2._LOGOS[player] = ("logo-1", b"png")
    np_v2.forget(player)
    assert player not in np_v2._LOGOS
