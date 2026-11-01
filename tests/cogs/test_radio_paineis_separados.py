"""Dois painéis, duas funcionalidades (item 8): a rádio ao vivo e a personalizada têm cada uma o SEU painel e não se misturam. A ao vivo é só estação
(estações por local, reserva, desligar); a personalizada é a playlist do servidor como rádio (pausar, pular, desligar). Nenhuma toca por busca, link,
Spotify nem YouTube, e nenhuma puxa autoplay. O "Próxima" saiu do painel ao vivo.

Também: o status do canal de voz da ao vivo (estação e o que está no ar), a troca da estação principal e o botão Reserva."""

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.i18n import get_i18n
from empiremuse.rules.radio import (
    PANEL_IDS,
    PANEL_NEXT_ID,
    PANEL_PAUSE_ID,
    PANEL_RESERVE_ID,
    PANEL_STATIONS_ID,
    PANEL_STOP_ID,
    check_radio,
    panel_action,
    radio_id,
)
from empiremuse.utils.radio.panel import LiveView, StationCard, custom_payload, live_payload, panel_payload
from tests.cogs.test_radio_ao_vivo import MAIN, RESERVE, LivePorts, make_live

pytestmark = pytest.mark.critical

FAVICON = "https://cdn.example/logo.png"
CARD = StationCard(name="Rádio Boa FM", detail="MP3 128k · Brasil", favicon=FAVICON)


def tr_pt():
    return get_i18n().for_locale("pt-BR")


def ids(payload):
    return [b.custom_id for b in payload["components"][0].children]


def text(payload):
    embed = payload["embeds"][0]
    return "\n".join([embed.title or "", embed.description or "", *(f"{f.name}\n{f.value}" for f in embed.fields)])


class TestLivePanel:
    def test_it_has_pause_stations_and_stop_and_never_next(self):
        payload = live_payload(tr_pt(), station=CARD, view=LiveView())

        assert ids(payload) == [PANEL_PAUSE_ID, PANEL_STATIONS_ID, PANEL_STOP_ID]
        assert PANEL_NEXT_ID not in ids(payload)
        assert payload["components"][0].children[2].style is disnake.ButtonStyle.danger

    def test_the_reserve_button_exists_only_when_there_is_a_reserve_and_leads_back_when_on_it(self):
        with_reserve = live_payload(tr_pt(), station=CARD, view=LiveView(has_reserve=True))
        on_reserve = live_payload(tr_pt(), station=CARD, view=LiveView(has_reserve=True, on_reserve=True))

        assert ids(with_reserve) == [PANEL_PAUSE_ID, PANEL_STATIONS_ID, PANEL_RESERVE_ID, PANEL_STOP_ID]
        assert with_reserve["components"][0].children[2].label == "Reserva"
        assert on_reserve["components"][0].children[2].label == "Principal"

    def test_it_shows_the_station_the_now_playing_title_and_the_status_in_the_jukebox_style(self):
        payload = live_payload(tr_pt(), station=CARD, view=LiveView(title="Artista - Faixa"))

        shown = text(payload)
        assert "Rádio Boa FM" in shown and "Tocando: Artista - Faixa" in shown and "MP3 128k · Brasil" in shown
        assert "Ao vivo" in shown and "-# " in shown  # as linhas miúdas da Jukebox
        assert payload["embeds"][0].footer.text and len(payload["embeds"][0].fields) == 1

    def test_waiting_offline_and_reserve_states(self):
        assert "Aguardando o título" in text(live_payload(tr_pt(), station=CARD, view=LiveView()))
        offline = text(live_payload(tr_pt(), station=CARD, view=LiveView(offline=True, title="x")))
        assert "Fora do ar" in offline and "Tocando:" not in offline
        assert "reserva" in text(live_payload(tr_pt(), station=CARD, view=LiveView(on_reserve=True))).lower()

    def test_the_thumbnail_is_the_station_logo_or_else_the_bot_avatar(self):
        avatar = "https://cdn.discordapp.com/avatar.png"
        with_logo = live_payload(tr_pt(), station=CARD, avatar=avatar)
        without = live_payload(tr_pt(), station=StationCard(name="X"), avatar=avatar)
        neither = live_payload(tr_pt(), station=StationCard(name="X"))

        assert with_logo["embeds"][0].thumbnail.url == FAVICON
        assert without["embeds"][0].thumbnail.url == avatar
        assert neither["embeds"][0].thumbnail.url is None

    def test_station_text_and_title_go_through_the_single_external_text_function(self):
        hostile = StationCard(name="@everyone <@1> https://evil.example", detail="[x](https://evil.example)")
        payload = live_payload(tr_pt(), station=hostile, view=LiveView(title="@here [clique](https://evil.example/x)"))

        shown = text(payload)
        assert "@everyone" not in shown and "@here" not in shown and "<@" not in shown and "://" not in shown
        assert len(text(live_payload(tr_pt(), station=CARD, view=LiveView(title="z" * 1000)))) < 600

    def test_the_buttons_carry_the_mnauliady_icons_and_fixed_ids(self):
        row = live_payload(tr_pt(), station=CARD, view=LiveView(has_reserve=True))["components"][0]

        assert all(b.emoji is not None and b.label for b in row.children)
        assert all(not any(ch.isdigit() or ch == ":" for ch in b.custom_id) for b in row.children)


class TestCustomPanel:
    def test_it_has_pause_next_and_stop_and_nothing_of_the_live_radio(self):
        payload = custom_payload(tr_pt(), playlist_name="Rock")

        assert ids(payload) == [PANEL_PAUSE_ID, PANEL_NEXT_ID, PANEL_STOP_ID]
        assert PANEL_STATIONS_ID not in ids(payload) and PANEL_RESERVE_ID not in ids(payload)

    def test_it_names_the_playlist_and_says_it_follows_a_clock(self):
        shown = text(custom_payload(tr_pt(), playlist_name="Rock"))

        assert "Rock" in shown and "relógio" in shown and "Rádio personalizada" in shown

    def test_the_playlist_name_is_neutralised_and_there_is_no_request_search_or_queue_control(self):
        payload = custom_payload(tr_pt(), playlist_name="@everyone <@123> https://evil.example/x")

        shown = text(payload)
        assert "@everyone" not in shown and "<@123>" not in shown and "://" not in shown
        assert {b.custom_id for b in payload["components"][0].children} <= set(PANEL_IDS)
        assert len(payload["components"]) == 1  # nenhum select (busca, gênero, playlists) nem segunda linha

    def test_the_dispatcher_picks_the_panel_by_the_kind_of_the_radio(self):
        assert ids(panel_payload(tr_pt(), playlist_name="Rock")) == [PANEL_PAUSE_ID, PANEL_NEXT_ID, PANEL_STOP_ID]
        live = panel_payload(tr_pt(), live=True, station=CARD, view=LiveView())
        assert PANEL_STATIONS_ID in ids(live) and PANEL_NEXT_ID not in ids(live)


class TestActions:
    def test_the_five_fixed_ids_map_and_forged_ones_do_not(self):
        assert [panel_action(i) for i in PANEL_IDS] == ["pause", "next", "stop", "stations", "reserve"]
        for forged in ("radio_panel_stations:1", "RADIO_PANEL_RESERVE", "radio_panel_reserve ", 5, None):
            assert panel_action(forged) is None


class TestReserveButtonAndStationChange:
    @pytest.mark.asyncio
    async def test_the_reserve_button_switches_to_the_other_station(self):
        runner, ports, _ = make_live()
        await runner.start()

        switched = await runner.switch_station()

        assert switched is True and ports.active == RESERVE

    @pytest.mark.asyncio
    async def test_a_second_press_inside_the_cooldown_does_nothing(self):
        runner, ports, _ = make_live()
        await runner.start()
        runner._last_skip = ports.now()

        assert await runner.switch_station() is False and ports.active == MAIN

    @pytest.mark.asyncio
    async def test_without_a_healthy_other_station_or_when_not_playing_it_does_nothing(self):
        runner, ports, _ = make_live(LivePorts(reserve=None))
        await runner.start()
        assert await runner.switch_station() is False and ports.active == MAIN

        runner, ports, _ = make_live()
        await runner.start()
        ports.unavailable.add(RESERVE)
        assert await runner.switch_station() is False and ports.active == MAIN

        runner, ports, _ = make_live()
        assert await runner.switch_station() is False  # ainda não está tocando

    @pytest.mark.asyncio
    async def test_a_new_main_station_resets_the_failure_cycle_the_outage_and_the_title(self):
        runner, _, _ = make_live()
        await runner.start()
        runner.failures, runner.outage = 3, True
        runner.set_title("velho")

        await runner.station_changed()

        assert (runner.failures, runner.outage, runner.title) == (0, False, None)

    @pytest.mark.asyncio
    async def test_a_stopped_runner_ignores_the_change(self):
        runner, _, _ = make_live()
        await runner.start()
        runner._stopped = True
        runner.failures = 2

        await runner.station_changed()

        assert runner.failures == 2


class TestNoAutoplayOnRadios:
    """Fila vazia num player de rádio NÃO puxa autoplay (YouTube, Last.fm...), nem por ``keep_connected`` nem por outro caminho."""

    @staticmethod
    def player(**extra):
        calls = SimpleNamespace(autoqueue=0)

        async def autoqueue():
            calls.autoqueue += 1
            return SimpleNamespace(title="sugestão do YouTube")

        return (
            SimpleNamespace(
                queue=deque(),
                autoplay=True,
                keep_connected=True,
                stop=AsyncMock(),
                get_autoqueue_tracks=autoqueue,
                process_next=AsyncMock(),
                bot=SimpleNamespace(config={"IDLE_TIMEOUT": 180}, loop=MagicMock()),
                **extra,
            ),
            calls,
        )

    @pytest.mark.asyncio
    async def test_an_empty_queue_on_a_radio_player_only_stops(self):
        from empiremuse.utils.music.models.playback_steps import _pn_next_track

        player, calls = self.player(radio_id="1:2")
        req = SimpleNamespace()

        handled = await _pn_next_track(player, req)

        assert handled is True and calls.autoqueue == 0 and player.stop.await_count == 1
        assert not hasattr(req, "track") and player.process_next.await_count == 0

    @pytest.mark.asyncio
    async def test_the_same_empty_queue_on_a_normal_player_still_uses_autoplay(self):
        from empiremuse.utils.music.models.playback_steps import _pn_next_track

        player, calls = self.player()
        req = SimpleNamespace()

        handled = await _pn_next_track(player, req)

        assert handled is False and calls.autoqueue == 1 and req.track.title == "sugestão do YouTube"

    @pytest.mark.asyncio
    async def test_the_autoplay_itself_refuses_a_radio_player(self):
        from empiremuse.utils.music.models.player_autoqueue import PlayerAutoqueueMixin

        radio = SimpleNamespace(radio_id="1:2", queue_autoplay=deque([SimpleNamespace(title="x")]))

        assert await PlayerAutoqueueMixin.get_autoqueue_tracks(radio) is None


class TestLiveSkipDoesNothing:
    @pytest.mark.asyncio
    async def test_the_live_ports_never_call_process_next_on_a_skip(self):
        from empiremuse.utils.radio.live_ports import LiveRadioPorts
        from tests.cogs.test_radio_portas import BOT_ID, GUILD, build as build_ports

        w = build_ports()
        radio = check_radio(
            {
                "_id": radio_id(BOT_ID, GUILD),
                "channel_id": "900",
                "kind": "live",
                "source": {"station": MAIN, "reserve": RESERVE},
            }
        )
        ports = LiveRadioPorts(w.ports.pool, radio)
        await w.ports.connect()

        await ports.skip_track()

        w.player.process_next.assert_not_awaited()


class TestVoiceStatusOfTheLiveRadio:
    """O status do canal de voz mostrava "Tocando: Unknown title - Unknown artist": agora a estação é o título e o que está no ar é o autor."""

    @staticmethod
    def build():
        from empiremuse.utils.radio.live_ports import LiveRadioPorts
        from tests.cogs.test_radio_portas import BOT_ID, GUILD, build as build_ports

        w = build_ports()
        track = SimpleNamespace(title="Unknown title", author="Unknown artist", info={})
        w.player.current = track
        w.player.radio_live = True
        w.player.tr = lambda key, **kw: {"radio.live.status_author": "Ao vivo"}.get(key, key)
        w.player.update_stage_topic = AsyncMock()
        w.bot.music.players[GUILD] = w.player
        radio = check_radio(
            {
                "_id": radio_id(BOT_ID, GUILD),
                "channel_id": "900",
                "kind": "live",
                "source": {"station": MAIN, "reserve": None},
            }
        )
        return w, LiveRadioPorts(w.ports.pool, radio), track

    @pytest.mark.asyncio
    async def test_a_new_stream_title_becomes_the_author_and_refreshes_the_status(self):
        w, ports, track = self.build()

        ports._announce("Artista - Faixa")
        await asyncio.sleep(0)

        assert track.author == "Artista - Faixa"
        w.player.update_stage_topic.assert_awaited_once_with(reconnect=False)

    @pytest.mark.asyncio
    async def test_without_a_title_the_author_is_the_live_label_and_a_repeat_does_not_refresh_again(self):
        w, ports, track = self.build()

        ports._announce(None)
        await asyncio.sleep(0)
        ports._announce(None)
        await asyncio.sleep(0)

        assert track.author == "Ao vivo" and w.player.update_stage_topic.await_count == 1

    @pytest.mark.asyncio
    async def test_a_player_that_is_not_a_live_radio_is_left_alone(self):
        w, ports, track = self.build()
        w.player.radio_live = False

        ports._announce("Artista - Faixa")
        await asyncio.sleep(0)

        assert track.author == "Unknown artist" and w.player.update_stage_topic.await_count == 0

    @pytest.mark.asyncio
    async def test_a_failure_updating_the_status_is_only_logged(self, caplog):
        w, ports, _ = self.build()
        w.player.update_stage_topic = AsyncMock(side_effect=RuntimeError("falha com token secreto"))

        with caplog.at_level("WARNING"):
            ports._announce("Artista - Faixa")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert "RuntimeError" in caplog.text and "token secreto" not in caplog.text

    @pytest.mark.asyncio
    async def test_the_status_text_shows_the_station_and_the_stream_title_sanitised(self):
        from empiremuse.rules.voice_status import DEFAULT_VOICE_TEMPLATE, render_voice_status, voice_status_value

        _, ports, track = self.build()
        track.title = "Rádio Boa FM"
        ports._announce("@everyone [x](https://evil.example) *negrito*")

        values = {
            "{track.emoji}": "♪",
            "{track.title}": voice_status_value(track.title),
            "{track.author}": voice_status_value(track.author),
        }
        status = render_voice_status(DEFAULT_VOICE_TEMPLATE, values)

        assert "Rádio Boa FM" in status and "Unknown" not in status
        assert "@everyone" not in status and "https://" not in status and "*negrito*" not in status
