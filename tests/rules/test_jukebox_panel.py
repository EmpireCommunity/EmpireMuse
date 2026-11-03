"""Regras puras do canal de pedidos único (item 11, P4): quem é o bot do painel, a call privada nunca mostra faixa (S10),
o botão age só no player da call de quem apertou (S7) e a migração copia só o canal do principal."""

import pytest

from empiremuse.rules.jukebox_panel import (
    HEAL_MIN_INTERVAL_MS,
    PANEL_REFRESH_MS,
    PAUSED,
    PLAYING,
    PanelPeer,
    JukeboxAction,
    choose_player,
    clock,
    elect_panel_bot,
    heal_wait_ms,
    last_action_of,
    make_entry,
    progress_text,
    refresh_wait_ms,
    jukebox_action,
)
from empiremuse.rules.servers import SettingError, changed_jukebox, migrate_jukebox

pytestmark = pytest.mark.critical

MAIN, R2, R10, EXCLUSIVE = (
    PanelPeer("TOKEN", 1),
    PanelPeer("TOKEN_2", 2),
    PanelPeer("TOKEN_10", 10),
    PanelPeer("TOKEN_3", 3, exclusive=True),
)


# ---------------------------------------------------------------------- eleição do bot do painel


@pytest.mark.parametrize(
    ("peers", "elected"),
    [
        ([R10, R2, MAIN], 1),  # o principal, esteja onde estiver na lista
        ([PanelPeer("ALFA", 4), MAIN], 1),  # e por papel, não por ordem do nome
        ([R10, R2], 2),  # sem o principal: a 1ª reserva pela ordem natural (TOKEN_2 antes de TOKEN_10)
        ([EXCLUSIVE, R10], 10),  # exclusivo de servidor só entra se não há mais ninguém
        ([EXCLUSIVE], 3),
        ([], None),
    ],
)
def test_the_panel_bot_is_the_main_else_the_first_reserve_else_an_exclusive_one(peers, elected):
    assert elect_panel_bot(peers) == elected


# ---------------------------------------------------------------------- S10: call privada


def entry(**overrides):
    values = {
        "bot_name": "Muse",
        "bot_id": 1,
        "voice_name": "Sala",
        "private": False,
        "title": "Faixa [x](http://e.vil)",
        "url": "https://exemplo.com/faixa",
        "paused": False,
        "queue": 3,
        "requester": "Ana",
    }
    return make_entry(**(values | overrides))


def test_a_private_call_never_carries_the_track_or_the_link():
    private = entry(private=True)
    assert private.private is True and private.title is None and private.url is None and private.requester is None
    assert private.bot_name == "Muse" and private.voice_name == "Sala"  # quem está onde continua aparecendo
    public = entry()
    assert (
        public.requester == "Ana" and public.title and public.url == "https://exemplo.com/faixa" and not public.private
    )


@pytest.mark.parametrize(("paused", "state"), [(False, PLAYING), (True, PAUSED)])
def test_the_state_follows_the_player(paused, state):
    assert entry(paused=paused).state == state


def test_third_party_texts_are_cleaned_and_bounded_and_the_queue_never_goes_negative():
    hostile = entry(bot_name="Mu‮se\n" + "x" * 500, voice_name=None, title="t" * 1000, url="u" * 1000, queue=-4)
    assert "\n" not in hostile.bot_name and "‮" not in hostile.bot_name and len(hostile.bot_name) <= 40
    assert hostile.voice_name == "call" and len(hostile.title) <= 100 and len(hostile.url) <= 250
    assert hostile.queue == 0


# ---------------------------------------------------------------------- S7: o botão age no player da call de quem apertou


@pytest.mark.parametrize(
    ("candidates", "member_voice", "chosen"),
    [
        ([(1, 100), (2, 200)], 200, 2),  # nunca o 1º da lista: o que está na call de quem apertou
        ([(1, 100), (2, 200)], 100, 1),
        ([(1, 100), (2, 200)], 300, None),  # call sem player
        ([(1, 100), (2, 200)], None, None),  # fora de call
        ([(1, None), (2, 200)], None, None),  # player sem call não casa com "sem call"
        ([], 100, None),
    ],
)
def test_the_player_is_the_one_in_the_callers_voice_channel_and_never_another(candidates, member_voice, chosen):
    assert choose_player(candidates, member_voice) == chosen


# ---------------------------------------------------------------------- no máximo uma edição a cada 3 s


@pytest.mark.parametrize(
    ("last", "now", "wait"),
    [
        (None, 5_000, 0),  # nunca editou
        (1_000, 1_000, PANEL_REFRESH_MS),
        (1_000, 2_000, PANEL_REFRESH_MS - 1_000),
        (1_000, 1_000 + PANEL_REFRESH_MS, 0),
        (9_000, 1_000, 0),  # relógio que voltou não trava o painel
    ],
)
def test_the_panel_is_edited_at_most_once_every_three_seconds(last, now, wait):
    assert refresh_wait_ms(last, now) == wait


# ---------------------------------------------------------------------- migração: só o canal do principal


@pytest.mark.parametrize(
    ("legacy", "request_section"),
    [
        ({"player_controller": {"channel": "12", "message_id": "34"}}, {"channel": "12", "message_id": "34"}),
        ({"player_controller": {"channel": 12, "message_id": 34}}, {"channel": "12", "message_id": "34"}),
        ({"player_controller": {"channel": None, "message_id": "34"}}, {"channel": None, "message_id": None}),
        ({"player_controller": {"channel": "abc", "message_id": "34"}}, {"channel": None, "message_id": None}),
        ({"player_controller": {"channel": "-5", "message_id": "0"}}, {"channel": None, "message_id": None}),
        ({"player_controller": {"channel": True, "message_id": "34"}}, {"channel": None, "message_id": None}),
        ({"player_controller": {"channel": "12", "message_id": "x"}}, {"channel": "12", "message_id": None}),
        ({}, {"channel": None, "message_id": None}),
        (None, {"channel": None, "message_id": None}),
    ],
)
def test_migration_keeps_only_valid_ids_and_leaves_the_panel_bot_to_the_election(legacy, request_section):
    assert migrate_jukebox(legacy) == {**request_section, "panel_bot_id": None}


def test_a_writer_that_sets_the_request_channel_to_garbage_is_refused_and_unchanged_fields_are_ignored():
    seen = {"channel": "12", "message_id": "34"}
    assert changed_jukebox({"player_controller": {"channel": "12", "message_id": "34"}}, seen) == {}
    assert changed_jukebox({"player_controller": {"channel": "99"}}, seen) == {"channel": "99"}
    assert changed_jukebox({"player_controller": {"message_id": None}}, seen) == {"message_id": None}
    with pytest.raises(SettingError):
        changed_jukebox({"player_controller": {"channel": "não é id"}}, seen)


# ---------------------------------------------------------------------- Jukebox estrita (item 8, J1)


def entrada(**over):
    base = dict(bot_name="B", bot_id=1, voice_name="Call", private=False, title="T", url="u", paused=False, queue=0)
    return make_entry(**{**base, **over})


class TestProgresso:
    def test_the_bar_fills_in_proportion_and_shows_both_clocks(self):
        assert progress_text(45_000, 90_000, width=10) == "▰▰▰▰▰▱▱▱▱▱ 0:45 / 1:30"

    def test_hours_only_appear_when_needed(self):
        assert clock(59_000) == "0:59" and clock(3_600_000) == "1:00:00" and clock(3_725_000) == "1:02:05"

    @pytest.mark.parametrize("position, duration", [(None, 90_000), (10, None), (10, 0), (10, -5)])
    def test_without_position_or_duration_there_is_no_bar(self, position, duration):
        assert progress_text(position, duration) is None

    def test_a_position_past_the_end_is_a_full_bar_and_a_negative_one_is_empty(self):
        assert progress_text(200_000, 100_000, width=4) == "▰▰▰▰ 1:40 / 1:40"
        assert progress_text(-5, 100_000, width=4) == "▱▱▱▱ 0:00 / 1:40"


class TestEntradaComVolumeEProgresso:
    def test_volume_and_progress_are_kept_and_bounded(self):
        e = entrada(volume=5000, position_ms=-3, duration_ms=200_000)
        assert (e.volume, e.position_ms, e.duration_ms) == (1000, 0, 200_000)

    def test_a_private_call_never_carries_volume_or_progress(self):
        e = entrada(private=True, volume=80, position_ms=1000, duration_ms=2000)
        assert (e.volume, e.position_ms, e.duration_ms) == (None, None, None)

    @pytest.mark.parametrize("junk", ["80", True, None, []])
    def test_text_and_boolean_volumes_are_dropped(self, junk):
        assert entrada(volume=junk).volume is None

    def test_a_zero_or_missing_duration_is_a_live_stream(self):
        assert entrada(duration_ms=0).duration_ms is None and entry().duration_ms is None


class TestPainelApagado:
    def test_first_heal_is_immediate_and_a_second_inside_the_window_waits(self):
        assert heal_wait_ms(None, 1_000) == 0
        assert heal_wait_ms(1_000, 1_000 + 2_000) == HEAL_MIN_INTERVAL_MS - 2_000
        assert heal_wait_ms(1_000, 1_000 + HEAL_MIN_INTERVAL_MS) == 0

    def test_a_clock_that_went_back_does_not_block(self):
        assert heal_wait_ms(10_000, 5_000) == 0


class TestJukeboxAction:
    @staticmethod
    def action(auto_delete, text_requests=True, **over):
        facts = dict(author_is_bot=False, is_command=False, has_mentions=False, is_system=False)
        return jukebox_action(auto_delete, text_requests, **{**facts, **over})

    @pytest.mark.parametrize("auto_delete", [True, False])
    @pytest.mark.parametrize("text_requests", [True, False])
    def test_bots_and_commands_are_never_touched(self, auto_delete, text_requests):
        assert self.action(auto_delete, text_requests, author_is_bot=True) is JukeboxAction.IGNORE
        assert self.action(auto_delete, text_requests, is_command=True) is JukeboxAction.IGNORE

    def test_off_keeps_the_plain_flow(self):
        assert self.action(False) is JukeboxAction.PROCESS
        assert self.action(False, has_mentions=True) is JukeboxAction.IGNORE

    def test_auto_delete_removes_a_typed_name_at_once_and_still_serves_it(self):
        assert self.action(True) is JukeboxAction.DELETE_AND_PROCESS

    def test_auto_delete_removes_mentions_and_system_messages_without_a_request(self):
        assert self.action(True, has_mentions=True) is JukeboxAction.DELETE
        assert self.action(True, is_system=True) is JukeboxAction.DELETE

    def test_without_text_requests_a_plain_message_is_not_a_request(self):
        assert self.action(False, False) is JukeboxAction.IGNORE  # nada a fazer: não pede e não apaga
        assert self.action(True, False) is JukeboxAction.DELETE  # a Jukebox só aceita comando: o texto solto sai

    def test_text_requests_default_to_on(self):
        assert jukebox_action(False, author_is_bot=False, is_command=False, has_mentions=False) is JukeboxAction.PROCESS


class TestUltimaAcao:
    def test_the_first_non_private_non_empty_action_wins(self):
        assert last_action_of([(True, "secreto"), (False, ""), (False, "pulou"), (False, "pausou")]) == "pulou"

    def test_nothing_usable_is_empty(self):
        assert last_action_of([]) == "" and last_action_of([(True, "x"), (False, None)]) == ""
