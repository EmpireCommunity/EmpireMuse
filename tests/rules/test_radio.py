"""Regras puras da rádio (item 8, R1): ``check_radio`` e o chat (limites e tipos), o teto, o recuo da reconexão (R-5), a varredura (R-4)
e o relógio virtual."""

import dataclasses

import pytest

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import (
    ABSENCE_PAUSE_S,
    CHAT_LIMITS,
    MAX_PER_GUILD,
    PRESENCE_RESUME_S,
    SWEEP_BATCH,
    SWEEP_MAX_AGE_S,
    Radio,
    backoff_s,
    belongs_to_guild,
    check_chat,
    check_radio,
    source_chosen,
    clamp_active,
    default_chat,
    has_room,
    radio_id,
    should_pause,
    should_resume,
    split_radio_id,
    sweep_victims,
    to_document,
    virtual_position,
    voice_sweep_interval_s,
)

pytestmark = pytest.mark.critical

BOT, GUILD, CHANNEL = 111, 222, 333
UUID = "960e57c5-1c3d-4d7e-8c2b-1f3a6e0a9b11"


def doc(**over):
    base = {
        "_id": radio_id(BOT, GUILD),
        "channel_id": str(CHANNEL),
        "kind": "custom",
        "source": {"playlist": "Rock"},
        "chat": {"voice_delete_after_s": 60, "stage_max_messages": 7, "stage_max_age_s": 600},
        "enabled": False,
        "rev": 3,
    }
    return {**base, **over}


class TestCheckRadio:
    def test_a_valid_document_becomes_a_radio_with_integer_ids(self):
        radio = check_radio(doc())

        assert isinstance(radio, Radio)
        assert (radio.id, radio.bot_id, radio.guild_id, radio.channel_id) == (f"{BOT}:{GUILD}", BOT, GUILD, CHANNEL)
        assert radio.source == {"playlist": "Rock"} and radio.chat == default_chat() and radio.rev == 3

    def test_the_panel_shape_asdict_of_a_radio_is_accepted(self):
        shape = dataclasses.asdict(check_radio(doc()))

        edited = check_radio({**shape, "chat": {**shape["chat"], "voice_delete_after_s": 90}})

        assert edited.chat["voice_delete_after_s"] == 90

    @pytest.mark.parametrize("bad", ["", "1", "1:", ":2", "a:b", "1:2:3", "-1:2", None, 5, "1: 2"])
    def test_a_malformed_id_is_refused(self, bad):
        assert isinstance(check_radio(doc(_id=bad)), RuleError)

    def test_bot_and_guild_ids_that_disagree_with_the_id_are_refused(self):
        assert isinstance(check_radio({**doc(), "bot_id": BOT + 1}), RuleError)
        assert isinstance(check_radio({**doc(), "guild_id": str(GUILD + 1)}), RuleError)
        assert isinstance(check_radio({**doc(), "bot_id": BOT, "guild_id": GUILD}), Radio)

    @pytest.mark.parametrize("bad", [None, "", 0, -3, True, "x", "12 3", 1.5])
    def test_the_channel_must_be_a_positive_snowflake(self, bad):
        assert check_radio(doc(channel_id=bad)).code == "radio_channel_invalid"

    @pytest.mark.parametrize("bad", ["live2", "", None, 1, "Custom"])
    def test_the_kind_is_one_of_the_two(self, bad):
        assert check_radio(doc(kind=bad)).code == "radio_kind_invalid"

    @pytest.mark.parametrize(
        "source",
        [
            None,
            {"playlist": ""},
            {"playlist": "x" * 101},
            {"playlist": 3},
            {"playlist": "a\nb"},
            {"playlist": "ok", "x": 1},
        ],
    )
    def test_a_custom_source_is_exactly_one_playlist_name(self, source):
        assert check_radio(doc(source=source)).code == "radio_source_invalid"

    @pytest.mark.parametrize("kind", ["custom", "live"])
    def test_a_configured_radio_may_have_no_station_yet_and_cannot_be_turned_on(self, kind):
        radio = check_radio(doc(kind=kind, source={}))

        assert radio.source == {} and source_chosen(radio) is False
        assert source_chosen(check_radio(doc(source={"station": "0123456789abcdef01234567"}))) is True

    def test_a_live_source_needs_station_uuids(self):
        ok = doc(kind="live", source={"station": UUID, "reserve": None})
        assert check_radio(ok).source == {"station": UUID, "reserve": None}
        for bad in ({"station": "x"}, {"station": UUID, "reserve": "nope"}, {"station": UUID, "url": "http://x"}):
            assert check_radio(doc(kind="live", source=bad)).code == "radio_source_invalid"

    @pytest.mark.parametrize("bad", ["true", 1, 0, None])
    def test_enabled_is_a_real_boolean(self, bad):
        assert check_radio(doc(enabled=bad)).code == "radio_enabled_invalid"

    @pytest.mark.parametrize("bad", [-1, True, "3", 1.0])
    def test_rev_is_a_non_negative_integer(self, bad):
        assert check_radio(doc(rev=bad)).code == "radio_rev_invalid"

    def test_unknown_fields_are_ignored(self):
        assert isinstance(check_radio({**doc(), "updated_by": "7", "premium": True}), Radio)

    def test_not_a_mapping_is_refused(self):
        assert isinstance(check_radio([1, 2]), RuleError)

    def test_the_document_round_trips_and_keeps_updated_by_out_of_the_radio(self):
        radio = check_radio(doc(panel_message_id="999"))

        stored = to_document(radio, updated_by="77")

        assert stored["updated_by"] == "77" and stored["channel_id"] == str(CHANNEL)
        assert stored["panel_message_id"] == "999"
        assert check_radio(stored) == radio and not hasattr(radio, "updated_by")


class TestChat:
    def test_defaults_when_nothing_is_given(self):
        # o padrão da voz é 60 s (decisão do dono depois do teste ao vivo: ninguém perde uma mensagem); o do palco não mudou
        assert check_chat(None) == {"voice_delete_after_s": 60, "stage_max_messages": 7, "stage_max_age_s": 600}

    @pytest.mark.parametrize("field", list(CHAT_LIMITS))
    def test_the_limits_are_inclusive_at_both_ends(self, field):
        low, high, _ = CHAT_LIMITS[field]

        assert check_chat({field: low})[field] == low and check_chat({field: high})[field] == high
        assert check_chat({field: low - 1}).code == "radio_chat_invalid"
        assert check_chat({field: high + 1}).code == "radio_chat_invalid"

    @pytest.mark.parametrize("bad", [True, "30", 30.0, None, [30]])
    def test_a_non_integer_value_is_refused(self, bad):
        assert check_chat({"voice_delete_after_s": bad}).code == "radio_chat_invalid"

    def test_an_unknown_field_is_refused_and_named(self):
        refusal = check_chat({"enabled": True})

        assert refusal.code == "radio_chat_unknown_field" and refusal.key == "enabled"

    def test_a_partial_update_keeps_the_rest_from_the_base(self):
        base = {"voice_delete_after_s": 90, "stage_max_messages": 10, "stage_max_age_s": 120}

        assert check_chat({"stage_max_messages": 4}, base=base) == {**base, "stage_max_messages": 4}


class TestIds:
    def test_the_guild_suffix_check(self):
        assert belongs_to_guild(f"{BOT}:{GUILD}", GUILD) and not belongs_to_guild(f"{BOT}:{GUILD}", GUILD + 1)
        assert not belongs_to_guild(f"{BOT}:{GUILD}x", GUILD) and not belongs_to_guild(None, GUILD)
        assert not belongs_to_guild(f"{BOT}:{GUILD}:{GUILD}", GUILD)

    def test_split_round_trip(self):
        assert split_radio_id(radio_id(BOT, GUILD)) == (BOT, GUILD)


class TestCap:
    def test_three_per_guild_and_garbage_counts_as_zero(self):
        assert [has_room(n) for n in (0, 1, 2, 3, 4, -1)] == [True, True, True, False, False, False]
        assert MAX_PER_GUILD == 3
        assert [clamp_active(v) for v in (2, 99, -4, "2", True, None)] == [2, 3, 0, 0, 0, 0]


class TestPresence:
    def test_the_pause_waits_thirty_seconds_and_the_resume_five(self):
        assert not should_pause(100.0, 100.0 + ABSENCE_PAUSE_S - 0.1)
        assert should_pause(100.0, 100.0 + ABSENCE_PAUSE_S)
        assert not should_resume(100.0, 100.0 + PRESENCE_RESUME_S - 0.1)
        assert should_resume(100.0, 100.0 + PRESENCE_RESUME_S)

    def test_nobody_counting_means_no_decision(self):
        assert not should_pause(None, 1e9) and not should_resume(None, 1e9)


class TestBackoff:
    def test_the_steps_double_up_to_a_minute_and_stay_there(self):
        assert [backoff_s(n) for n in range(8)] == [5, 10, 20, 40, 60, 60, 60, 60]

    def test_jitter_stays_within_twenty_percent(self):
        for attempt in range(6):
            base = backoff_s(attempt, 0.5)
            assert backoff_s(attempt, 0.0) == pytest.approx(base * 0.8)
            assert backoff_s(attempt, 1.0) == pytest.approx(base * 1.2)

    def test_there_is_never_an_immediate_return(self):
        assert min(backoff_s(n, 0.0) for n in range(10)) >= 4.0

    def test_sixty_moves_in_sixty_seconds_make_at_most_six_returns(self):
        """R-5: um script que move o bot a cada segundo não vira rajada; cada volta espera pelo menos o recuo mínimo do passo atual."""
        clock, returns, attempt = 0.0, 0, 0
        while clock < 60:
            clock += backoff_s(attempt, 0.0)
            if clock <= 60:
                returns += 1
            attempt += 1

        assert returns <= 6


class TestSweep:
    CHAT = {"voice_delete_after_s": 30, "stage_max_messages": 3, "stage_max_age_s": 600}
    NOW = 1_000_000.0

    def test_voice_deletes_only_what_is_older_than_the_window(self):
        msgs = [(1, self.NOW - 100, False), (2, self.NOW - 10, False)]

        assert sweep_victims(msgs, self.NOW, stage=False, chat=self.CHAT) == [1]

    def test_the_pinned_the_panel_and_the_bot_notices_are_never_swept(self):
        msgs = [(1, self.NOW - 500, True), (2, self.NOW - 500, False)]

        assert sweep_victims(msgs, self.NOW, stage=False, chat=self.CHAT) == [2]

    def test_stage_keeps_at_most_n_recent_messages_and_drops_the_oldest(self):
        msgs = [(i, self.NOW - 100 + i, False) for i in range(1, 8)]  # 7 recentes (menos de 600 s)

        assert sweep_victims(msgs, self.NOW, stage=True, chat=self.CHAT) == [1, 2, 3, 4]

    def test_stage_also_drops_what_passed_the_age(self):
        msgs = [(1, self.NOW - 700, False), (2, self.NOW - 10, False)]

        assert sweep_victims(msgs, self.NOW, stage=True, chat=self.CHAT) == [1]

    def test_a_thousand_messages_become_one_batch_of_at_most_a_hundred(self):
        msgs = [(i, self.NOW - 100 - i, False) for i in range(1000)]

        assert len(sweep_victims(msgs, self.NOW, stage=False, chat=self.CHAT)) == SWEEP_BATCH

    def test_messages_of_fourteen_days_or_more_are_left_alone(self):
        msgs = [(1, self.NOW - SWEEP_MAX_AGE_S, False), (2, self.NOW - SWEEP_MAX_AGE_S + 5, False)]

        assert sweep_victims(msgs, self.NOW, stage=False, chat=self.CHAT) == [2]

    def test_the_voice_interval_is_half_the_window_but_never_under_five_seconds(self):
        assert voice_sweep_interval_s(5) == 5.0
        assert voice_sweep_interval_s(30) == 15.0
        assert voice_sweep_interval_s(600) == 300.0


class TestVirtualClock:
    def test_it_walks_the_list_and_wraps_around(self):
        durations = [60_000, 30_000, 90_000]  # 3 min

        assert virtual_position(durations, 0) == (0, 0)
        assert virtual_position(durations, 59_999) == (0, 59_999)
        assert virtual_position(durations, 60_000) == (1, 0)
        assert virtual_position(durations, 100_000) == (2, 10_000)
        assert virtual_position(durations, 180_000) == (0, 0)
        assert virtual_position(durations, 180_000 * 7 + 65_000) == (1, 5_000)

    def test_an_empty_or_zero_length_list_is_the_start(self):
        assert virtual_position([], 12345) == (0, 0) and virtual_position([0, 0], 12345) == (0, 0)

    def test_zero_length_tracks_are_skipped_and_a_negative_elapsed_is_the_start(self):
        assert virtual_position([0, 1000], 500) == (1, 500) and virtual_position([1000], -5) == (0, 0)
