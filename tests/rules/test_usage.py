"""Regras puras do registro de uso e do banimento: texto de terceiros, resumo da janela e decisão de sair de servidor banido."""

import pytest

from empiremuse.rules import usage
from empiremuse.rules.usage import (
    MS_PER_DAY,
    BanRecord,
    ban_from_doc,
    clamp_retention_days,
    clean_text,
    end_reason,
    error_label,
    fit_lines,
    guilds_to_leave,
    new_ban_record,
    normalize_event,
    render_text,
    should_leave,
    summarize,
)

pytestmark = pytest.mark.critical

NOW = 1_800_000_000_000


def event(n, kind="slash", **fields):
    return {"id": f"e{n:03d}", "ts": NOW - n * 1000, "kind": kind, **fields}


# ---------------------------------------------------------------------- texto de terceiros


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\x1b[31;1mServidor\x1b[0m", "Servidor"),
        ("\x1b]8;;http://x\x07link\x1b]8;;\x07", "link"),
        ("linha 1\r\nlinha 2\tfim", "linha 1 linha 2 fim"),
        ("nome‮​falso", "nomefalso"),
        ("  muitos    espaços  ", "muitos espaços"),
        (None, ""),
        (12345, "12345"),
    ],
)
def test_clean_text_removes_ansi_controls_and_collapses_lines(raw, expected):
    assert clean_text(raw, 100) == expected


def test_clean_text_caps_with_an_ellipsis_inside_the_limit():
    out = clean_text("a" * 500, 20)
    assert len(out) == 20
    assert out.endswith("…")


@pytest.mark.parametrize(
    "raw",
    [
        "@everyone",
        "<@123456789012345678>",
        "[clique](https://evil.example)",
        "||spoiler||",
        "# título",
        "`code`",
        "*b* _i_",
    ],
)
def test_render_text_neutralizes_markdown_and_mentions(raw):
    out = render_text(raw, 100)
    assert "@everyone" not in out and "<@" not in out and "](" not in out and "||" not in out
    # cada caractere especial vem precedido de barra: o Discord mostra o texto, não a formatação
    for index, found in enumerate(out):
        if found in "*_`|#<>[]()":
            assert index > 0 and out[index - 1] == "\\"


def test_render_text_cleans_stored_data_too():
    assert render_text("\x1b[31mvermelho\x1b[0m\n@here", 100) == "vermelho \\@\u200bhere"


def test_fit_lines_never_cuts_a_line_and_respects_the_limit():
    lines = ["a" * 10, "b" * 10, "c" * 10]
    assert fit_lines(lines, 25) == "a" * 10 + "\n" + "b" * 10
    assert fit_lines(lines, 5) == ""


# ---------------------------------------------------------------------- evento


def test_normalize_event_drops_everything_about_who_and_the_raw_query():
    raw = event(
        1,
        guild=10,
        user_id=99,
        author_id=98,
        user_name="fulano",
        avatar="x",
        member_count=500,
        query="texto digitado pelo usuário",
        name="play",
    )
    out = normalize_event(raw)
    assert out == {"id": "e001", "ts": NOW - 1000, "kind": "slash", "guild": 10, "name": "play"}


@pytest.mark.parametrize(
    "raw",
    [
        {"ts": NOW, "kind": "slash"},
        {"id": "", "ts": NOW, "kind": "slash"},
        {"id": "a", "kind": "slash"},
        {"id": "a", "ts": NOW, "kind": "desconhecido"},
        {"id": "a", "ts": True, "kind": "slash"},
        {"id": "a", "ts": -1, "kind": "slash"},
    ],
)
def test_normalize_event_rejects_events_without_the_essentials(raw):
    assert normalize_event(raw) is None


def test_normalize_event_keeps_only_fixed_labels_and_clamps_numbers():
    out = normalize_event(
        event(
            1,
            ok=False,
            error="Traceback: senha=abc",
            ms=10**12,
            end="inventado",
            listened=-5,
            title="\x1b[1mMúsica\x1b[0m",
        )
    )
    assert out["error"] == "unexpected"
    assert out["ms"] == usage.MAX_LATENCY_MS
    assert out["title"] == "Música"
    assert "end" not in out and "listened" not in out


# ---------------------------------------------------------------------- resumo


def test_summarize_counts_the_window_and_ranks_channels():
    events = [
        event(1, text_id=1, text_name="geral", guild=5),
        event(2, text_id=1, text_name="geral-novo", guild=5),
        event(3, "button", text_id=2, text_name="música", guild=5),
        event(4, "playback", voice_id=7, voice_name="Sala", title="A", listened=120, end="finished"),
        event(5, "playback", voice_id=7, voice_name="Sala", title="B", listened=30, end="skipped"),
        event(6, "playback", voice_id=8, voice_name="Outra", title="C", listened=10),
        event(7, "guild", name="join"),
    ]
    summary = summarize(events, now_ms=NOW)
    assert (summary.commands, summary.tracks, summary.listened_seconds) == (3, 3, 160)
    assert summary.last_ts == NOW - 1000  # entrada em servidor não conta como atividade
    assert [(c.id, c.name, c.count) for c in summary.top_text] == [(1, "geral", 2), (2, "música", 1)]
    assert [(c.id, c.count) for c in summary.top_voice] == [(7, 2), (8, 1)]


def test_summarize_shows_the_latest_name_of_a_renamed_channel():
    events = [event(5, text_id=1, text_name="antigo"), event(1, text_id=1, text_name="novo")]
    assert summarize(events, now_ms=NOW).top_text[0].name == "novo"


def test_summarize_recent_is_newest_first_capped_and_includes_guild_events():
    events = [event(n) for n in range(2, 40)] + [event(1, "guild", name="leave")]
    summary = summarize(events, now_ms=NOW)
    assert len(summary.recent) == usage.RECENT_EVENTS
    assert summary.recent[0]["kind"] == "guild"
    assert [e["ts"] for e in summary.recent] == sorted((e["ts"] for e in summary.recent), reverse=True)


def test_summarize_ignores_events_outside_the_window_and_malformed_ones():
    events = [
        event(1),
        {"id": "velho", "ts": NOW - 8 * MS_PER_DAY, "kind": "slash"},
        {"id": "futuro", "ts": NOW + 60_000, "kind": "slash"},
        {"lixo": True},
    ]
    assert summarize(events, now_ms=NOW).commands == 1


def test_summarize_empty_has_no_activity():
    summary = summarize([], now_ms=NOW)
    assert (summary.commands, summary.tracks, summary.last_ts, summary.recent) == (0, 0, None, ())


# ---------------------------------------------------------------------- banimento


def test_ban_decision_on_join_and_ready():
    banned = {10: object(), 30: object()}
    assert should_leave(10, banned) is True
    assert should_leave(20, banned) is False
    assert guilds_to_leave([10, 20, 30], banned) == [10, 30]
    assert guilds_to_leave([20], banned) == []
    assert guilds_to_leave([10], {}) == []


def test_new_ban_record_cleans_name_and_reason():
    record = new_ban_record(10, "\x1b[31mServidor\x1b[0m\n@everyone", "spam\r\nspam" + "x" * 500, NOW)
    assert record.name == "Servidor @everyone"
    assert record.reason.startswith("spam spam") and len(record.reason) <= usage.REASON_LIMIT
    assert record.banned_at == NOW


@pytest.mark.parametrize(
    ("doc", "guild_id"),
    [({"_id": "123", "name": "S", "banned_at": 5, "reason": "r"}, 123), ({"_id": 456}, 456)],
)
def test_ban_from_doc_reads_valid_documents(doc, guild_id):
    record = ban_from_doc(doc)
    assert isinstance(record, BanRecord) and record.guild_id == guild_id


@pytest.mark.parametrize("doc", [{}, {"_id": "abc"}, {"_id": "0"}, {"_id": True}, {"_id": None}, {"_id": [1]}])
def test_ban_from_doc_rejects_ids_that_are_not_guilds(doc):
    assert ban_from_doc(doc) is None


# ---------------------------------------------------------------------- configuração e rótulos


@pytest.mark.parametrize(
    ("raw", "days"),
    [(30, 30), ("7", 7), (0, 30), (-3, 30), ("abc", 30), (None, 30), (10_000, usage.MAX_RETENTION_DAYS)],
)
def test_clamp_retention_days(raw, days):
    assert clamp_retention_days(raw) == days


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("FINISHED", "finished"),
        ("TrackEndReason.REPLACED", "skipped"),
        ("LOAD_FAILED", "error"),
        ("STOPPED", "stopped"),
        ("CLEANUP", "stopped"),
        (None, "stopped"),
    ],
)
def test_end_reason(raw, label):
    assert end_reason(raw) == label


@pytest.mark.parametrize(
    ("names", "label"),
    [
        (["CommandOnCooldown", "CheckFailure"], "cooldown"),
        (["NotOwner", "CheckFailure"], "forbidden"),
        (["GenericError", "CheckFailure"], "rejected"),
        (["BadArgument", "UserInputError"], "invalid_input"),
        (["KeyError", "Exception"], "unexpected"),
    ],
)
def test_error_label_is_a_fixed_label_from_the_class_names(names, label):
    assert error_label(names) == label


@pytest.mark.regression
@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("- sala", "\\- sala"),
        ("1. sala", "1\\. sala"),
        ("12. sala", "12\\. sala"),
        ("-sala", "-sala"),  # sem espaço depois não é lista
        ("sala - dois", "sala - dois"),  # só o começo da linha vira lista
        ("3.5 sala", "3.5 sala"),
    ],
)
def test_render_text_keeps_a_leading_list_marker_as_text(raw, shown):
    """V-153-1: nome de canal ou servidor que começa com marcador de lista abria uma lista no embed quando vinha no começo da linha."""
    assert render_text(raw, 100) == shown
