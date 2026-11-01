"""Controller sem player (item 21, revisão): as mensagens de erro saem no idioma do servidor mesmo quando ainda não existe player."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.music_cog import controller_buttons, controller_embed
from empiremuse.utils.music.errors import GenericError

pytestmark = pytest.mark.critical

GID = 111


def guild():
    return SimpleNamespace(id=GID, preferred_locale="pt-BR")


def response(done=False):
    return SimpleNamespace(is_done=lambda: done, defer=AsyncMock())


def cog(*, bots=()):
    pool = SimpleNamespace(
        i18n=get_i18n(),
        get_guild_bots=lambda gid: list(bots),
        enqueue_playlist_embed_cooldown=SimpleNamespace(
            get_bucket=lambda inter: SimpleNamespace(update_rate_limit=lambda: 5)
        ),
        enqueue_track_embed_cooldown=SimpleNamespace(
            get_bucket=lambda inter: SimpleNamespace(update_rate_limit=lambda: 3)
        ),
    )
    bot = SimpleNamespace(
        pool=pool,
        music=SimpleNamespace(players={}),
        get_cog=lambda name: SimpleNamespace(player_controller=AsyncMock()),
    )
    return SimpleNamespace(
        bot=bot, player_interaction_concurrency=SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    )


@pytest.mark.asyncio
async def test_a_click_with_no_active_player_answers_in_the_server_language(monkeypatch):
    monkeypatch.setattr(controller_buttons, "_pc_route_panel_click", AsyncMock(return_value=False))
    idle = AsyncMock()
    monkeypatch.setattr(controller_buttons, "send_idle_embed", idle)
    interaction = SimpleNamespace(
        guild=guild(),
        guild_id=GID,
        message=SimpleNamespace(id=1),
        author=SimpleNamespace(id=7),
        send=AsyncMock(),
        response=response(),
    )
    req = SimpleNamespace(control="pause_resume", interaction=interaction)

    assert await controller_buttons._pc_player_checks(cog(), req) is True

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)  # confirmada antes de ler o idioma
    interaction.send.assert_awaited_once_with("Não há player ativo no servidor...", ephemeral=True)
    idle.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_member_outside_voice_is_told_so_when_no_bot_has_a_player_yet():
    member = SimpleNamespace(voice=None)
    channel = SimpleNamespace(guild=SimpleNamespace(get_member=lambda uid: member))
    bot = SimpleNamespace(identifier="TOKEN", music=SimpleNamespace(players={}), get_channel=lambda cid: channel)
    interaction = SimpleNamespace(
        guild=guild(),
        guild_id=GID,
        channel_id=5,
        response=response(),
        author=SimpleNamespace(id=7),
        message=SimpleNamespace(
            embeds=[SimpleNamespace(author=SimpleNamespace(url="https://youtu.be/x"), description="")]
        ),
    )
    req = SimpleNamespace(interaction=interaction)

    with pytest.raises(GenericError) as error:
        await controller_embed._pc_embed_resolve(cog(bots=[bot]), req)

    assert error.value.text == "Você deve entrar em um canal de voz pra usar esse botão...."


@pytest.mark.asyncio
async def test_no_bot_with_a_channel_gives_the_translated_error_not_an_attribute_error():
    interaction = SimpleNamespace(
        guild=guild(),
        guild_id=GID,
        channel_id=5,
        response=response(),
        author=SimpleNamespace(id=7),
        message=SimpleNamespace(
            embeds=[SimpleNamespace(author=SimpleNamespace(url="https://youtu.be/x"), description="")]
        ),
    )
    bot = SimpleNamespace(identifier="TOKEN", music=SimpleNamespace(players={}), get_channel=lambda cid: None)

    with pytest.raises(GenericError) as error:
        await controller_embed._pc_embed_resolve(cog(bots=[bot]), SimpleNamespace(interaction=interaction))

    assert error.value.text == "Não há bots disponíveis no momento."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function", "expected"),
    [
        (controller_embed._pc_embed_playlist, "pra adicionar uma playlist no player atual"),
        (controller_embed._pc_embed_track, "para adicionar uma nova música na fila"),
    ],
)
async def test_the_cooldown_errors_work_before_a_player_exists(function, expected):
    interaction = SimpleNamespace(guild=guild(), guild_id=GID, author=SimpleNamespace(id=7), response=response())
    req = SimpleNamespace(
        author=None,
        bot=None,
        channel=None,
        control="embed_enqueue_track",
        interaction=interaction,
        node=None,
        player=None,
        seek_status=None,
        track=None,
        url="https://youtu.be/x",
        vc_id=None,
    )

    with pytest.raises(GenericError) as error:
        await function(cog(), req)

    assert "Você terá que aguardar" in error.value.text and expected in error.value.text


@pytest.mark.asyncio
async def test_the_interaction_is_acknowledged_before_the_language_is_read_and_never_twice(monkeypatch):
    order = []
    i18n = get_i18n()

    async def reading(guild_):
        order.append("language")
        return i18n.for_locale("pt-BR")

    monkeypatch.setattr(i18n, "for_guild", reading)
    fresh = SimpleNamespace(
        guild=guild(),
        response=SimpleNamespace(
            is_done=lambda: False, defer=AsyncMock(side_effect=lambda **kw: order.append("defer"))
        ),
    )
    done = SimpleNamespace(guild=guild(), response=response(done=True))

    await i18n.ack_for_guild(fresh)
    await i18n.ack_for_guild(done)

    assert order == ["defer", "language", "language"]  # a 2ª já estava respondida: nada de ``defer`` de novo
    done.response.defer.assert_not_awaited()
    fresh.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_a_failing_acknowledgement_still_gives_the_translator(monkeypatch):
    interaction = SimpleNamespace(
        guild=guild(),
        response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock(side_effect=RuntimeError("expirada"))),
    )

    tr = await get_i18n().ack_for_guild(interaction)

    assert tr("player.there_is_no_active_player") == "Não há player ativo no servidor..."
