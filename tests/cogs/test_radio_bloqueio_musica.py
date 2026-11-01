"""O player de uma rádio não é alcançável pelos caminhos de música (item 8, R1, V181-2): nem /play, /stop, DJ, pedido digitado ou botão da
Jukebox, nem para quem tem ``manage_channels``. Só o painel da rádio e o ``/radio`` mexem nele."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.utils.music import checks
from empiremuse.utils.music.errors import GenericError

pytestmark = pytest.mark.critical

GID = 500


def make_bot(bot_id, identifier, *, call=None, radio=False):
    channel = SimpleNamespace(id=call, name=f"call-{call}")
    guild = SimpleNamespace(
        id=GID, me=SimpleNamespace(voice=SimpleNamespace(channel=channel) if call else None), get_member=lambda _i: None
    )
    player = None
    if call:
        player = SimpleNamespace(guild=guild, guild_id=GID, keep_connected=False, is_closing=False, destroy=AsyncMock())
        if radio:
            player.radio_id = f"{bot_id}-{GID}"
    return SimpleNamespace(
        identifier=identifier,
        user=SimpleNamespace(id=bot_id),
        bot_ready=True,
        exclusive_guild_id=None,
        music=SimpleNamespace(players={GID: player} if player else {}),
        get_guild=lambda gid: guild if gid == GID else None,
        pool=SimpleNamespace(i18n=SimpleNamespace(for_guild=AsyncMock(return_value=lambda key, **k: key))),
    )


def pool_of(*bots):
    return SimpleNamespace(get_all_bots=lambda: list(bots), get_guild_bots=lambda gid: list(bots))


def member(call=None):
    voice = SimpleNamespace(channel=SimpleNamespace(id=call)) if call else None
    return SimpleNamespace(voice=voice, id=7)


def inter_for(*, manage_channels):
    guild = MagicMock(spec=disnake.Guild)
    guild.id = GID
    author = SimpleNamespace(id=7, guild_permissions=SimpleNamespace(manage_channels=manage_channels))
    return SimpleNamespace(guild_id=GID, guild=guild, author=author)


class TestPlayerPermRefusesARadioPlayer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("manage_channels", [True, False])
    async def test_nobody_not_even_manage_channels_commands_a_radio_player(self, manage_channels):
        bot = make_bot(2, "TOKEN_2", call=100, radio=True)
        player = bot.music.players[GID]

        with pytest.raises(GenericError) as refusal:
            await checks.check_player_perm(inter_for(manage_channels=manage_channels), bot, None)

        assert "player" in str(refusal.value) and "locked" in str(
            refusal.value
        )  # a chave traduzida (o GenericError escapa o "_")
        player.destroy.assert_not_awaited()  # o player da rádio fica intacto

    @pytest.mark.asyncio
    async def test_a_normal_music_player_keeps_the_usual_rules(self):
        bot = make_bot(2, "TOKEN_2", call=100, radio=False)

        assert await checks.check_player_perm(inter_for(manage_channels=True), bot, None) is True


class TestJukeboxPathsSkipRadioPlayers:
    def test_a_typed_request_or_button_never_resolves_to_the_radios_player(self):
        radio_bot, music_bot = make_bot(2, "TOKEN_2", call=100, radio=True), make_bot(3, "TOKEN_3", call=200)
        pool = pool_of(radio_bot, music_bot)

        assert (
            checks.resolve_player(pool, GID, member(100)) is None
        )  # quem está na call da rádio não tem player de música
        assert checks.resolve_player(pool, GID, member(200))[0] is music_bot

    def test_the_request_goes_to_a_free_bot_never_to_the_radio_bot(self):
        radio_bot, free_bot = make_bot(2, "TOKEN_2", call=100, radio=True), make_bot(3, "TOKEN_3")
        pool = pool_of(radio_bot, free_bot)

        assert checks.pick_request_bot(pool, GID, member(100)) is free_bot
        assert checks.pick_request_bot(pool_of(radio_bot), GID, member(100)) is None  # só a rádio: todos ocupados

    def test_has_radio_player_only_for_a_radio_player(self):
        assert checks.has_radio_player(make_bot(2, "T", call=100, radio=True), GID)
        assert not checks.has_radio_player(make_bot(3, "T3", call=100), GID)
        assert not checks.has_radio_player(make_bot(4, "T4"), GID)
