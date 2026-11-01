"""O filtro de IP dos links que o Lavalink busca: texto que não é link passa; link que resolve para QUALQUER endereço não público é recusado,
com mensagem genérica ao usuário e só o host no log. Cobre a classificação do link (rules.urls.lavalink_target), o guard assíncrono
(utils.url_guard), o ponto do cog (music_cog.tracks) e a última barreira (wavelink.Node.get_tracks)."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.rules.urls import UrlTarget, lavalink_target
from empiremuse.utils.url_guard import UnsafeUrl, ensure_public_url

pytestmark = pytest.mark.critical

PUBLIC = "93.184.216.34"


def resolving(*addresses):
    async def resolve(host, port):
        return list(addresses)

    return resolve


async def must_not_resolve(host, port):
    raise AssertionError("não deveria resolver")


class TestTarget:
    @pytest.mark.parametrize(
        "query",
        [
            "ytsearch:http://x",
            "ytmsearch:something",
            "hello world",
            "",
            "   ",
            None,
            5,
            "ftp://x.example/a",
            "file:///etc/passwd",
        ],
    )
    def test_text_that_is_not_an_http_link_does_not_apply(self, query):
        assert lavalink_target(query) is None

    @pytest.mark.parametrize(
        ("query", "host"),
        [
            ("https://Example.COM./a?b=1", "example.com"),
            ("<https://example.com/p>", "example.com"),
            ("  HTTP://LOCALHOST:8080/x  ", "localhost"),
            ("http://[::ffff:127.0.0.1]/", "::ffff:127.0.0.1"),
            ("http://[64:ff9b::7f00:1]:2333/", "64:ff9b::7f00:1"),
            ("http://127.0.0.1:2333/r/abc", "127.0.0.1"),
            ("icy://radio.example:8000/live", "radio.example"),
            ("https://music.example.org/a b c", "music.example.org"),  # texto depois do 1º espaço não é o link
        ],
    )
    def test_the_host_is_extracted_lowercase_without_brackets_or_trailing_dot(self, query, host):
        assert lavalink_target(query) == UrlTarget(host, False)

    @pytest.mark.parametrize(
        "query",
        [
            "http://2130706433/",  # decimal (127.0.0.1)
            "http://0x7f000001/",  # hexadecimal
            "http://0x7f.1/",
            "http://127.1/",  # forma curta
            "http://010.0.0.1/",  # octal
            "http://017700000001/",
            "http://user@good.example/",  # credencial: cada parser lê o host de um jeito
            "http://good.example@127.0.0.1/",
            "http://good.example\\@127.0.0.1/",  # barra invertida
            "http://127.0.0.1\\.good.example/",
            "http://good.example\t.evil/",  # tab no meio: alguns parsers o apagam
            "http://good.example\r\n127.0.0.1/",
            "http://:80/",
            "http:///path",
            "http://good.example:99999/",
            "http://good.example:abc/",
        ],
    )
    def test_ambiguous_or_dangerous_syntax_is_refused_before_any_dns(self, query):
        target = lavalink_target(query)

        assert target is not None and target.refused is True


AMBIGUOUS_LINKS = [
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0x7f.1/",
    "http://127.1/",
    "http://010.0.0.1/",
    "http://017700000001/",
    "http://user@good.example/",
    "http://good.example@127.0.0.1/",
    "http://good.example\\@127.0.0.1/",
    "http://127.0.0.1\\.good.example/",
    "http://good.example\t.evil/",
    "http://good.example\r\n127.0.0.1/",
    "http://:80/",
    "http:///path",
    "http://good.example:99999/",
    "http://good.example:abc/",
    "http://good.example @127.0.0.1/x",  # espaço antes do @: o Lavalink recebe o texto inteiro
    "http://good.example /127.0.0.1/x",
    "icy://good.example @10.0.0.1/",
]


class TestSyntaxLayerInTheGuard:
    """A recusa por sintaxe vale no guard mesmo se o DNS devolver um IP PÚBLICO (os hosts da lista seriam resolvidos como públicos)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", AMBIGUOUS_LINKS)
    async def test_ambiguous_or_dangerous_syntax_is_refused_even_when_dns_says_public(self, query):
        with pytest.raises(UnsafeUrl):
            await ensure_public_url(query, resolve=resolving(PUBLIC))

    @pytest.mark.asyncio
    async def test_a_link_followed_by_plain_text_still_passes(self):
        await ensure_public_url("https://music.example.org/a.mp3 uma música legal", resolve=resolving(PUBLIC))

    @pytest.mark.asyncio
    async def test_the_default_dns_deadline_is_five_seconds(self, monkeypatch):
        from empiremuse.utils import url_guard

        seen = []
        real = asyncio.wait_for

        async def recording(awaitable, timeout):
            seen.append(timeout)
            return await real(awaitable, timeout)

        monkeypatch.setattr(url_guard.asyncio, "wait_for", recording)

        await ensure_public_url("https://music.example.org/a", resolve=resolving(PUBLIC))

        assert seen == [5.0]

    def test_trusted_local_is_removed_from_the_kwargs_and_never_reaches_the_load(self):
        from empiremuse.wavelink.node import take_trusted_local

        kwargs = {"trusted_local": True, "track_cls": object}
        assert take_trusted_local(kwargs) is True and kwargs == {"track_cls": object}

        untouched = {"track_cls": object}
        assert take_trusted_local(untouched) is False and untouched == {"track_cls": object}

        user_made = {"trusted_local": "sim"}  # qualquer valor verdadeiro liga: por isso só código interno o passa
        assert take_trusted_local(user_made) is True and user_made == {}


class TestGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["ytsearch:lofi", "uma música qualquer", "scsearch:x http://127.0.0.1"])
    async def test_text_that_is_not_a_link_never_resolves_anything(self, query):
        await ensure_public_url(query, resolve=must_not_resolve)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "127.0.0.53",
            "::1",
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.0.1",
            "169.254.169.254",  # metadados da nuvem
            "169.254.0.1",
            "100.64.0.1",  # CGNAT
            "100.127.255.254",
            "0.0.0.0",
            "224.0.0.1",  # multicast
            "239.255.255.250",
            "255.255.255.255",
            "192.0.0.1",
            "198.18.0.1",
            "240.0.0.1",
            "fe80::1",  # link-local v6
            "fc00::1",  # ULA
            "fd12:3456::1",
            "ff02::1",  # multicast v6
            "::",
            "::ffff:127.0.0.1",  # IPv4 mapeado
            "::ffff:10.0.0.1",
            "64:ff9b::7f00:1",  # NAT64
            "64:ff9b::a00:1",
            "2002:7f00:1::",  # 6to4
            "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
            "not-an-ip",
        ],
    )
    async def test_a_name_that_resolves_to_a_non_public_address_is_refused(self, address):
        with pytest.raises(UnsafeUrl):
            await ensure_public_url("https://radio.example.org/live", resolve=resolving(address))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "[::1]",
            "10.1.2.3",
            "169.254.169.254",
            "100.64.0.1",
            "[::ffff:127.0.0.1]",
            "[64:ff9b::7f00:1]",
            "224.0.0.1",
            "0.0.0.0",
        ],
    )
    async def test_a_non_public_ip_literal_is_refused_without_resolving(self, host):
        with pytest.raises(UnsafeUrl):
            await ensure_public_url(f"http://{host}:2333/r/token", resolve=must_not_resolve)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["http://[2606:4700:4700::1111]/x", f"https://{PUBLIC}/x"])
    async def test_a_public_ip_literal_passes(self, query):
        await ensure_public_url(query, resolve=must_not_resolve)

    @pytest.mark.asyncio
    async def test_a_public_name_passes(self):
        await ensure_public_url("https://music.example.org/a.mp3", resolve=resolving(PUBLIC, "2606:4700:4700::1111"))

    @pytest.mark.asyncio
    async def test_one_private_answer_among_public_ones_refuses_the_whole_name(self):  # rebinding por várias respostas
        with pytest.raises(UnsafeUrl):
            await ensure_public_url("https://music.example.org/a", resolve=resolving(PUBLIC, "10.0.0.1"))

    @pytest.mark.asyncio
    async def test_localhost_and_internal_names_are_refused_by_what_they_resolve_to(self):
        for name, address in (("localhost", "127.0.0.1"), ("lavalink", "172.18.0.2"), ("mongo.internal", "10.0.0.8")):
            with pytest.raises(UnsafeUrl):
                await ensure_public_url(f"http://{name}:2333/x", resolve=resolving(address))

    @pytest.mark.asyncio
    async def test_dns_failures_empty_answers_and_timeouts_refuse_closed(self):
        import socket

        async def nxdomain(host, port):
            raise socket.gaierror("Name or service not known")

        async def hangs(host, port):
            await asyncio.Event().wait()  # nunca responde

        for resolve in (nxdomain, resolving()):
            with pytest.raises(UnsafeUrl):
                await ensure_public_url("https://music.example.org/a", resolve=resolve)
        with pytest.raises(UnsafeUrl):  # o prazo é injetado: sem esperar tempo real
            await ensure_public_url("https://music.example.org/a", resolve=hangs, timeout=0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "host", ["música.example", "straße.example", "ｅｘａｍｐｌｅ.com", "exаmple.com", "good.example。evil.example"]
    )
    async def test_a_non_ascii_host_is_refused_without_resolving(
        self, host
    ):  # V184 L2: Python (IDNA2003) e Java podem ler outro nome
        with pytest.raises(UnsafeUrl):
            await ensure_public_url(f"https://{host}/a", resolve=must_not_resolve)

    @pytest.mark.asyncio
    async def test_a_punycode_host_is_plain_ascii_and_passes_when_public(self):
        await ensure_public_url("https://xn--msica-7ua.example/a", resolve=resolving(PUBLIC))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scheme", ["icy", "ICY", "Icy"])
    async def test_icy_links_are_treated_as_http(self, scheme):  # V184 L1: o lavaplayer converte icy:// em http://
        with pytest.raises(UnsafeUrl):
            await ensure_public_url(f"{scheme}://127.0.0.1:8000/stream", resolve=must_not_resolve)
        with pytest.raises(UnsafeUrl):
            await ensure_public_url(f"{scheme}://radio.example/stream", resolve=resolving("10.0.0.1"))
        await ensure_public_url(f"{scheme}://radio.example/stream", resolve=resolving(PUBLIC))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            " icy://127.0.0.1:2333/x ",  # espaços ao redor
            "<icy://10.0.0.1/>",  # entre < > (sem prévia no Discord)
            "icy://user@good.example/",  # credencial
            "icy://127.1/",  # forma curta
            "icy://2130706433/",  # decimal
            "icy://[::ffff:127.0.0.1]:2333/",
            "icy://localhost:2333/",
        ],
    )
    async def test_every_spelling_of_an_icy_link_to_an_internal_host_is_refused(
        self, query
    ):  # Keystone ALTO: bypass por icy://
        with pytest.raises(UnsafeUrl):
            await ensure_public_url(query, resolve=resolving("127.0.0.1"))

    @pytest.mark.asyncio
    async def test_the_system_resolver_runs_on_its_own_small_executor_not_the_default_one(self, monkeypatch):
        import socket
        import threading

        from empiremuse.utils import url_guard

        seen = []

        def fake_getaddrinfo(host, port, **kwargs):
            seen.append(threading.current_thread().name)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]

        monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)

        assert await url_guard.system_resolve("music.example.org", 0) == [PUBLIC]
        assert seen and seen[0].startswith("url-guard-dns")  # nunca uma thread do executor padrão do laço

    @pytest.mark.asyncio
    async def test_the_refusal_carries_only_the_host(self):
        with pytest.raises(UnsafeUrl) as refusal:
            await ensure_public_url("http://secret.internal:2333/r/TOKEN?token=SEGREDO", resolve=resolving("10.0.0.1"))

        assert refusal.value.host == "secret.internal" and "SEGREDO" not in repr(refusal.value) + str(refusal.value)


class TestCogEntryPoint:
    """O ``Music.get_tracks`` (e com ele /play, prefixo, pedido digitado, painel, favoritos): recusa genérica e nada chega ao nó."""

    def cog(self):
        translate = lambda key, **kw: {"play.url_refused": "Não consigo carregar esse link."}.get(key, key)  # noqa: E731
        i18n = SimpleNamespace(for_guild=AsyncMock(return_value=translate))
        nodes = SimpleNamespace(values=lambda: pytest.fail("nenhum nó pode ser consultado"))
        return SimpleNamespace(
            bot=SimpleNamespace(pool=SimpleNamespace(i18n=i18n, last_fm=None), music=SimpleNamespace(nodes=nodes))
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            "http://127.0.0.1:2333/v4/stats",
            "http://localhost:2333/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::ffff:127.0.0.1]/",
            "http://10.0.0.5/a.mp3",
            "http://2130706433/",
            "http://good.example@127.0.0.1/",
            "icy://127.0.0.1:2333/x",
            "icy://169.254.169.254/",
            "ICY://10.0.0.5:8000/live",
        ],
    )
    async def test_an_unsafe_link_gets_a_generic_message_and_never_reaches_a_node(self, query, caplog):
        from empiremuse.cogs.music import Music
        from empiremuse.utils.music.errors import GenericError

        ctx = SimpleNamespace(guild=SimpleNamespace(id=1, preferred_locale="pt-BR"))

        with caplog.at_level(logging.WARNING), pytest.raises(GenericError) as refusal:
            await Music.get_tracks(self.cog(), query, ctx, SimpleNamespace(id=1))

        assert refusal.value.text == "Não consigo carregar esse link."
        for leak in ("127.0.0.1", "169.254", "localhost", "meta-data", "2333"):
            assert leak not in refusal.value.text  # nada do host nem do motivo para quem pediu
        assert (
            "meta-data" not in caplog.text and "/v4/stats" not in caplog.text
        )  # o log leva só o host, nunca o caminho

    @pytest.mark.asyncio
    async def test_the_log_has_only_the_host(self, caplog):
        from empiremuse.cogs.music import Music
        from empiremuse.utils.music.errors import GenericError

        ctx = SimpleNamespace(guild=None)

        with caplog.at_level(logging.WARNING), pytest.raises(GenericError):
            await Music.get_tracks(self.cog(), "http://10.9.8.7:2333/r/TOKEN?token=SEGREDO", ctx, SimpleNamespace(id=1))

        assert "10.9.8.7" in caplog.text and "SEGREDO" not in caplog.text and "TOKEN" not in caplog.text


class TestNodeBackstop:
    """A última barreira: mesmo um caminho que não passe pelo cog (autoqueue, favoritos antigos, código novo) não chega ao ``loadtracks``."""

    @pytest.mark.asyncio
    async def test_a_loopback_link_never_reaches_the_rest_call(self):
        from empiremuse.wavelink.errors import TrackNotFound
        from empiremuse.wavelink.node import Node

        touched = []
        node = SimpleNamespace(session=SimpleNamespace(get=lambda *a, **k: touched.append("rest")))

        with pytest.raises(TrackNotFound):
            await Node.get_tracks(node, "http://127.0.0.1:2333/r/token")

        assert touched == []

    @pytest.mark.asyncio
    async def test_the_trusted_local_flag_is_only_for_code_that_owns_the_local_address(self):
        from empiremuse.wavelink.node import Node

        node = SimpleNamespace()  # sem ``session``: passar da barreira é bater neste AttributeError

        with pytest.raises(AttributeError):
            await Node.get_tracks(node, "http://127.0.0.1:2333/r/token", trusted_local=True)

    @pytest.mark.asyncio
    async def test_searches_by_name_are_not_filtered(self):
        from empiremuse.wavelink.node import Node

        node = SimpleNamespace()

        with pytest.raises(AttributeError):  # passou da barreira (não é link) e só quebrou no nó falso
            await Node.get_tracks(node, "ytsearch:lofi")


class TestTheSentTrackIsTheOneChecked:
    """V184-1 + CodeRabbit: toda troca de faixa passa pelo ``Node.update_player``, e o que se confere é o ``encodedTrack`` que VAI no PATCH
    (decodificado pelo nó), não o ``player.current``: os dois podem divergir (id temporário, faixa resolvida por busca, fila ou sessão guardadas)."""

    def make_node(
        self, *, current_source="youtube", current_uri="https://www.youtube.com/watch?v=x", decoded=None, error=None
    ):
        from collections import OrderedDict

        from cachetools import TTLCache

        from empiremuse.wavelink.node import Node

        node = object.__new__(Node)
        node.players = {
            7: SimpleNamespace(current=SimpleNamespace(info={"sourceName": current_source, "uri": current_uri}))
        }
        node.session_id = None  # passar da barreira é bater em MissingSessionID
        node._client = SimpleNamespace(bot=SimpleNamespace(music=SimpleNamespace(players={})))
        node._trusted_encoded = OrderedDict()
        node._checked_encoded = TTLCache(maxsize=64, ttl=600)
        if error is not None:
            node.build_track = AsyncMock(side_effect=error)
        else:
            node.build_track = AsyncMock(return_value=SimpleNamespace(info=decoded))
        return node

    @staticmethod
    def info(source, uri):
        return {"sourceName": source, "uri": uri, "title": "t"}

    async def play(self, node, encoded="ENC1", **data):
        from empiremuse.wavelink.node import Node

        await Node.update_player(node, 7, {"encodedTrack": encoded, **data}, True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "uri",
        [
            "http://127.0.0.1:2333/v4/stats",
            "http://localhost:2333/x",
            "https://169.254.169.254/latest/meta-data/",
            "http://[::ffff:127.0.0.1]/",
            "icy://10.0.0.5:8000/live",
            "http://good.example@127.0.0.1/",
        ],
    )
    async def test_a_sent_http_track_to_an_internal_address_is_refused_even_if_the_player_thinks_it_plays_something_else(
        self, uri
    ):
        from empiremuse.wavelink.errors import TrackNotFound

        node = self.make_node(current_source="youtube", decoded=self.info("http", uri))  # current e enviado DIVERGEM

        with pytest.raises(TrackNotFound):
            await self.play(node)

    @pytest.mark.asyncio
    async def test_what_is_validated_is_the_sent_track_not_the_current_one(self):
        from empiremuse.wavelink.errors import MissingSessionID

        stale = self.make_node(
            current_source="http",
            current_uri="http://127.0.0.1:2333/x",
            decoded=self.info("youtube", "https://www.youtube.com/watch?v=ok"),
        )

        with pytest.raises(MissingSessionID):  # o current é interno, mas o enviado é o YouTube: passa
            await self.play(stale)

    @pytest.mark.asyncio
    async def test_a_sent_http_track_to_a_public_address_goes_on_and_the_decision_is_cached(self):
        from empiremuse.wavelink.errors import MissingSessionID

        node = self.make_node(decoded=self.info("http", f"https://{PUBLIC}/a.mp3"))

        for _ in range(3):
            with pytest.raises(MissingSessionID):
                await self.play(node)

        assert node.build_track.await_count == 1  # decodificou uma vez; as outras vêm do cache

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error", [OSError("nó fora"), TimeoutError(), ValueError("lixo")], ids=["os_error", "timeout", "bad_payload"]
    )
    @pytest.mark.parametrize("current_source", ["http", "youtube"])
    async def test_when_the_track_cannot_be_decoded_nothing_is_sent_fail_closed(self, error, current_source, caplog):
        from empiremuse.wavelink.errors import TrackNotFound

        node = self.make_node(current_source=current_source, error=error)

        with caplog.at_level(logging.WARNING), pytest.raises(TrackNotFound):
            await self.play(node)

        assert type(error).__name__ in caplog.text and "ENC1" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_decoded_track_without_info_is_refused(self):
        from empiremuse.wavelink.errors import TrackNotFound

        with pytest.raises(TrackNotFound):
            await self.play(self.make_node(decoded=None))

    @pytest.mark.asyncio
    async def test_a_refused_track_is_not_cached_as_allowed(self):
        from empiremuse.wavelink.errors import TrackNotFound

        node = self.make_node(decoded=self.info("http", "http://10.0.0.1/a"))
        for _ in range(2):
            with pytest.raises(TrackNotFound):
                await self.play(node)

        assert node.build_track.await_count == 2

    @pytest.mark.asyncio
    async def test_tracks_loaded_by_the_trusted_local_caller_play_without_decoding(self):
        from empiremuse.wavelink.errors import MissingSessionID

        node = self.make_node(
            decoded=self.info("http", "http://127.0.0.1:2333/r/token")
        )  # decodificada, seria recusada
        node._get_tracks = AsyncMock(return_value=[SimpleNamespace(id="LIVE")])

        loaded = await type(node).get_tracks(node, "http://127.0.0.1:2333/r/token", trusted_local=True)

        assert [t.id for t in loaded] == ["LIVE"]
        with pytest.raises(MissingSessionID):
            await self.play(node, "LIVE")
        node.build_track.assert_not_awaited()
        with pytest.raises(
            Exception
        ) as other:  # outra faixa com o mesmo endereço, que ninguém confiou, continua recusada
            await self.play(node, "OTHER")
        assert type(other.value).__name__ == "TrackNotFound"

    @pytest.mark.asyncio
    async def test_tracks_loaded_through_the_guard_play_without_decoding(self):
        from empiremuse.wavelink.errors import MissingSessionID

        node = self.make_node(decoded=self.info("http", "http://127.0.0.1/"))
        node._get_tracks = AsyncMock(
            return_value=SimpleNamespace(tracks=[SimpleNamespace(id="A"), SimpleNamespace(id="B")])
        )

        await type(node).get_tracks(node, "ytsearch:lofi")

        for encoded in ("A", "B"):
            with pytest.raises(MissingSessionID):
                await self.play(node, encoded)
        node.build_track.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_trusted_set_is_bounded(self):
        from empiremuse.wavelink.node import TRUSTED_TRACKS_MAX

        node = self.make_node()

        node._trust_loaded([SimpleNamespace(id=f"T{n}") for n in range(TRUSTED_TRACKS_MAX + 40)])

        assert len(node._trusted_encoded) == TRUSTED_TRACKS_MAX and "T0" not in node._trusted_encoded
        assert f"T{TRUSTED_TRACKS_MAX + 39}" in node._trusted_encoded

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", [{"paused": True, "volume": 50}, {"encodedTrack": None}, {"encodedTrack": ""}])
    async def test_a_payload_without_a_new_track_is_not_checked(self, data):
        from empiremuse.wavelink.errors import MissingSessionID
        from empiremuse.wavelink.node import Node

        node = self.make_node(decoded=self.info("http", "http://127.0.0.1/"))

        with pytest.raises(MissingSessionID):
            await Node.update_player(node, 7, data, True)

        node.build_track.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_refusal_logs_only_the_host(self, caplog):
        from empiremuse.wavelink.errors import TrackNotFound

        node = self.make_node(decoded=self.info("http", "http://10.9.8.7:2333/r/TOKEN?token=SEGREDO"))

        with caplog.at_level(logging.WARNING), pytest.raises(TrackNotFound):
            await self.play(node)

        assert "10.9.8.7" in caplog.text and "SEGREDO" not in caplog.text and "TOKEN" not in caplog.text
