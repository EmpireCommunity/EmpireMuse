"""O painel de controle da rádio na call (item 8, R1, R-6): o conteúdo, o envio e a recriação, e os botões. A rádio vem do bot e do servidor da
interação, nunca do ``custom_id``; ``manage_guild`` é conferido na execução; o painel apagado volta na hora, mas só um a cada 5 s."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.i18n import get_i18n
from empiremuse.rules.radio import PANEL_IDS, PANEL_NEXT_ID, PANEL_PAUSE_ID, PANEL_STOP_ID, panel_action, radio_id
from empiremuse.utils.radio.panel import panel_payload
from empiremuse.utils.radio.runner import Phase
from tests.cogs.test_radio_comandos import (  # noqa: F401 - fresh_cooldown é fixture autouse
    CHANNEL,
    GUILD,
    build as build_world,
    fresh_cooldown,
    premium,
    run_on,
    settle,
)
from tests.cogs.test_radio_portas import build as build_ports, grant

pytestmark = pytest.mark.critical

BOT_ID = 11


def tr_pt():
    return get_i18n().for_locale("pt-BR")


class TestPayload:
    def test_the_custom_panel_has_three_buttons_with_fixed_ids_and_nothing_of_the_radio_inside(self):
        payload = panel_payload(tr_pt(), playlist_name="Rock")

        row = payload["components"][0]
        assert [b.custom_id for b in row.children] == [PANEL_PAUSE_ID, PANEL_NEXT_ID, PANEL_STOP_ID]
        assert all(":" not in b.custom_id and not any(ch.isdigit() for ch in b.custom_id) for b in row.children)
        assert row.children[2].style is disnake.ButtonStyle.danger

    def test_the_playlist_name_goes_through_the_single_external_text_function(self):
        payload = panel_payload(tr_pt(), playlist_name="@everyone <@123> https://evil.example/x")

        embed = payload["embeds"][0]
        text = "\n".join([embed.description or "", *(f"{f.name}\n{f.value}" for f in embed.fields)])
        assert "@everyone" not in text and "<@123>" not in text and "://" not in text

    def test_panel_actions_map_only_the_fixed_ids(self):
        assert [panel_action(i) for i in PANEL_IDS] == ["pause", "next", "stop", "stations", "reserve"]
        for forged in (
            "radio_panel_pause:1:2",
            "radio_panel_stop ",
            "RADIO_PANEL_STOP",
            "",
            None,
            5,
            "musicplayer_stop",
        ):
            assert panel_action(forged) is None


class TestEnsurePanel:
    @pytest.mark.asyncio
    async def test_it_sends_the_panel_once_stores_the_id_and_tracks_it(self):
        w = build_ports()
        await grant(w)
        w.channel.send = AsyncMock(return_value=SimpleNamespace(id=4242))
        w.channel.fetch_message = AsyncMock()
        w.bot.get_color = lambda me: 0

        await w.ports.ensure_panel()

        w.channel.send.assert_awaited_once()
        assert w.channel.send.await_args.kwargs["allowed_mentions"].everyone is False
        assert (await w.hub.store.get(w.ports.radio.id)).panel_message_id == 4242
        assert w.hub.manager._panels == {4242: w.ports.radio.id}

    @pytest.mark.asyncio
    async def test_an_existing_panel_is_kept_not_sent_again(self):
        w = build_ports()
        await grant(w)
        await w.hub.store.set_panel_message(w.ports.radio.id, 4242)
        w.channel.send = AsyncMock()
        w.channel.fetch_message = AsyncMock(return_value=SimpleNamespace(id=4242))

        await w.ports.ensure_panel()

        w.channel.send.assert_not_awaited()
        assert w.hub.manager._panels == {4242: w.ports.radio.id}

    @pytest.mark.asyncio
    async def test_a_deleted_panel_is_replaced(self):
        w = build_ports()
        await grant(w)
        await w.hub.store.set_panel_message(w.ports.radio.id, 4242)
        w.channel.fetch_message = AsyncMock(side_effect=disnake.NotFound(MagicMock(status=404), "x"))
        w.channel.send = AsyncMock(return_value=SimpleNamespace(id=5555))
        w.bot.get_color = lambda me: 0

        await w.ports.ensure_panel()

        assert (await w.hub.store.get(w.ports.radio.id)).panel_message_id == 5555

    @pytest.mark.asyncio
    async def test_without_permission_to_send_it_only_warns(self, caplog):
        w = build_ports()
        await grant(w)
        w.channel.fetch_message = AsyncMock()
        w.channel.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(status=403), "x"))
        w.bot.get_color = lambda me: 0

        with caplog.at_level("WARNING"):
            await w.ports.ensure_panel()

        assert "sem permissão" in caplog.text and (await w.hub.store.get(w.ports.radio.id)).panel_message_id is None

    @pytest.mark.asyncio
    async def test_skip_calls_next_on_the_radios_player(self):
        w = build_ports()
        await w.ports.connect()

        await w.ports.skip_track()

        w.player.process_next.assert_awaited_once_with()


class TestRecreateOnDelete:
    @pytest.mark.asyncio
    async def test_a_deleted_panel_comes_back_at_once(self):
        w = build_world()
        await premium(w)
        await run_on(w, channel=w.channel, playlist="Rock")
        radio = radio_id(2, GUILD)
        calls = []
        w.ports.ensure_panel = lambda: _record(calls)
        w.hub.manager.note_panel(radio, 777)

        w.hub.manager.panel_deleted(777)
        await settle(w)

        assert calls == ["panel"]

    @pytest.mark.asyncio
    async def test_any_other_deleted_message_is_none_of_its_business(self):
        w = build_world()
        await premium(w)
        await run_on(w, channel=w.channel, playlist="Rock")
        calls = []
        w.ports.ensure_panel = lambda: _record(calls)
        w.hub.manager.note_panel(radio_id(2, GUILD), 777)

        w.hub.manager.panel_deleted(778)
        await settle(w)

        assert calls == []

    @pytest.mark.asyncio
    async def test_someone_deleting_in_a_loop_gets_one_new_panel_per_window(self, monkeypatch):
        from empiremuse.utils.radio import manager as manager_module

        w = build_world()
        await premium(w)
        await run_on(w, channel=w.channel, playlist="Rock")
        radio = radio_id(2, GUILD)
        calls, sleeps = [], []
        clock = [1000.0]
        monkeypatch.setattr(manager_module.time, "monotonic", lambda: clock[0])

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        monkeypatch.setattr(manager_module.asyncio, "sleep", fake_sleep)
        w.ports.ensure_panel = lambda: _record(calls)

        for message_id in (1, 2, 3):
            w.hub.manager.note_panel(radio, message_id)
            w.hub.manager.panel_deleted(message_id)
            await settle(w)
            clock[0] += 1.0

        assert calls == ["panel", "panel", "panel"] and all(s > 0 for s in sleeps) and len(sleeps) == 2

    @pytest.mark.asyncio
    async def test_a_stopped_radio_does_not_get_its_panel_back(self):
        w = build_world()
        await premium(w)
        await run_on(w, channel=w.channel, playlist="Rock")
        calls = []
        w.ports.ensure_panel = lambda: _record(calls)
        w.hub.manager.note_panel(radio_id(2, GUILD), 777)
        await w.hub.manager.deactivate(radio_id(2, GUILD))

        w.hub.manager.panel_deleted(777)
        await settle(w)

        assert calls == []


async def _record(calls):
    calls.append("panel")


def click(custom_id, *, manage_guild=True, channel_id=CHANNEL, guild_id=GUILD):
    return SimpleNamespace(
        data=SimpleNamespace(custom_id=custom_id),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
        guild=SimpleNamespace(id=guild_id, preferred_locale="pt-BR"),
        guild_id=guild_id,
        channel_id=channel_id,
        author=SimpleNamespace(id=7, guild_permissions=SimpleNamespace(manage_guild=manage_guild)),
    )


async def running(w):
    await premium(w)
    await run_on(w, channel=w.channel, playlist="Rock")
    # o clique chega ao bot DONO do painel: aqui o bot 2, o que está tocando
    w.cog.bot.user = SimpleNamespace(id=2)
    return w.hub.manager.runner(radio_id(2, GUILD))


def said(interaction):
    return interaction.edit_original_message.await_args.kwargs["content"]


class TestButtons:
    @pytest.mark.asyncio
    async def test_pause_toggles_and_answers_only_the_person_who_pressed(self):
        w = build_world()
        runner = await running(w)
        press = click(PANEL_PAUSE_ID)

        await w.cog.radio_panel_click(press)
        assert runner.phase is Phase.PAUSED_ADMIN and "pausada" in said(press)
        assert press.response.defer.await_args.kwargs == {"ephemeral": True, "with_message": True}

        again = click(PANEL_PAUSE_ID)
        await w.cog.radio_panel_click(again)
        assert runner.phase is Phase.PLAYING and "retomada" in said(again)

    @pytest.mark.asyncio
    async def test_next_skips_then_asks_to_wait(self):
        w = build_world()
        runner = await running(w)
        first, second = click(PANEL_NEXT_ID), click(PANEL_NEXT_ID)

        await w.cog.radio_panel_click(first)
        await w.cog.radio_panel_click(second)

        assert "próxima" in said(first) and "Não há o que pular" in said(second) and w.ports.skips == 1
        assert runner.phase is Phase.PLAYING

    @pytest.mark.asyncio
    async def test_stop_turns_the_radio_off_everywhere(self):
        w = build_world()
        await running(w)
        press = click(PANEL_STOP_ID)

        await w.cog.radio_panel_click(press)

        assert not w.hub.manager.is_running(radio_id(2, GUILD)) and "desligada" in said(press)
        assert (await w.hub.premium.fresh(GUILD)).active == 0

    @pytest.mark.asyncio
    async def test_without_manage_guild_nothing_happens(self):
        w = build_world()
        runner = await running(w)

        for custom_id in PANEL_IDS:
            press = click(custom_id, manage_guild=False)
            await w.cog.radio_panel_click(press)
            assert "Gerenciar Servidor" in said(press)

        assert runner.phase is Phase.PLAYING and w.hub.manager.is_running(radio_id(2, GUILD)) and w.ports.skips == 0

    @pytest.mark.asyncio
    async def test_a_forged_custom_id_is_ignored_without_even_answering(self):
        w = build_world()
        runner = await running(w)
        for forged in ("radio_panel_stop:2:500", f"radio_panel_stop_{radio_id(2, GUILD)}", "musicplayer_stop"):
            press = click(forged)

            await w.cog.radio_panel_click(press)

            press.response.defer.assert_not_awaited()

        assert runner.phase is Phase.PLAYING

    @pytest.mark.asyncio
    async def test_the_radio_comes_from_this_bot_and_the_interaction_guild_never_from_the_button(self):
        w = build_world()
        await running(w)
        w.cog.bot.user = SimpleNamespace(id=3)  # um OUTRO bot reserva recebe o clique: ele não tem rádio neste servidor
        press = click(PANEL_STOP_ID)

        await w.cog.radio_panel_click(press)

        assert w.hub.manager.is_running(radio_id(2, GUILD)) and "não está mais ligada" in said(press)

    @pytest.mark.asyncio
    async def test_a_click_from_another_guild_or_channel_does_not_reach_the_radio(self):
        w = build_world()
        runner = await running(w)

        for press in (click(PANEL_STOP_ID, guild_id=GUILD + 1), click(PANEL_STOP_ID, channel_id=CHANNEL + 1)):
            await w.cog.radio_panel_click(press)
            assert "não está mais ligada" in said(press)

        assert runner.phase is Phase.PLAYING and w.hub.manager.is_running(radio_id(2, GUILD))

    @pytest.mark.asyncio
    async def test_a_button_on_a_radio_that_is_off_says_so(self):
        w = build_world()
        await running(w)
        await w.hub.manager.deactivate(radio_id(2, GUILD))
        press = click(PANEL_PAUSE_ID)

        await w.cog.radio_panel_click(press)

        assert "não está mais ligada" in said(press)

    @pytest.mark.asyncio
    async def test_a_failure_inside_still_answers_the_click(self, caplog):
        w = build_world()
        runner = await running(w)
        runner.skip = AsyncMock(side_effect=RuntimeError("falha com token secreto"))
        press = click(PANEL_NEXT_ID)

        with caplog.at_level("WARNING"):
            await w.cog.radio_panel_click(press)

        assert (
            press.edit_original_message.await_count == 1
            and "secreto" not in caplog.text
            and "RuntimeError" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_generic_error_is_shown_with_its_plain_text_not_the_escaped_one(self):  # CodeRabbit #180
        from empiremuse.utils.music.errors import GenericError

        w = build_world()
        runner = await running(w)
        runner.skip = AsyncMock(side_effect=GenericError("canal_de_voz *sem* escape"))
        press = click(PANEL_NEXT_ID)

        await w.cog.radio_panel_click(press)

        assert press.edit_original_message.await_args.kwargs["content"] == "canal_de_voz *sem* escape"

    @pytest.mark.asyncio
    async def test_the_cog_forwards_deleted_messages_to_the_manager(self):
        w = build_world()
        w.hub.manager.panel_deleted = MagicMock()

        await w.cog.radio_panel_deleted(SimpleNamespace(message_id=9))
        await w.cog.radio_panel_bulk_deleted(SimpleNamespace(message_ids={1, 2}))

        assert sorted(c.args[0] for c in w.hub.manager.panel_deleted.call_args_list) == [1, 2, 9]
