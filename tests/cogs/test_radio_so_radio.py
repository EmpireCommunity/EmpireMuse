"""O bot só-rádio (item 8, R1): não atende prefixo, pedido nem menção (um aviso por canal por minuto) e nunca vira bot do painel da Jukebox."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.notice import refuse_radio_only
from empiremuse.utils.server_request import ServerRequest
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
        request = ServerRequest(SimpleNamespace(pool=pool))

        assert [peer.identifier for peer in request.peers(1)] == ["TOKEN_3"]
        assert request.elected_id(1) == 3  # sem o principal, a primeira reserva que NÃO é só-rádio
