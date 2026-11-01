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
    async def test_dns_failures_empty_answers_and_timeouts_refuse_closed(self, monkeypatch):
        import socket

        from empiremuse.utils import url_guard

        async def nxdomain(host, port):
            raise socket.gaierror("Name or service not known")

        async def hangs(host, port):
            await asyncio.sleep(10)

        monkeypatch.setattr(url_guard, "RESOLVE_TIMEOUT_S", 0.1)

        for resolve in (nxdomain, resolving(), hangs):
            with pytest.raises(UnsafeUrl):
                await ensure_public_url("https://music.example.org/a", resolve=resolve)

    @pytest.mark.asyncio
    async def test_an_internationalized_host_is_resolved_in_ascii(self):
        seen = []

        async def resolve(host, port):
            seen.append(host)
            return [PUBLIC]

        await ensure_public_url("https://música.example/a", resolve=resolve)

        assert seen == ["xn--msica-7ua.example"]

    @pytest.mark.asyncio
    async def test_a_host_that_cannot_be_encoded_is_refused(self):
        with pytest.raises(UnsafeUrl):
            await ensure_public_url("https://" + "a" * 70 + ".example/x", resolve=resolving(PUBLIC))

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
