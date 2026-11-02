"""O bot só-rádio (item 8, R1): não atende prefixo, pedido nem menção (um aviso por canal por minuto) e nunca vira bot do painel da Jukebox."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.notice import refuse_radio_only
from empiremuse.utils.server_jukebox import ServerJukebox
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical


def build(*, radio_only=True):
    pool = SimpleNamespace(database=FakeDb(), i18n=get_i18n())
    pool.radio = RadioHub(pool)
    if radio_only:
        pool.radio.roles._radio_only = frozenset({"TOKEN_3"})
    channel = SimpleNamespace(id=55, send=AsyncMock())
    message = SimpleNamespace(channel=channel, guild=SimpleNamespace(id=1, preferred_locale="pt-BR"))
    bot = SimpleNamespace(pool=pool, identifier="TOKEN_3")
    return SimpleNamespace(bot=bot, pool=pool, message=message, channel=channel)


class TestNotice:
    @pytest.mark.asyncio
    async def test_a_prefix_command_is_refused_with_one_notice(self):
        w = build()

        handled = await refuse_radio_only(w.bot, SimpleNamespace(valid=True), w.message)

        assert handled is True
        w.channel.send.assert_awaited_once()
        assert "dedicado a uma rádio" in w.channel.send.await_args.args[0]
        assert w.channel.send.await_args.kwargs["allowed_mentions"].everyone is False
        assert w.channel.send.await_args.kwargs["delete_after"] == 15

    @staticmethod
    def other_bot(*, in_guild=True, can_send=True, thread_parent=None, identifier="TOKEN", ready=True):
        """Outro bot do pool: pronto, dentro (ou fora) do servidor, com (ou sem) permissão de escrever no canal ou na thread."""
        from unittest.mock import MagicMock

        import disnake

        me = SimpleNamespace(id=2)
        if thread_parent is not None:
            channel = MagicMock(spec=disnake.Thread)
            channel.parent = MagicMock()
            channel.parent.permissions_for.return_value = SimpleNamespace(send_messages_in_threads=thread_parent)
        else:
            channel = SimpleNamespace(permissions_for=lambda who: SimpleNamespace(send_messages=can_send))
        guild = SimpleNamespace(me=me, get_channel_or_thread=lambda cid: channel)
        return SimpleNamespace(
            identifier=identifier, bot_ready=ready, get_guild=lambda gid: guild if in_guild else None
        )

    @pytest.mark.asyncio
    async def test_no_notice_when_another_ready_bot_will_run_the_shared_prefix_command(self):  # CodeRabbit #180
        w = build()
        other = self.other_bot()
        w.pool.get_guild_bots = lambda gid: [w.bot, other]

        handled = await refuse_radio_only(w.bot, SimpleNamespace(valid=True), w.message)

        assert handled is True  # ele continua não atendendo
        w.channel.send.assert_not_awaited()  # mas não avisa à toa: o outro bot executa

    @pytest.mark.asyncio
    async def test_the_notice_stays_when_the_only_other_bots_are_radio_only_or_not_ready(self):
        w = build()
        w.pool.radio.roles._radio_only = frozenset({"TOKEN_3", "TOKEN_4"})
        w.pool.get_guild_bots = lambda gid: [
            w.bot,
            self.other_bot(identifier="TOKEN_4"),  # também só-rádio
            self.other_bot(ready=False),  # ainda subindo
        ]

        await refuse_radio_only(w.bot, SimpleNamespace(valid=True), w.message)

        w.channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "other",
        [
            dict(in_guild=False),  # pronto, mas não está neste servidor
            dict(can_send=False),  # no servidor, sem permissão de escrever neste canal
            dict(thread_parent=False),  # numa thread, sem send_messages_in_threads no canal pai
        ],
        ids=["absent_from_the_server", "cannot_send_in_the_channel", "cannot_send_in_the_thread"],
    )
    async def test_the_notice_stays_when_the_other_bot_cannot_actually_answer(self, other):  # CodeRabbit #180
        w = build()
        w.pool.get_guild_bots = lambda gid: [w.bot, self.other_bot(**other)]

        await refuse_radio_only(w.bot, SimpleNamespace(valid=True), w.message)

        w.channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_in_a_thread_the_notice_is_suppressed_when_the_other_bot_can_send_in_threads(self):
        w = build()
        w.pool.get_guild_bots = lambda gid: [w.bot, self.other_bot(thread_parent=True)]

        await refuse_radio_only(w.bot, SimpleNamespace(valid=True), w.message)

        w.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_one_notice_per_channel_per_window(self):
        w = build()
        for _ in range(5):
            assert await refuse_radio_only(w.bot, SimpleNamespace(valid=True), w.message) is True

        assert w.channel.send.await_count == 1

    @pytest.mark.asyncio
    async def test_a_plain_message_or_a_mention_is_ignored_without_a_notice(self):
        w = build()

        assert await refuse_radio_only(w.bot, SimpleNamespace(valid=False), w.message) is True

        w.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_bots_are_not_touched(self):
        w = build(radio_only=False)

        assert await refuse_radio_only(w.bot, SimpleNamespace(valid=True), w.message) is False
        w.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_send_does_not_break_the_handler(self, caplog):
        w = build()
        w.channel.send.side_effect = RuntimeError("sem permissão com token secreto")

        with caplog.at_level("WARNING"):
            assert await refuse_radio_only(w.bot, SimpleNamespace(valid=True), w.message) is True

        assert "RuntimeError" in caplog.text and "secreto" not in caplog.text


class TestJukeboxPanelElection:
    def test_a_radio_only_bot_never_becomes_the_jukebox_panel_bot(self):
        pool = SimpleNamespace(database=FakeDb(), i18n=get_i18n())
        pool.radio = RadioHub(pool)
        pool.radio.roles._radio_only = frozenset({"TOKEN_2"})

        def bot(bot_id, identifier):
            return SimpleNamespace(
                identifier=identifier,
                user=SimpleNamespace(id=bot_id),
                exclusive_guild_id=None,
                get_guild=lambda gid: SimpleNamespace(id=gid),
            )

        pool.get_all_bots = lambda: [bot(2, "TOKEN_2"), bot(3, "TOKEN_3")]
        request = ServerJukebox(SimpleNamespace(pool=pool))

        assert [peer.identifier for peer in request.peers(1)] == ["TOKEN_3"]
        assert request.elected_id(1) == 3  # sem o principal, a primeira reserva que NÃO é só-rádio


class TestPlainMention:
    """V181/CodeRabbit: o bot só-rádio não responde nem à menção pura (o embed de boas-vindas é de um bot de música)."""

    def self_and_message(self, *, radio_only):
        from unittest.mock import MagicMock

        from empiremuse.utils.client import BotCore

        pool = SimpleNamespace(database=FakeDb(), i18n=get_i18n())
        pool.radio = RadioHub(pool)
        pool.radio.roles._radio_only = frozenset({"TOKEN_3"} if radio_only else set())
        bot = MagicMock()
        bot.bot_ready, bot.appinfo, bot.exclusive_guild_id = True, object(), None
        bot.is_closed = lambda: False
        bot.music.players = {}
        bot.user = SimpleNamespace(id=99, mention="<@99>")
        bot.identifier = "TOKEN_3"
        bot.pool = pool
        bot.config = {"INTERACTION_COMMAND_ONLY": True, "INVITE_PERMISSIONS": 8, "ENABLE_SONGREQUEST_MENTION": False}
        bot.is_reserve = False
        bot.get_color = lambda me: 0
        bot.can_send_message = AsyncMock(return_value=True)
        bot.get_prefix = AsyncMock(return_value="!")
        bot.get_context = AsyncMock(return_value=SimpleNamespace(valid=False))
        bot._is_radio_only = lambda: BotCore._is_radio_only(bot)
        guild = SimpleNamespace(id=1, me=SimpleNamespace(), preferred_locale="pt-BR")
        channel = SimpleNamespace(
            id=5,
            send=AsyncMock(),
            permissions_for=lambda me: SimpleNamespace(read_message_history=True, send_messages=True),
        )
        message = SimpleNamespace(
            content="<@99>",
            guild=guild,
            channel=channel,
            author=SimpleNamespace(bot=False, mention="<@7>", voice=None, id=7),
            flags=SimpleNamespace(ephemeral=False),
            id=1,
            mentions=[],
            reply=AsyncMock(),
        )
        return BotCore, bot, message

    @pytest.mark.asyncio
    async def test_a_radio_only_bot_does_not_answer_a_plain_mention(self):
        core, bot, message = self.self_and_message(radio_only=True)

        await core.on_message(bot, message)

        message.reply.assert_not_awaited()
        message.channel.send.assert_not_awaited()  # nem boas-vindas, nem aviso: a menção pura não é comando

    @pytest.mark.asyncio
    async def test_a_normal_bot_still_greets_a_plain_mention(self):
        core, bot, message = self.self_and_message(radio_only=False)

        await core.on_message(bot, message)

        assert message.reply.await_count + message.channel.send.await_count == 1
