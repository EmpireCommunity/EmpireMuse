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
        music=SimpleNamespace(players={GID: player} if player else {}),
        get_guild=lambda gid: guild if gid == GID else None,
        pool=SimpleNamespace(i18n=SimpleNamespace(for_guild=AsyncMock(return_value=lambda key, **k: key))),
    )


def pool_of(*bots):
    return SimpleNamespace(get_all_bots=lambda: list(bots))


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
        bot = make_bot(2, "TOKEN_RESERVE_2", call=100, radio=True)
        player = bot.music.players[GID]

        with pytest.raises(GenericError) as refusal:
            await checks.check_player_perm(inter_for(manage_channels=manage_channels), bot, None)

        assert "player" in str(refusal.value) and "locked" in str(
            refusal.value
        )  # a chave traduzida (o GenericError escapa o "_")
        player.destroy.assert_not_awaited()  # o player da rádio fica intacto

    @pytest.mark.asyncio
    async def test_a_normal_music_player_keeps_the_usual_rules(self):
        bot = make_bot(2, "TOKEN_RESERVE_2", call=100, radio=False)

        assert await checks.check_player_perm(inter_for(manage_channels=True), bot, None) is True


class TestJukeboxPathsSkipRadioPlayers:
    def test_a_typed_request_or_button_never_resolves_to_the_radios_player(self):
        radio_bot, music_bot = (
            make_bot(2, "TOKEN_RESERVE_2", call=100, radio=True),
            make_bot(3, "TOKEN_RESERVE_3", call=200),
        )
        pool = pool_of(radio_bot, music_bot)

        assert (
            checks.resolve_player(pool, GID, member(100)) is None
        )  # quem está na call da rádio não tem player de música
        assert checks.resolve_player(pool, GID, member(200))[0] is music_bot

    def test_the_request_goes_to_a_free_bot_never_to_the_radio_bot(self):
        radio_bot, free_bot = make_bot(2, "TOKEN_RESERVE_2", call=100, radio=True), make_bot(3, "TOKEN_RESERVE_3")
        pool = pool_of(radio_bot, free_bot)

        assert checks.pick_request_bot(pool, GID, member(100)) is free_bot
        assert checks.pick_request_bot(pool_of(radio_bot), GID, member(100)) is None  # só a rádio: todos ocupados

    def test_has_radio_player_only_for_a_radio_player(self):
        assert checks.has_radio_player(make_bot(2, "T", call=100, radio=True), GID)
        assert not checks.has_radio_player(make_bot(3, "T3", call=100), GID)
        assert not checks.has_radio_player(make_bot(4, "T4"), GID)


@pytest.mark.regression
@pytest.mark.parametrize("path", ["request", "available", "pool"])
@pytest.mark.asyncio
async def test_a_radio_token_never_enters_music_selection(path):
    from empiremuse.rules.bot_tokens import load_bot_tokens
    from tests.rules.test_bot_tokens import token

    identifier = load_bot_tokens({"TOKEN": token(), "TOKEN_RADIO_2": token("b")}).bots[1].identifier
    radio_bot = make_bot(2, identifier)
    radio_bot.user.mention = "<@2>"
    radio_bot.get_guild = MagicMock(wraps=radio_bot.get_guild)
    pool = pool_of(radio_bot)
    if path == "request":
        assert checks.pick_request_bot(pool, GID, member(100)) is None
    elif path == "available":
        assert checks.get_available_bots_info(pool, GID, member(100)) == ("", [])
    else:

        class NextCandidate(Exception):
            pass

        reserve = make_bot(3, "TOKEN_RESERVE_Z")
        reserve.get_guild = MagicMock(side_effect=NextCandidate)
        pool = pool_of(radio_bot, reserve)
        bot = SimpleNamespace(
            user=SimpleNamespace(id=1), pool=pool, identifier="TOKEN", music=SimpleNamespace(players={})
        )
        inter = SimpleNamespace(bot=bot, guild_id=GID, guild=SimpleNamespace(), author=member(100))
        inter.author.voice.channel.voice_states = {}
        with pytest.raises(NextCandidate):
            await checks.check_pool_bots(inter)
    if path != "request":
        radio_bot.get_guild.assert_not_called()


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("check", [checks.has_player, checks.has_source], ids=["has_player", "has_source"])
async def test_music_commands_without_the_dj_check_also_refuse_a_radio_player(check):
    """``/autoplay`` e o voto para pular não passam pelo ``check_player_perm``: quem está na call da rádio os alcançava pelo bot dela."""
    bot = make_bot(2, "TOKEN_RESERVE_2", call=100, radio=True)
    inter = SimpleNamespace(music_bot=bot, guild_id=GID)

    with pytest.raises(GenericError) as refusal:
        await check().predicate(inter)

    assert "player" in str(refusal.value) and "locked" in str(refusal.value)


def player_check(command):
    """O ``has_player``/``has_source`` de um comando real (os outros checks pedem voz e o banco)."""
    return next(c for c in command.checks if c.__qualname__.split(".")[0] in ("has_player", "has_source"))


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False], ids=["controller_allowed", "savequeue_refused"])
async def test_read_only_views_allow_a_radio_player_and_queue_commands_still_refuse(allowed):
    """Revisão do PR 255: o controller (vista de leitura) segue aberto num player de rádio; salvar a fila (o buffer interno da rádio, não
    uma fila de música do membro) segue recusado com a mensagem da rádio."""
    from empiremuse.music_cog.now_playing import NowPlayingMixin
    from empiremuse.music_cog.player_session import PlayerSessionMixin

    command = NowPlayingMixin.controller if allowed else PlayerSessionMixin.save_queue
    bot = make_bot(2, "TOKEN_RESERVE_2", call=100, radio=True)
    player = bot.music.players[GID]
    player.current, player.node = object(), SimpleNamespace(is_available=True)
    inter = SimpleNamespace(music_bot=bot, guild_id=GID)

    if allowed:
        assert await player_check(command)(inter) is True
    else:
        with pytest.raises(GenericError) as refusal:
            await player_check(command)(inter)
        assert "player" in str(refusal.value) and "locked" in str(refusal.value)


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("radio", [True, False], ids=["radio_player", "music_bot"])
async def test_the_shortcut_never_picks_a_bot_that_plays_a_radio_here(radio):
    """CodeRabbit #200: com o membro na call do próprio bot, o atalho devolvia ``inter.bot`` mesmo com o player de uma rádio. Agora o atalho só
    vale para quem toca música; o bot da rádio segue para o laço (que o pula e escolhe outro)."""

    class ReachedThePool(Exception):
        pass

    own = make_bot(2, "TOKEN_RESERVE_2", call=100, radio=radio)
    reserve = make_bot(3, "TOKEN_RESERVE_3")
    reserve.get_guild = MagicMock(side_effect=ReachedThePool)
    own.pool = pool_of(own, reserve)
    author = member(100)
    author.voice.channel.voice_states = {2: object()}  # o membro está na call deste bot
    inter = SimpleNamespace(bot=own, guild_id=GID, guild=SimpleNamespace(), author=author)

    if radio:
        with pytest.raises(ReachedThePool):
            await checks.check_pool_bots(inter, bypass_attribute=True)
    else:
        assert await checks.check_pool_bots(inter, bypass_attribute=True) == (own, inter.guild)
        reserve.get_guild.assert_not_called()


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [{"check_player": False}, {"check_player": True, "return_first": True}],
    ids=["no_check_player", "return_first"],
)
async def test_a_mention_prefix_without_voice_on_a_radio_bot_goes_to_the_pool(options):
    """CodeRabbit no PR 265: prefixo por menção, autor fora de voz e o bot atual com player de rádio. Antes: ``AttributeError`` em
    ``voice.channel`` (sem ``check_player``) ou ``NoVoice`` sem olhar os outros bots (com ``return_first``). Agora a escolha segue para o laço."""
    from empiremuse.utils.others import CustomContext

    class ReachedThePool(Exception):
        pass

    class MentionCtx(CustomContext):
        author = guild = channel = message = None  # sombra das propriedades do ``Context``: o teste monta tudo

        def __init__(self, **fields):
            self.__dict__.update(fields)

    own = make_bot(2, "TOKEN_RESERVE_2", call=100, radio=True)
    own.check_bot_forum_post = lambda *args, **kwargs: True
    reserve = make_bot(3, "TOKEN_RESERVE_3")
    reserve.get_guild = MagicMock(side_effect=ReachedThePool)
    own.pool = SimpleNamespace(get_all_bots=lambda: [own, reserve], bot_mentions=("<@2>",), message_ids={})
    inter = MentionCtx(
        bot=own,
        guild_id=GID,
        guild=own.get_guild(GID),
        author=member(None),
        channel=SimpleNamespace(id=9, owner_id=None),
        message=SimpleNamespace(content="<@2> play x", id=1),
    )

    with pytest.raises(ReachedThePool):
        await checks.check_pool_bots(inter, bypass_attribute=True, **options)
