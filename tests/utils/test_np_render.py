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

    async def render(self, player, card, spec, tr, *, station_logo=None):
        self.calls += 1
        if self.no_card:
            return Rendered(None, busy=True)
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

    async def card_spec(_player, _skin):
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
    assert "attachments" not in data and data["embeds"][0].image.url == "attachment://card.gif"


def test_a_kept_attachment_never_reaches_a_new_message():
    player = Player(ImageSkin())
    data = card_payload()
    data["embeds"][0].set_image(url="attachment://card.png")

    np_steps._card_for_new_message(player, data)

    assert data["embeds"][0].image.url is None and "file" not in data
    assert player.update is True and player.last_data == {}


def test_send_never_receives_attachments():
    assert "attachments" not in np_steps._send_kwargs({"attachments": [], "content": "x"})
