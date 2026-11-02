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
    MENU_VALUES,
    PANEL_IDS,
    PANEL_MENU_ID,
    PANEL_MUTE_ID,
    PANEL_NEXT_ID,
    PANEL_PAUSE_ID,
    PANEL_PREVIOUS_ID,
    PANEL_STOP_ID,
    PANEL_VOLUME_DOWN_ID,
    PANEL_VOLUME_UP_ID,
    check_radio,
    menu_action,
    panel_action,
    radio_id,
)
from empiremuse.utils.radio.panel import (
    LiveView,
    StationCard,
    custom_payload,
    live_payload,
    panel_payload,
    split_stream_title,
)
from tests.cogs.test_radio_ao_vivo import MAIN, RESERVE, make_live

pytestmark = pytest.mark.critical

CARD = StationCard(name="Rádio Boa FM", country="Brasil", codec="MP3", bitrate=128)


def png_logo() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"logo"


def tr_pt():
    return get_i18n().for_locale("pt-BR")


def ids(payload):
    return [b.custom_id for b in payload["components"][0].children]


def text(payload):
    embed = payload["embeds"][0]
    return "\n".join([embed.title or "", embed.description or "", *(f"{f.name}\n{f.value}" for f in embed.fields)])


class TestLivePanel:
    """O painel da rádio ao vivo, como o dono especificou: título, ``{estação} – {país}``, o que está no ar, 5 botões e o select "Mais opções"."""

    def test_the_title_the_station_line_and_the_two_rows(self):
        payload = live_payload(tr_pt(), station=CARD, view=LiveView(has_list=True))

        embed = payload["embeds"][0]
        assert embed.title == "Rádio ao Vivo 24/7" and "**Rádio Boa FM – Brasil**" in embed.description
        assert ids(payload) == [
            PANEL_PREVIOUS_ID,
            PANEL_VOLUME_DOWN_ID,
            PANEL_MUTE_ID,
            PANEL_VOLUME_UP_ID,
            PANEL_NEXT_ID,
        ]
        menu = payload["components"][1].children[0]
        assert menu.custom_id == PANEL_MENU_ID and [o.value for o in menu.options] == list(MENU_VALUES)

    def test_without_a_list_there_is_no_previous_or_next(self):
        payload = live_payload(tr_pt(), station=CARD, view=LiveView())

        assert ids(payload) == [PANEL_VOLUME_DOWN_ID, PANEL_MUTE_ID, PANEL_VOLUME_UP_ID]

    def test_there_is_no_music_player_control_on_it(self):
        payload = live_payload(tr_pt(), station=CARD, view=LiveView(has_list=True))

        everything = set(ids(payload)) | {payload["components"][1].children[0].custom_id}
        assert (
            everything <= set(PANEL_IDS) | {PANEL_MENU_ID}
            and PANEL_PAUSE_ID not in everything
            and PANEL_STOP_ID not in everything
        )
        assert len(payload["components"]) == 2  # nada de fila, busca, pedido, favoritos da Jukebox nem repetir

    def test_without_the_country_only_the_station_name(self):
        shown = live_payload(tr_pt(), station=StationCard(name="Rádio Boa FM"))["embeds"][0].description

        assert "**Rádio Boa FM**" in shown and "–" not in shown

    def test_now_playing_is_song_dash_artist_from_the_stream_title(self):
        shown = live_payload(tr_pt(), station=CARD, view=LiveView(title="Artista - Faixa"))["embeds"][0].description

        assert "Tocando agora: Faixa – Artista" in shown

    def test_a_stream_title_without_a_separator_is_all_song(self):
        shown = live_payload(tr_pt(), station=CARD, view=LiveView(title="Só uma faixa"))["embeds"][0].description

        assert "Tocando agora: Só uma faixa" in shown and "–  " not in shown

    def test_without_a_title_there_is_no_now_playing_line_and_never_the_word_unknown(self):
        for view in (LiveView(), LiveView(title=""), LiveView(title="   ")):
            shown = live_payload(tr_pt(), station=CARD, view=view)["embeds"][0].description.lower()

            assert "tocando agora" not in shown and "unknown" not in shown and "desconhecid" not in shown

    def test_muted_offline_and_reserve_states(self):
        muted = live_payload(tr_pt(), station=CARD, view=LiveView(muted=True, title="Artista - Faixa"))
        offline = live_payload(tr_pt(), station=CARD, view=LiveView(offline=True, title="x"))
        reserve = live_payload(tr_pt(), station=CARD, view=LiveView(on_reserve=True))

        mute_button = muted["components"][0].children[1]
        assert mute_button.style is disnake.ButtonStyle.danger and mute_button.label == "Som"
        assert "Silenciada" in muted["embeds"][0].description and "Tocando agora" not in muted["embeds"][0].description
        assert (
            "Fora do ar" in offline["embeds"][0].description and "Tocando agora" not in offline["embeds"][0].description
        )
        assert "reserva" in reserve["embeds"][0].description.lower()
        assert live_payload(tr_pt(), station=CARD)["components"][0].children[1].label == "Mudo"

    def test_the_thumbnail_is_the_station_logo_attached_or_the_generic_icon_or_the_avatar(self, monkeypatch):
        from empiremuse.utils.radio import panel as panel_module

        monkeypatch.setattr(panel_module, "generic_icon", lambda: b"generico")
        with_logo = live_payload(tr_pt(), station=CARD, logo=png_logo())
        generic = live_payload(tr_pt(), station=CARD)
        monkeypatch.setattr(panel_module, "generic_icon", lambda: None)
        avatar = live_payload(tr_pt(), station=CARD, avatar="https://cdn.discordapp.com/avatar.png")

        assert (
            with_logo["embeds"][0].thumbnail.url == "attachment://logo.png" and with_logo["file"].filename == "logo.png"
        )
        assert with_logo["file"].fp.read() == png_logo()
        assert (
            generic["embeds"][0].thumbnail.url == "attachment://logo.png" and generic["file"].fp.read() == b"generico"
        )
        assert avatar["embeds"][0].thumbnail.url == "https://cdn.discordapp.com/avatar.png" and "file" not in avatar

    def test_station_text_and_title_go_through_the_single_external_text_function(self):
        hostile = StationCard(name="@everyone <@1> https://evil.example", country="[x](https://evil.example)")
        payload = live_payload(
            tr_pt(), station=hostile, view=LiveView(title="@here [clique](https://evil.example/x) - Faixa")
        )

        shown = text(payload)
        assert "@everyone" not in shown and "@here" not in shown and "<@" not in shown and "://" not in shown
        long_title = text(live_payload(tr_pt(), station=CARD, view=LiveView(title="z" * 1000)))
        assert len(long_title) < 700

    def test_the_buttons_carry_the_mnauliady_icons_and_fixed_ids(self):
        row = live_payload(tr_pt(), station=CARD, view=LiveView(has_list=True))["components"][0]

        assert all(b.emoji is not None and b.label for b in row.children)
        assert all(not any(ch.isdigit() or ch == ":" for ch in b.custom_id) for b in row.children)


class TestSplitStreamTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Artista - Faixa", ("Faixa", "Artista")),
            ("Artista - Faixa - Remix", ("Faixa - Remix", "Artista")),
            ("Sem separador", ("Sem separador", None)),
            (" - Faixa", (" - Faixa".strip(), None)),
            ("Artista - ", ("Artista -", None)),
            ("", ("", None)),
            (None, ("", None)),
            (5, ("", None)),
        ],
    )
    def test_it_splits_artist_and_song_on_the_first_dash(self, raw, expected):
        assert split_stream_title(raw) == expected


class TestCustomPanel:
    def test_it_has_pause_next_and_stop_and_nothing_of_the_live_radio(self):
        payload = custom_payload(tr_pt(), playlist_name="Rock")

        assert ids(payload) == [PANEL_PAUSE_ID, PANEL_NEXT_ID, PANEL_STOP_ID]
        assert not {PANEL_PREVIOUS_ID, PANEL_MUTE_ID, PANEL_VOLUME_DOWN_ID, PANEL_VOLUME_UP_ID} & set(ids(payload))

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
        live = panel_payload(tr_pt(), live=True, station=CARD, view=LiveView(has_list=True))
        assert PANEL_MUTE_ID in ids(live) and PANEL_PAUSE_ID not in ids(live)


class TestActions:
    def test_the_fixed_ids_map_and_forged_ones_do_not(self):
        assert [panel_action(i) for i in PANEL_IDS] == [
            "pause",
            "next",
            "stop",
            "previous",
            "volume_down",
            "mute",
            "volume_up",
            "station_previous",
            "station_next",
        ]
        for forged in ("radio_panel_mute:1", "RADIO_PANEL_MUTE", "radio_panel_mute ", "radio_panel_menu", 5, None):
            assert panel_action(forged) is None

    def test_the_menu_values_are_the_five_options_and_nothing_else(self):
        assert [menu_action(v) for v in MENU_VALUES] == list(MENU_VALUES)
        for forged in ("Stations", "stations ", "../x", "", None, 5, ["stop"]):
            assert menu_action(forged) is None


class TestStationChange:
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


class TestRadioIsOutsideTheMusicSession:
    """A sessão de música (retomada após o reinício) não guarda o player de uma rádio: ela o traria de volta como um player comum, controlável pela
    Jukebox (a personalizada) ou com a URL local do relay (a ao vivo). Quem retoma a rádio é o gerenciador de rádios."""

    @pytest.mark.asyncio
    async def test_save_info_skips_a_radio_player_even_connected_to_a_call(self):
        from empiremuse.cogs.player_resume import PlayerSession

        saved = AsyncMock()
        cog = SimpleNamespace(save_session=saved)
        in_call = SimpleNamespace(channel=SimpleNamespace(id=900))
        radio = SimpleNamespace(
            radio_id="1:2", guild=SimpleNamespace(me=SimpleNamespace(voice=in_call)), is_closing=False
        )

        await PlayerSession.save_info(cog, radio)  # sem o guard isto seguiria montando e gravando a sessão

        saved.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_save_queue_does_nothing_for_a_radio_player(self):
        from empiremuse.utils.music.models.player_state import PlayerStateMixin

        radio = SimpleNamespace(radio_id="1:2", _session_resuming=False, bot=MagicMock())

        await PlayerStateMixin.process_save_queue(radio)

        radio.bot.get_cog.assert_not_called()
        normal = SimpleNamespace(_session_resuming=False, _queue_updater_task=None, bot=MagicMock())
        normal.bot.config = {"PLAYER_SESSIONS_MONGODB": False, "MONGO": False}
        normal.bot.get_cog.return_value = None
        await PlayerStateMixin.process_save_queue(normal)
        normal.bot.get_cog.assert_called_once_with("PlayerSession")


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
