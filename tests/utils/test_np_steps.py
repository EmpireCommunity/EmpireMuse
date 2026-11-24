"""A mensagem do player no ``np_steps``: a chave estável do payload, o fórum em call privada, o ocioso por skin, a mensagem
nova e o redesenho pedido. Skins de mentira (texto); sem rede nem relógio real."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.utils.music.models import np_steps, player_message

pytestmark = pytest.mark.critical

TRACK_URL = "https://music.example/track"


def payload(label="Pular"):
    embed = disnake.Embed(description="-# termina <t:1:R>")
    embed.set_author(name="—", url=TRACK_URL, icon_url="https://music.example/icon.png")
    return {"content": None, "embeds": [embed], "components": [disnake.ui.Button(label=label, custom_id="skip")]}


class TextSkin:
    def __init__(self, name="muse_text", load=None):
        self.name = name
        self._load = load

    def load(self, _player):
        if self._load is not None:
            return self._load()
        return {"content": None, "embeds": [disnake.Embed(description="texto")], "components": []}


class Player:
    def __init__(self, skin):
        self.static, self.skin, self.guild_id = False, skin.name, 1
        self.bot = SimpleNamespace(
            pool=SimpleNamespace(player_skins={skin.name: skin}, player_static_skins={skin.name: skin}),
            config={"COVER_HOSTS": ()},
        )
        self.tr = lambda key, **_kw: {"jbx.hidden_track": "Faixa oculta"}.get(key, key)
        self.last_data, self.message, self.update = {}, object(), False
        self.ignore_np_once = self.has_thread = False
        self.guild = call_guild(private=False)

    def is_last_message(self):
        return True


def call_guild(*, private: bool):
    """O bot numa call; privada = ``@everyone`` não vê o canal."""
    perms = SimpleNamespace(view_channel=not private, connect=True)
    channel = SimpleNamespace(permissions_for=lambda _role: perms, guild=SimpleNamespace(default_role=None))
    return SimpleNamespace(me=SimpleNamespace(voice=SimpleNamespace(channel=channel)))


async def run(player):
    req = SimpleNamespace(interaction=None, force=False, data=None)
    return await np_steps._np_data(player, req), req


def test_two_equal_loads_have_the_same_key_and_a_changed_component_does_not():
    assert payload() != payload()  # o disnake compara botões por identidade
    assert np_steps.payload_key(payload()) == np_steps.payload_key(payload())
    assert np_steps.payload_key(payload("Voltar")) != np_steps.payload_key(payload())


@pytest.mark.asyncio
@pytest.mark.regression
async def test_a_text_skin_with_components_skips_an_edit_when_nothing_changed():
    player = Player(TextSkin(load=payload))

    first, _ = await run(player)
    again, _ = await run(player)

    assert (first, again) == (False, True)


@pytest.mark.asyncio
@pytest.mark.regression
async def test_an_old_skin_edits_without_touching_the_attachments():
    player = Player(TextSkin("default"))

    _, req = await run(player)

    assert "attachments" not in req.data


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("skin_name", ["muse_text", "default"])
async def test_a_forum_static_player_never_puts_the_real_title_in_the_content_of_a_private_call(skin_name, private):
    player = Player(TextSkin(skin_name))
    player.bot.user = SimpleNamespace(
        id=1, display_avatar=SimpleNamespace(replace=lambda **_kw: SimpleNamespace(url="a"))
    )
    player.static, player.skin_static, player.paused = True, skin_name, False
    player.queue, player.queue_autoplay, player.command_log = [], [], ""
    player.current = SimpleNamespace(title="SEGREDO do fórum", duration=1000, is_stream=False)
    player.text_channel = SimpleNamespace(parent=MagicMock(spec=disnake.ForumChannel))
    player.guild = call_guild(private=private)

    _, req = await run(player)

    content = req.data.get("content") or ""
    assert ("SEGREDO" in content, "Faixa oculta" in content) == (not private, private)


# ---------------------------------------------------------------------- mensagem nova e edição que falhou


@pytest.mark.regression
def test_send_never_receives_attachments():
    assert "attachments" not in np_steps._send_kwargs({"attachments": [], "content": "x"})


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_thread_retry_sends_the_whole_files_again(monkeypatch):
    sent = []

    async def edit(**kwargs):
        sent.append(kwargs["files"][0].fp.read())
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
    data = payload()
    data.update(files=[disnake.File(io.BytesIO(b"GIF89a-disc"), filename="disc.gif")], attachments=[])

    assert await np_steps._np_message_edit(player, SimpleNamespace(data=data, interaction=None)) is True
    assert sent == [b"GIF89a-disc", b"GIF89a-disc"]


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_panel_resend_never_keeps_an_old_attachment():
    edits = []

    class Message:
        async def edit(self, **kwargs):
            edits.append(kwargs)

    async def resend(_guild_id):
        return Message()

    player = Player(TextSkin())
    player.message, player.static, player.allowed_mentions = None, True, None
    player.bot.pool.jukebox_panel = SimpleNamespace(resend=resend)
    player.channel_cleanup = AsyncMock()

    await np_steps._np_send_new(player, SimpleNamespace(data=TextSkin().load(player)))

    assert edits[0]["attachments"] == []


# ---------------------------------------------------------------------- ocioso (despacho por skin)


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("skin_name", ["default", "muse_text"])
async def test_the_idle_message_follows_the_skin_and_keeps_the_old_skins_as_they_were(skin_name):
    player = Player(TextSkin(skin_name))
    embed = disnake.Embed(description="Nada na fila. Desliga <t:9:R>")
    kwargs = {"embed": embed, "content": None, "components": [disnake.ui.Button(label="x", custom_id="idle")]}

    out = await np_steps.idle_payload(player, kwargs)

    if skin_name == "default":
        assert out is kwargs and "embeds" not in out and "attachments" not in out
        return
    assert "embed" not in out and out["embeds"] == [embed] and out["components"]
    assert embed.author.name == "jbx.idle.title" and out["attachments"] == []


# ---------------------------------------------------------------------- disco e atualizador


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_disc_motion_is_not_read_from_the_database_on_every_update(monkeypatch):
    player = Player(TextSkin())
    player.__class__ = type("WeakPlayer", (Player,), {})  # a leitura é guardada por referência fraca ao player
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
    class Stop(Exception):
        pass

    async def stop_loop(_seconds):
        raise Stop

    async def invoke_np():
        if asked_again:
            fake.update = True  # o redesenho foi pedido dentro desta atualização

    fake = SimpleNamespace(text_channel=object(), controller_mode=True, auto_update=0, update=True, invoke_np=invoke_np)
    monkeypatch.setattr(player_message.asyncio, "sleep", stop_loop)

    with pytest.raises(Stop):
        await player_message.PlayerMessageMixin.message_updater(fake)

    assert fake.update is asked_again
