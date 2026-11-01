"""Estações do Radio Browser e o fluxo (item 8, R3, R-7 e R-8): estação aceita, URL, tipo de conteúdo, assinatura, playlist e termo de busca."""

import time

import pytest

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio_stations import (
    PLAYLIST_MAX_ENTRIES,
    Station,
    check_search_term,
    check_station,
    check_stream_url,
    content_type_ok,
    favicon_thumbnail,
    looks_like_playlist,
    parse_playlist,
    safe_station_url_for_log,
    signature_ok,
    valid_mirror,
    valid_uuid,
)

pytestmark = pytest.mark.critical

UUID = "960e57c5-1c3d-4d7e-8c2b-1f3a6e0a9b11"


def station(**over):
    base = {
        "stationuuid": UUID,
        "name": "Rádio Boa",
        "url_resolved": "https://stream.example.org:8443/live.mp3",
        "codec": "MP3",
        "bitrate": 128,
        "hls": 0,
        "country": "Brazil",
        "homepage": "https://evil.example",
        "favicon": "https://x.example/f.png",
    }
    return {**base, **over}


class TestMirror:
    @pytest.mark.parametrize(
        "name", ["de1.api.radio-browser.info", "a-b-2.api.radio-browser.info", "all.api.radio-browser.info"]
    )
    def test_the_official_pattern_is_accepted(self, name):
        assert valid_mirror(name)

    @pytest.mark.parametrize(
        "name",
        [
            "evil.example",
            "radio-browser.info",
            "api.radio-browser.info",
            "de1.api.radio-browser.info.evil.example",
            "evil.example/de1.api.radio-browser.info",
            "DE1.api.radio-browser.info",
            "de1.api.radio-browser.info\n",
            "dé1.api.radio-browser.info",
            "1.2.3.4",
            "",
            None,
        ],
    )
    def test_anything_else_is_dropped(self, name):
        assert not valid_mirror(name)


class TestSearchTerm:
    def test_two_to_sixty_four_characters(self):
        assert check_search_term(" rock ") == "rock" and check_search_term("ab") == "ab"
        assert check_search_term("x" * 64) == "x" * 64

    @pytest.mark.parametrize("bad", ["", "a", " a ", "x" * 65, "a\nb", "a\x00b", None, 5, ["rock"]])
    def test_outside_that_it_is_refused(self, bad):
        assert isinstance(check_search_term(bad), RuleError)


class TestStreamUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://stream.example.org/live",
            "https://stream.example.org/live.mp3",
            "http://stream.example.org:8000/live",
            "https://stream.example.org:8999/x",
            "http://stream.example.org:80/x",
            "https://stream.example.org:443/x",
        ],
    )
    def test_allowed(self, url):
        assert check_stream_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://x.example/live",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://user:pass@x.example/live",
            "http://user@x.example/live",
            "http://x.example:22/live",
            "http://x.example:7999/live",
            "http://x.example:9000/live",
            "http://x.example:2333/live",
            "http://x.example:27017/",
            "http:///nohost",
            "//x.example/live",
            "http://x.example/a b",
            "http://x.example/\n",
            "http://x.example:99999/",
            "x" * 3000,
            "",
            None,
            5,
        ],
    )
    def test_refused(self, url):
        assert check_stream_url(url) is None


class TestCheckStation:
    def test_a_good_station_is_accepted_and_the_homepage_is_not_kept(self):
        got = check_station(station())

        assert isinstance(got, Station) and got.uuid == UUID and got.codec == "MP3" and got.bitrate == 128
        assert "evil.example" not in repr(got)

    @pytest.mark.parametrize("codec", ["MP3", "AAC", "AAC+", "OGG", "mp3", " aac "])
    def test_the_four_codecs(self, codec):
        assert isinstance(check_station(station(codec=codec)), Station)

    @pytest.mark.parametrize("codec", ["FLAC", "UNKNOWN", "", None, "MP3;evil", 5, "AACP"])
    def test_other_codecs_are_refused(self, codec):
        assert check_station(station(codec=codec)).code == "station_codec"

    @pytest.mark.parametrize("hls", [1, "1", 2, True])
    def test_hls_is_out_of_this_release(self, hls):
        assert check_station(station(hls=hls)).code == "station_hls"

    def test_hls_zero_in_any_shape_is_fine(self):
        for ok in (0, "0"):
            assert isinstance(check_station(station(hls=ok)), Station)

    @pytest.mark.parametrize("bitrate", [321, 10**6, -1, "abc", None, True, 1.5, [128]])
    def test_the_bitrate_is_at_most_320(self, bitrate):
        assert check_station(station(bitrate=bitrate)).code == "station_bitrate"

    def test_the_bitrate_as_text_and_zero_are_accepted(self):
        assert check_station(station(bitrate="128")).bitrate == 128 and check_station(station(bitrate=0)).bitrate == 0

    @pytest.mark.parametrize(
        "uuid", ["x", "", None, "960E57C5-1C3D-4D7E-8C2B-1F3A6E0A9B11", UUID + "0", "../../etc", 5]
    )
    def test_an_invalid_uuid_is_refused(self, uuid):
        assert check_station(station(stationuuid=uuid)).code == "station_uuid"

    @pytest.mark.parametrize(
        "url", ["http://user:pw@x.example/a", "http://x.example:2333/a", "ftp://x.example/a", None]
    )
    def test_an_unsafe_url_is_refused(self, url):
        assert check_station(station(url_resolved=url)).code == "station_url"

    def test_the_text_goes_through_the_single_external_text_function(self):
        got = check_station(station(name="@everyone <@1> https://evil.example/x\n", country="discord.gg/abc"))

        assert "@everyone" not in got.name and "<@1>" not in got.name and "://" not in got.name and "\n" not in got.name
        assert "discord.gg" not in got.country

    def test_a_huge_name_is_cut(self):
        assert len(check_station(station(name="n" * 10_240)).name) <= 100

    def test_not_a_mapping_is_refused(self):
        assert check_station([1, 2]).code == "station_invalid" and check_station(None).code == "station_invalid"

    def test_the_uuid_helper(self):
        assert valid_uuid(UUID) and not valid_uuid("nope") and not valid_uuid(None)


class TestFavicon:
    def test_https_and_clean_only(self):
        assert favicon_thumbnail("https://cdn.example/f.png") == "https://cdn.example/f.png"
        for bad in (
            "http://cdn.example/f.png",
            "javascript:x",
            "https://u:p@cdn.example/f.png",
            "https://x.example/<a>",
            None,
            5,
        ):
            assert favicon_thumbnail(bad) is None


class TestContentTypeAndSignature:
    @pytest.mark.parametrize(
        "header",
        [
            "audio/mpeg",
            "audio/aac",
            "audio/aacp",
            "audio/ogg",
            "application/ogg",
            "AUDIO/MPEG; charset=utf-8",
            " audio/mpeg ",
        ],
    )
    def test_audio_types_pass(self, header):
        assert content_type_ok(header)

    @pytest.mark.parametrize(
        "header",
        [
            "audio/x-scpls",
            "audio/x-mpegurl",
            "application/xspf+xml",
            "text/html",
            "text/plain",
            "application/octet-stream",
            "audio/mp4",
            "",
            None,
        ],
    )
    def test_playlists_html_and_the_rest_do_not(self, header):
        assert not content_type_ok(header)

    def test_mp3_needs_the_mpeg_sync(self):
        assert signature_ok("MP3", b"\xff\xfb\x90\x00") and signature_ok("mp3", b"\xff\xf3\x00")
        assert (
            not signature_ok("MP3", b"[playlist]")
            and not signature_ok("MP3", b"\xff\x00")
            and not signature_ok("MP3", b"ID3\x03")
        )

    def test_aac_needs_adts(self):
        assert signature_ok("AAC", b"\xff\xf1\x50\x80") and signature_ok("AAC+", b"\xff\xf9\x50")
        assert not signature_ok("AAC", b"\xff\xe0\x00")

    def test_ogg_needs_oggs(self):
        assert signature_ok("OGG", b"OggS\x00\x02") and not signature_ok("OGG", b"Ogg")

    @pytest.mark.parametrize(
        "body",
        [
            b"[playlist]\nFile1=http://mongo:27017/",
            b"#EXTM3U\nhttp://127.0.0.1:2333/",
            b"<?xml version",
            b"<html>",
            b"",
            b"\x00\x01",
        ],
    )
    def test_a_playlist_a_page_or_nothing_never_matches_any_codec(self, body):
        for codec in ("MP3", "AAC", "OGG"):
            assert not signature_ok(codec, body)

    def test_an_unknown_codec_never_matches(self):
        assert not signature_ok("FLAC", b"\xff\xfb\x90") and not signature_ok(None, b"\xff\xfb")


class TestPlaylist:
    def test_a_pls(self):
        body = b"[playlist]\nNumberOfEntries=2\nFile1=http://a.example:8000/live\nTitle1=A\nFile2=https://b.example/x.mp3\n"

        assert parse_playlist(body) == ["http://a.example:8000/live", "https://b.example/x.mp3"]

    def test_an_m3u_skips_comments_and_hls(self):
        body = b"#EXTM3U\n#EXTINF:-1,A\nhttp://a.example/live\nhttp://b.example/list.m3u8\nhttp://c.example/y?x=1\n"

        assert parse_playlist(body) == ["http://a.example/live", "http://c.example/y?x=1"]

    def test_entries_to_internal_things_are_filtered_by_url_rules(self):
        body = b"File1=http://mongo:27017/\nFile2=http://u:p@x.example/\nFile3=ftp://x.example/\nFile4=http://ok.example/s\n"

        assert parse_playlist(body) == ["http://ok.example/s"]

    def test_only_ten_entries_and_16_kib_are_read(self):
        many = "\n".join(f"http://h{n}.example/s" for n in range(50)).encode()
        late = b"#" * (16 * 1024) + b"\nhttp://late.example/s\n"

        assert len(parse_playlist(many)) == PLAYLIST_MAX_ENTRIES
        assert parse_playlist(late) == []

    def test_garbage_and_huge_input_are_fast_and_empty(self):
        started = time.perf_counter()

        assert parse_playlist(b"\xff" * 100_000) == [] and parse_playlist(b"[" * 100_000) == []
        assert time.perf_counter() - started < 1

    def test_which_urls_look_like_playlists(self):
        assert looks_like_playlist("http://a.example/radio.pls") and looks_like_playlist("http://a.example/r.M3U?x=1")
        assert not looks_like_playlist("http://a.example/live.mp3") and not looks_like_playlist(
            "http://a.example/live.m3u8"
        )
        assert looks_like_playlist("http://a.example/live", "audio/x-scpls") and not looks_like_playlist(
            "http://a.example/live", "audio/mpeg"
        )


class TestLogs:
    def test_only_the_host_goes_to_the_log(self):
        out = safe_station_url_for_log("https://Stream.Example.org:8443/live.mp3?token=SEGREDO")

        assert out == "stream.example.org" and "SEGREDO" not in out and "live" not in out
        assert safe_station_url_for_log("http://[bad") == "?"
