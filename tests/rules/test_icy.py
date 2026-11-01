"""Leitor ICY (item 8, R3, R-9): o áudio sai limpo, o título sai tratado e nada vindo da estação passa dos limites."""

import re
import time

import pytest

from empiremuse.rules.icy import (
    MAX_BLOCK,
    TITLE_INTERVAL_S,
    IcyStripper,
    TitleThrottle,
    check_metaint,
    clean_title,
    decode_block,
    parse_title,
)

pytestmark = pytest.mark.critical


def block(title: str | bytes, *, pad_to=None) -> bytes:
    raw = title if isinstance(title, bytes) else f"StreamTitle='{title}';".encode()
    size = pad_to if pad_to is not None else -(-len(raw) // 16) * 16
    return bytes([size // 16]) + raw.ljust(size, b"\x00")


def stream(metaint: int, parts: list[bytes], *, meta: list[bytes]) -> bytes:
    """Áudio em blocos de ``metaint`` bytes, cada um seguido do bloco de metadados dado (ou de um bloco vazio)."""
    out = b""
    for i, part in enumerate(parts):
        out += part + (meta[i] if i < len(meta) else b"\x00")
    return out


class TestMetaint:
    @pytest.mark.parametrize(
        "raw, expected", [("1", 1), ("8192", 8192), ("65536", 65536), (" 16000 ", 16000), (b"4096", 4096)]
    )
    def test_the_valid_range(self, raw, expected):
        assert check_metaint(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["0", "-1", "65537", "1000000000", "", "abc", "12 34", "1e3", None, 5, "０５", "9" * 7, b"\xff"]
    )
    def test_anything_else_means_no_parsing(self, raw):
        assert check_metaint(raw) is None


class TestParseTitle:
    def test_a_plain_title(self):
        assert parse_title(b"StreamTitle='Artista - Faixa';StreamUrl='';") == "Artista - Faixa"

    def test_a_title_with_a_quote_inside_keeps_it(self):
        assert parse_title(b"StreamTitle='Rock 'n' Roll';StreamUrl='';") == "Rock 'n' Roll"

    def test_no_title_tag_or_an_empty_title_is_none(self):
        assert parse_title(b"StreamUrl='x';") is None and parse_title(b"StreamTitle='';") is None
        assert parse_title(b"") is None and parse_title(b"\x00" * 64) is None

    def test_utf8_then_latin1_and_no_nul(self):
        assert parse_title("StreamTitle='Ação – São';".encode()) == "Ação – São"
        assert parse_title("StreamTitle='Ação';".encode("latin-1")) == "Ação"
        assert parse_title(b"StreamTitle='a\x00b\x00';\x00\x00") == "ab"
        assert decode_block(b"\xff\xfe") == "ÿþ"

    def test_a_full_4080_byte_block_without_a_closing_quote_is_fast_and_bounded(self):
        hostile = b"StreamTitle='" + b"a" * (MAX_BLOCK - 13)

        started = time.perf_counter()
        title = parse_title(hostile)

        assert title is not None and len(title) <= MAX_BLOCK and time.perf_counter() - started < 0.5

    def test_an_oversized_block_is_cut_not_trusted(self):
        assert len(parse_title(b"StreamTitle='" + b"b" * 100_000) or "") <= MAX_BLOCK

    def test_a_pathological_input_does_not_hang(self):
        started = time.perf_counter()
        parse_title(b"StreamTitle='" + b"';" * 2000)

        assert time.perf_counter() - started < 0.5


class TestCleanTitle:
    def test_hostile_titles_are_neutralized_by_the_single_function(self):
        for hostile in ("@everyone", "<@123>", "https://x.y/z", "discord.gg/abc", "ｄｉｓｃｏｒｄ．ｇｇ/x"):
            out = clean_title(hostile)
            assert out and not re.search(r"@everyone|<@|://|discord\.gg", out)

    def test_the_limit_and_empties(self):
        assert len(clean_title("x" * 5000, 50)) <= 50 and clean_title(None) is None and clean_title("   ") is None


class TestStripper:
    def test_without_a_valid_metaint_the_stream_passes_untouched(self):
        s = IcyStripper(None)

        assert s.feed(b"\x01\x02StreamTitle='x';") == (b"\x01\x02StreamTitle='x';", [])

    def test_audio_comes_out_clean_and_the_title_is_extracted(self):
        audio = [b"A" * 8, b"B" * 8]
        data = stream(8, audio, meta=[block("Primeira"), block("Segunda")])

        out, titles = IcyStripper(8).feed(data)

        assert out == b"A" * 8 + b"B" * 8 and titles == ["Primeira", "Segunda"]

    @pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 64])
    def test_any_chunking_gives_the_same_result(self, size):
        audio = [bytes([65 + i]) * 10 for i in range(6)]
        data = stream(10, audio, meta=[block(f"T{i}") for i in range(6)])
        s = IcyStripper(10)
        out, titles = b"", []

        for i in range(0, len(data), size):
            a, t = s.feed(data[i : i + size])
            out += a
            titles += t

        assert out == b"".join(audio) and titles == [f"T{i}" for i in range(6)]

    def test_an_empty_metadata_block_costs_one_byte_and_no_title(self):
        out, titles = IcyStripper(4).feed(b"abcd\x00efgh\x00")

        assert out == b"abcdefgh" and titles == []

    def test_a_maximum_block_is_read_whole(self):
        data = b"a" * 4 + block(b"StreamTitle='" + b"z" * 100 + b"';", pad_to=MAX_BLOCK) + b"b" * 4

        out, titles = IcyStripper(4).feed(data)

        assert out == b"aaaabbbb" and titles == ["z" * 100]

    def test_the_metadata_never_reaches_the_audio_output(self):
        data = stream(5, [b"12345"], meta=[block("SEGREDO")])

        out, _ = IcyStripper(5).feed(data)

        assert b"StreamTitle" not in out and b"SEGREDO" not in out

    def test_a_hostile_icy_name_in_the_title_is_just_text(self):
        _, titles = IcyStripper(2).feed(b"ab" + block("icy-name: @everyone"))

        assert titles == ["icy-name: @everyone"]  # a limpeza R-11 é de quem publica; o parser só entrega texto cru

    def test_a_flood_of_blocks_is_linear(self):
        data = b"".join(b"a" + block("t") for _ in range(20_000))
        started = time.perf_counter()

        out, titles = IcyStripper(1).feed(data)

        assert len(out) == 20_000 and len(titles) == 20_000 and time.perf_counter() - started < 2


class TestTitleThrottle:
    def test_a_title_every_100_ms_publishes_one_every_15_seconds(self):
        throttle = TitleThrottle()
        published, now = [], 1000.0

        for n in range(600):  # 60 s com um título novo a cada 100 ms
            if (out := throttle.offer(f"faixa {n}", now)) is not None:
                published.append((now, out))
            now += 0.1

        assert 4 <= len(published) <= 5
        assert all(b[0] - a[0] >= TITLE_INTERVAL_S - 0.001 for a, b in zip(published, published[1:]))

    def test_the_same_title_is_never_published_twice_in_a_row(self):
        throttle = TitleThrottle()

        assert throttle.offer("A", 0.0) == "A"
        assert throttle.offer("A", 100.0) is None and throttle.offer("A", 200.0) is None

    def test_the_latest_title_waits_for_the_window_and_is_released_by_polling(self):
        throttle = TitleThrottle()
        throttle.offer("A", 0.0)

        assert throttle.offer("B", 5.0) is None
        assert throttle.poll(14.9) is None
        assert throttle.poll(15.0) == "B"

    def test_going_back_to_the_previous_title_is_a_change_but_the_current_one_is_not(self):
        throttle = TitleThrottle()
        throttle.offer("A", 0.0)
        throttle.offer("B", 20.0)

        assert throttle.offer("A", 40.0) == "A" and throttle.offer("A", 80.0) is None
