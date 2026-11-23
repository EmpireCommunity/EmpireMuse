"""A ligação das skins de imagem na mensagem do player (``np_steps``): a chave estável do payload, a saída cedo, o cartão
no embed (imagem, autor, anexo com texto alternativo), o recuo para a skin de texto e a mensagem nova sem anexo antigo.
Pipeline e fatos de mentira; sem rede nem relógio."""

import io
from types import SimpleNamespace

import disnake
import pytest

from empiremuse.rules.card import CardFormat, RenderedKey
from empiremuse.utils.music.models import np_steps
from empiremuse.utils.render.cards import pipeline, slim
from empiremuse.utils.render.cards.pipeline import Rendered
from empiremuse.utils.render.state import RENDER_STATES
from tests.utils.test_render_cards import base_spec

pytestmark = pytest.mark.critical

TRACK_URL = "https://music.example/track"


def card_payload(label="Pular"):
    embed = disnake.Embed(description="-# termina <t:1:R>")
    embed.set_author(name="—", url=TRACK_URL, icon_url="https://music.example/icon.png")
    return {"content": None, "embeds": [embed], "components": [disnake.ui.Button(label=label, custom_id="skip")]}


class ImageSkin:
    name, theme, card = "muse_slim_black", "black", "slim"

    def __init__(self):
        self.label = "Pular"

    def project(self, spec):
        return slim.project(spec)

    def load(self, _player):
        return card_payload(self.label)


class TextSkin:
    def load(self, _player):
        return {"content": None, "embeds": [disnake.Embed(description="texto")], "components": []}


class Pipe:
    def __init__(self):
        self.calls = 0
        self.upgrade_due = False
        self.no_card = False
        self.keep = False
        self.error = None

    def supersede(self, player):
        pass

    async def render(self, player, card, spec, tr, *, station_logo=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.no_card:
            RENDER_STATES.replace(player, upgrade_due=True)
            return Rendered(None, busy=True)
        if self.keep:
            return Rendered(RENDER_STATES.get(player).last)
        key = RenderedKey(spec, None, CardFormat.PNG)
        RENDER_STATES.replace(player, last=key, upgrade_due=self.upgrade_due)
        return Rendered(key, b"\x89PNG-bytes")


class Player:
    def __init__(self, skin):
        self.static, self.skin, self.guild_id = False, skin.name, 1
        self.bot = SimpleNamespace(
            pool=SimpleNamespace(player_skins={skin.name: skin, "muse_text": TextSkin()}),
            config={"COVER_HOSTS": ()},
        )
        self.tr = lambda key, **_kw: {"jbx.card.alt_hidden": "Faixa oculta"}.get(key, key)
        self.last_data, self.message, self.update = {}, object(), False
        self.ignore_np_once = self.has_thread = False

    def is_last_message(self):
        return True


@pytest.fixture
def world(monkeypatch):
    skin, pipe, spec = ImageSkin(), Pipe(), {"value": base_spec(skin="muse_slim_black")}

    async def card_spec(_player, _skin, **_kw):
        return spec["value"]

    monkeypatch.setattr(np_steps, "_card_spec", card_spec)
    monkeypatch.setattr(pipeline, "card_pipeline", lambda _hosts: pipe)
    return SimpleNamespace(skin=skin, pipe=pipe, spec=spec, player=Player(skin))


def request():
    return SimpleNamespace(interaction=None, force=False, data=None)


async def run(player):
    req = request()
    return await np_steps._np_data(player, req), req


# ---------------------------------------------------------------------- chave estável


def test_two_equal_loads_have_the_same_key_and_a_changed_component_does_not():
    assert card_payload() != card_payload()  # o disnake compara botões por identidade
    assert np_steps.payload_key(card_payload()) == np_steps.payload_key(card_payload())
    assert np_steps.payload_key(card_payload("Voltar")) != np_steps.payload_key(card_payload())


@pytest.mark.asyncio
@pytest.mark.regression
async def test_a_text_skin_with_components_skips_an_edit_when_nothing_changed():
    skin = TextSkin()
    skin.name = "muse_text"
    skin.load = lambda _p: card_payload()
    player = Player(skin)
    player.bot.pool.player_skins = {"muse_text": skin}
    player.skin = "muse_text"

    first, _ = await run(player)
    again, _ = await run(player)

    assert (first, again) == (False, True)


# ---------------------------------------------------------------------- cartão no embed


@pytest.mark.asyncio
async def test_the_card_goes_in_the_first_embed_with_the_real_title_and_the_alt_text(world):
    stop, req = await run(world.player)

    embed, file = req.data["embeds"][0], req.data["file"]
    assert stop is False and embed.image.url == "attachment://card.png"
    assert (embed.author.name, embed.author.url) == ("Noite em Órbita", TRACK_URL)
    assert file.filename == "card.png" and file.description == "Noite em Órbita · Lua Violeta"
    assert req.data["attachments"] == []


@pytest.mark.asyncio
async def test_a_private_call_removes_the_author_and_says_only_hidden_track(world):
    world.spec["value"] = base_spec(skin="muse_slim_black", hidden=True, track=None, cover=None)

    _, req = await run(world.player)

    assert req.data["embeds"][0].author.name is None and req.data["file"].description == "Faixa oculta"


@pytest.mark.asyncio
async def test_nothing_changed_exits_early_without_drawing(world):
    await run(world.player)

    stop, _ = await run(world.player)

    assert stop is True and world.pipe.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["spec", "upgrade_due", "component"])
async def test_a_new_spec_a_pending_upgrade_or_a_new_component_redraws(world, change):
    world.pipe.upgrade_due = change == "upgrade_due"
    await run(world.player)
    if change == "spec":
        world.spec["value"] = base_spec(skin="muse_slim_black", volume=10)
    if change == "component":
        world.skin.label = "Voltar"

    stop, _ = await run(world.player)

    assert stop is False and world.pipe.calls == 2
    assert world.player.update is (change == "upgrade_due")


@pytest.mark.asyncio
async def test_without_any_card_the_text_skin_goes_out(world):
    world.pipe.no_card = True

    stop, req = await run(world.player)

    assert stop is False and req.data["embeds"][0].description == "texto" and "file" not in req.data


@pytest.mark.asyncio
async def test_a_new_message_never_reuses_the_attachment_of_the_old_one(world):
    await run(world.player)
    world.player.message = None  # a mensagem sumiu: a próxima é nova
    world.spec["value"] = base_spec(skin="muse_slim_black")

    stop, req = await run(world.player)

    assert stop is False and world.pipe.calls == 2 and "file" in req.data


# ---------------------------------------------------------------------- mensagem nova


@pytest.mark.regression
def test_a_new_message_after_a_failed_edit_sends_the_whole_card_again():
    data = card_payload()
    data["embeds"][0].set_image(url="attachment://card.gif")
    spent = disnake.File(io.BytesIO(b"GIF89a-card"), filename="card.gif", description="alt")
    spent.fp.read()  # o envio que falhou leu o buffer até o fim
    data.update(file=spent, attachments=[])

    np_steps._card_for_new_message(Player(ImageSkin()), data)

    fresh = data["file"]
    assert fresh.fp.read() == b"GIF89a-card" and (fresh.filename, fresh.description) == ("card.gif", "alt")
    assert data["embeds"][0].image.url == "attachment://card.gif"
    assert "attachments" not in np_steps._send_kwargs(data)


def test_a_kept_attachment_never_reaches_a_new_message():
    player = Player(ImageSkin())
    data = card_payload()
    data["embeds"][0].set_image(url="attachment://card.png")

    np_steps._card_for_new_message(player, data)

    assert data["embeds"][0].image.url is None and "file" not in data
    assert player.update is True and player.last_data == {}


def test_send_never_receives_attachments():
    assert "attachments" not in np_steps._send_kwargs({"attachments": [], "content": "x"})


# ---------------------------------------------------------------------- ocioso (despacho por skin)


def idle_kwargs():
    embed = disnake.Embed(description="Nada na fila. Desliga <t:9:R>")
    embed.set_thumbnail(url="https://cdn.example/avatar.png")
    return {"embed": embed, "content": None, "components": [disnake.ui.Button(label="x", custom_id="idle")]}


class ClassicSkin:
    name = "default"


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize(
    "kind", ["classic", "classic_after_card", "muse_text", "image", "image_without_card", "image_spec_raises"]
)
async def test_the_idle_message_follows_the_skin_and_keeps_the_old_skins_as_they_were(world, monkeypatch, kind):
    from empiremuse.rules.card import CardState

    world.spec["value"] = base_spec(skin="muse_slim_black", state=CardState.IDLE, track=None, cover=None, queue=())
    world.pipe.no_card = kind == "image_without_card"
    if kind == "image_spec_raises":

        async def broken_spec(_player, _skin, **_kw):
            raise ConnectionError("banco fora do ar")

        monkeypatch.setattr(np_steps, "_card_spec", broken_spec)
    if kind == "classic_after_card":
        RENDER_STATES.replace(world.player, last=RenderedKey(world.spec["value"], None, CardFormat.PNG))
    text = TextSkin()
    text.name = "muse_text"
    skin = {"classic": ClassicSkin(), "classic_after_card": ClassicSkin(), "muse_text": text}.get(kind, world.skin)
    world.player.skin = skin.name
    world.player.bot.pool.player_skins[skin.name] = skin
    kwargs = idle_kwargs()

    out = await np_steps.idle_payload(world.player, kwargs)

    if kind.startswith("classic"):
        assert out is kwargs and "embeds" not in out and world.pipe.calls == 0
        assert out.get("attachments") == ([] if kind == "classic_after_card" else None)
        assert RENDER_STATES.get(world.player).last is None
        return
    embed = out["embeds"][0]
    assert "embed" not in out and embed.description.startswith("Nada na fila") and out["components"]
    if kind == "image":
        assert embed.image.url == "attachment://card.png" and embed.thumbnail.url is None and "file" in out
    else:
        assert embed.author.name == "jbx.idle.title" and "file" not in out and out["attachments"] == []


# ---------------------------------------------------------------------- edição em thread que falhou


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_thread_retry_sends_the_whole_card_again(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    sent = []

    async def edit(**kwargs):
        sent.append(kwargs["file"].fp.read())
        if len(sent) == 1:
            raise disnake.HTTPException(MagicMock(status=500), "falhou")

    thread = MagicMock(spec=disnake.Thread)
    thread.id, thread.locked, thread.archived = 5, False, False
    monkeypatch.setattr(np_steps.asyncio, "sleep", AsyncMock())
    player = SimpleNamespace(
        message=SimpleNamespace(edit=edit),
        text_channel=thread,
        bot=SimpleNamespace(get_channel=lambda _id: thread),
        guild=SimpleNamespace(me=object()),
        allowed_mentions=None,
        start_message_updater_task=MagicMock(),
        updating=True,
        ignore_np_once=False,
    )
    data = card_payload()
    data.update(file=disnake.File(io.BytesIO(b"GIF89a-card"), filename="card.gif"), attachments=[])

    assert await np_steps._np_message_edit(player, SimpleNamespace(data=data, interaction=None)) is True
    assert sent == [b"GIF89a-card", b"GIF89a-card"]


# ---------------------------------------------------------------------- rodada de correções do S5


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("cause", ["busy", "private_busy", "render_raises", "spec_raises", "text_skin"])
async def test_every_edit_that_leaves_the_card_drops_the_old_attachment(world, monkeypatch, caplog, cause):
    await run(world.player)
    assert RENDER_STATES.get(world.player).last is not None
    if cause in ("busy", "private_busy"):
        world.pipe.no_card = True  # o pipeline devolve sem chave: vaga ocupada sem anexo, ou a call ficou privada
    if cause == "render_raises":
        world.pipe.error = RuntimeError("https://covers.example/x.jpg")
    if cause == "spec_raises":

        async def broken_spec(_player, _skin, **_kw):
            raise ConnectionError("banco fora do ar")

        monkeypatch.setattr(np_steps, "_card_spec", broken_spec)
    if cause == "text_skin":
        text = TextSkin()
        text.name = "muse_text"
        world.player.skin = "muse_text"
    world.spec["value"] = base_spec(skin="muse_slim_black", volume=10)

    with caplog.at_level("WARNING"):
        stop, req = await run(world.player)

    assert stop is False and req.data["attachments"] == [] and "file" not in req.data
    assert req.data["embeds"][0].description == "texto" and RENDER_STATES.get(world.player).last is None
    retry = cause in ("busy", "private_busy")
    assert world.player.update is retry and RENDER_STATES.get(world.player).upgrade_due is retry
    if cause.endswith("raises"):
        (record,) = [r for r in caplog.records if "Cartão" in r.getMessage()]
        assert record.exc_info is None and "muse_slim_black" in record.getMessage()
        assert "covers.example" not in record.getMessage()


@pytest.mark.asyncio
@pytest.mark.regression
async def test_an_old_skin_that_never_had_a_card_edits_as_before():
    skin = TextSkin()
    skin.name = "default"
    player = Player(skin)
    player.bot.pool.player_skins = {"default": skin}
    player.skin = "default"

    _, req = await run(player)

    assert "attachments" not in req.data


@pytest.mark.asyncio
@pytest.mark.regression
async def test_a_kept_attachment_edits_without_file_nor_attachments(world):
    await run(world.player)
    world.pipe.keep = True
    world.skin.label = "Voltar"  # o texto mudou, o cartão não

    stop, req = await run(world.player)

    assert stop is False and req.data["embeds"][0].image.url == "attachment://card.png"
    assert "file" not in req.data and "attachments" not in req.data


@pytest.mark.asyncio
@pytest.mark.regression
async def test_a_pending_redraw_that_keeps_the_same_card_does_not_edit_again(world):
    world.pipe.upgrade_due = True
    await run(world.player)
    world.pipe.keep = True
    world.player.update = False

    stop, _ = await run(world.player)

    assert stop is True and world.pipe.calls == 2 and world.player.update is True


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_panel_resend_never_keeps_an_old_attachment():
    edits = []

    class Message:
        async def edit(self, **kwargs):
            edits.append(kwargs)

    async def resend(_guild_id):
        return Message()

    player = Player(ImageSkin())
    player.message, player.static, player.allowed_mentions = None, True, None
    player.bot.pool.jukebox_panel = SimpleNamespace(resend=resend)

    async def cleanup():
        return None

    player.channel_cleanup = cleanup
    await np_steps._np_send_new(player, SimpleNamespace(data=TextSkin().load(player)))

    assert edits[0]["attachments"] == []


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_disc_motion_is_not_read_from_the_database_on_every_update(monkeypatch):
    from unittest.mock import AsyncMock

    player = Player(ImageSkin())
    player.bot.get_global_data = AsyncMock(return_value={"disc_motion": False})

    now = [100.0]
    monkeypatch.setattr(np_steps, "_clock", lambda: now[0])

    values = [await np_steps._disc_motion(player), await np_steps._disc_motion(player)]
    values.append(await np_steps._disc_motion(player, fresh=True))
    reads_before_expiry = player.bot.get_global_data.await_count
    now[0] += np_steps.DISC_MOTION_TTL_S
    values.append(await np_steps._disc_motion(player))

    assert values == [False] * 4 and (reads_before_expiry, player.bot.get_global_data.await_count) == (2, 3)


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("asked_again", [False, True])
async def test_a_redraw_asked_during_the_update_survives_the_updater_cycle(monkeypatch, asked_again):
    from empiremuse.utils.music.models import player_message

    class Stop(Exception):
        pass

    async def stop_loop(_seconds):
        raise Stop

    async def invoke_np():
        if asked_again:
            fake.update = True  # o cartão foi adiado dentro desta atualização

    fake = SimpleNamespace(text_channel=object(), controller_mode=True, auto_update=0, update=True, invoke_np=invoke_np)
    monkeypatch.setattr(player_message.asyncio, "sleep", stop_loop)
    updater = next(
        getattr(klass, "message_updater")
        for klass in vars(player_message).values()
        if isinstance(klass, type) and "message_updater" in vars(klass)
    )

    with pytest.raises(Stop):
        await updater(fake)

    assert fake.update is asked_again


# ---------------------------------------------------------------------- e2e (§10.7): a Expandida pelo fluxo do player


@pytest.mark.asyncio
@pytest.mark.regression
async def test_e2e_expanded_plays_as_gif_pauses_as_png_and_comes_back_as_gif(monkeypatch):
    from importlib import import_module
    from unittest.mock import AsyncMock

    from empiremuse.i18n import loader
    from empiremuse.rules.i18n import Translator
    from empiremuse.utils.music.skins.muse import muse_text
    from tests.utils.test_card_pipeline import Covers, Slot
    from tests.utils.test_muse_skins import _FakePlayer, _player
    from tests.utils.test_render_cards import RecordingBook, logo_png

    loaded = loader.load_catalogs(None)
    tr = Translator("pt-BR", loaded["pt-BR"].entries, loaded["en-US"].entries)
    skin = import_module("empiremuse.utils.music.skins.muse.muse_expanded_black").load()

    class E2EPlayer(_FakePlayer):
        __hash__ = object.__hash__  # o estado de render é guardado por referência fraca, como o player real

    player = _player(tr)
    player.__class__ = E2EPlayer
    player.bot.pool.player_skins = {skin.name: skin, "muse_text": muse_text.load()}
    player.bot.config["COVER_HOSTS"] = ()
    player.bot.get_global_data = AsyncMock(return_value={})  # disc_motion nunca escolhido = ligado
    player.static, player.skin, player.guild_id = False, skin.name, 1
    player.last_data, player.message, player.update, player.ignore_np_once = {}, object(), False, False
    player.is_last_message = lambda: True
    pipe = pipeline.CardPipeline(Covers(), slot=Slot(), book=RecordingBook(), clock=lambda: 100.0, bot_logo=logo_png())
    monkeypatch.setattr(pipeline, "card_pipeline", lambda _hosts: pipe)

    async def cycle():
        stop, req = await run(player)
        return None if stop else req.data.get("file")

    playing = await cycle()
    player.paused = True
    paused = await cycle()
    player.paused = False
    back = await cycle()

    assert (playing.filename, paused.filename, back.filename) == ("card.gif", "card.png", "card.gif")
    assert pipe.slot.runs == 3


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("had_card", [False, True])
async def test_closing_the_player_drops_a_card_that_was_on_the_message(had_card):
    from unittest.mock import AsyncMock

    from empiremuse.utils.music.models import state_steps

    player = Player(ImageSkin())
    player.lyric_embed, player.message = None, SimpleNamespace(edit=AsyncMock())
    player.get_skin = lambda disable=False: TextSkin().load(player)
    if had_card:
        RENDER_STATES.replace(player, last=RenderedKey(base_spec(), None, CardFormat.PNG))

    await state_steps._cu_close_controller_message(player)

    sent = player.message.edit.await_args.kwargs
    assert sent.get("attachments") == ([] if had_card else None)


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("message", ["same", "new"])
async def test_a_busy_slot_with_no_card_on_screen_does_not_repeat_the_same_text_edit(world, message):
    world.pipe.no_card = True
    if message == "new":  # o invoke_np vai mandar outra mensagem (forçado e a do player não é a última do canal)
        world.player.is_last_message = lambda: False

    async def run_once():
        req = SimpleNamespace(interaction=None, force=message == "new", data=None)
        return await np_steps._np_data(world.player, req)

    first = await run_once()
    world.player.update = False
    again = await run_once()

    assert (first, again) == (False, message == "same") and world.pipe.calls == 2 and world.player.update is True


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("newer_request", ["same_card_exits_early", "text_skin"])
async def test_a_request_that_does_not_draw_supersedes_a_drawing_still_in_the_slot(monkeypatch, newer_request):
    import asyncio

    from tests.utils.test_card_pipeline import Covers, DrawnThenWait
    from tests.utils.test_render_cards import RecordingBook, logo_png

    slot = DrawnThenWait()
    pipe = pipeline.CardPipeline(Covers(), slot=slot, book=RecordingBook(), clock=lambda: 100.0, bot_logo=logo_png())
    monkeypatch.setattr(pipeline, "card_pipeline", lambda _hosts: pipe)
    monkeypatch.setattr(pipeline, "_PIPELINE", pipe)
    playing = base_spec(skin="muse_slim_black", motion=False)
    spec = {"value": playing}

    async def card_spec(_player, _skin, **_kw):
        return spec["value"]

    monkeypatch.setattr(np_steps, "_card_spec", card_spec)
    player = Player(ImageSkin())
    await run(player)
    shown = RENDER_STATES.get(player).last
    slot.after = asyncio.Event()
    spec["value"] = base_spec(skin="muse_slim_black", motion=False, volume=10)  # A: pausar (desenha)
    older = asyncio.ensure_future(run(player))
    for _ in range(5):
        await asyncio.sleep(0)
    spec["value"] = playing  # B: retomar, igual ao que está na tela: sai cedo
    if newer_request == "text_skin":  # B: a skin mudou para uma de texto
        player.skin = "muse_text"

    newer, _ = await run(player)
    slot.after.set()
    old, _ = await older

    assert newer is (newer_request == "same_card_exits_early") and old is True and slot.runs == 2
    assert RENDER_STATES.get(player).last == (shown if newer_request == "same_card_exits_early" else None)
