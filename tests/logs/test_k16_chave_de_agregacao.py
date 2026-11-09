"""Item 7 (P3, K16, #127) — ``extra={"aggregate_key": str}``: 7º campo OPCIONAL da chave do agregador.

Só ``str`` não vazia, cortada em 64 caracteres e passada por ``sanitize_value``; qualquer outro tipo (int, None, bytes, list, str vazia) vale como
ausente. Nunca é emitida. Sem o extra nada muda (B7/B8/K8). Os produtores: ``wavelink/node.py`` e ``music_cog/nodes.py`` (``node.identifier``) e
``player_events.py`` (``str(event.code)``): dois nós ou dois códigos diferentes não escondem um ao outro; o mesmo continua agregando.
``configure_logging`` real (console + arquivo), relógio injetado, nada dorme.
"""

import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import empiremuse.wavelink as wavelink
from empiremuse.logs.setup import configure_logging, flush_logging
from empiremuse.logs.settings import LogSettings
from empiremuse.music_cog.nodes import NodesMixin
from empiremuse.utils.music.models.player_events import PlayerEventsMixin
from empiremuse.wavelink import node as node_module
from tests.logs.apoio import arquivo_do_log, Relogio
from tests.logs.test_player_events import cog_de_nos, no_fisico
from tests.pool.test_logs_sem_segredo import node_falso

pytestmark = pytest.mark.critical

LOG = "empiremuse.wavelink"


class Destinos:
    def __init__(self, tmp_path) -> None:
        self.console = io.StringIO()
        self.relogio = Relogio()
        configure_logging(
            LogSettings(logging.INFO, False, True, frozenset(), 300),
            [],
            stream=self.console,
            logs_dir=tmp_path / ".logs",
            clock=self.relogio,
        )
        self.arquivo = arquivo_do_log(tmp_path / ".logs")

    def textos(self) -> list[str]:
        flush_logging()
        return [self.console.getvalue(), self.arquivo.read_text(encoding="utf-8")]


@pytest.fixture
def destinos(tmp_path):
    return Destinos(tmp_path)


def avisar(identificador, chave=..., nivel=logging.WARNING):
    extra = {} if chave is ... else {"aggregate_key": chave}
    logging.getLogger(LOG).log(nivel, "nó %s caiu", identificador, extra=extra or None)


# ============================================================================ o agregador


class TestChaveOpcional:
    def test_two_different_keys_with_the_same_template_both_come_out_full(self, destinos):
        avisar("no-1", "A")
        avisar("no-2", "B")

        for texto in destinos.textos():
            assert "nó no-1 caiu" in texto and "nó no-2 caiu" in texto and "repetida" not in texto

    def test_the_same_key_a_hundred_times_aggregates(self, destinos):
        for n in range(100):
            avisar(f"no-{n}", "A")

        for texto in destinos.textos():
            assert texto.count("repetida 99×") == 1 and texto.count("nó no-0 caiu") == 2

    @pytest.mark.parametrize("invalida", [7, None, b"bytes", ["lista"], ("t",), "", 3.5, True], ids=repr)
    def test_a_key_that_is_not_a_non_empty_str_is_ignored_like_no_key(self, destinos, invalida):
        avisar("no-1", invalida)
        avisar("no-2", invalida)
        avisar("no-3")

        for texto in destinos.textos():
            assert "nó no-1 caiu" in texto and "nó no-2 caiu" not in texto and "nó no-3 caiu" not in texto
            assert texto.count("repetida 2×") == 1

    def test_control_characters_in_the_key_are_sanitised_and_never_reach_the_output(self, destinos):
        avisar("no-1", "ch\x1b[2Jave\r\nX‮")
        avisar("no-2", "ch\x1b[2Jave\r\nX‮")

        for texto in destinos.textos():
            assert not any(c in texto for c in ("\x1b", "\r", "‮")) and "chave" not in texto
            assert "repetida 1×" in texto

    def test_the_key_never_appears_in_the_console_or_the_file(self, destinos):
        avisar("no-1", "SEGREDO-DA-CHAVE-Z9")
        avisar("no-2", "SEGREDO-DA-CHAVE-Z9")

        for texto in destinos.textos():
            assert "SEGREDO-DA-CHAVE-Z9" not in texto


# ============================================================================ produtores: nós


@pytest.mark.asyncio
class TestProdutoresDeNo:
    async def test_two_different_nodes_dropping_in_a_row_both_log(self, destinos):
        a, b = no_fisico("no-a"), no_fisico("no-b")
        cog = cog_de_nos(a, b)

        await NodesMixin.node_reconnect(cog, a)
        await NodesMixin.node_reconnect(cog, b)

        for texto in destinos.textos():
            assert "no-a" in texto and "no-b" in texto and "repetida" not in texto

    async def test_the_same_node_dropping_again_aggregates(self, destinos):
        node = no_fisico("no-instavel")
        cog = cog_de_nos(node)

        from datetime import datetime, timezone

        for _ in range(6):
            node._retry_dt, node._retry_count = datetime.fromtimestamp(0, timezone.utc), 0
            await NodesMixin.node_reconnect(cog, node)

        for texto in destinos.textos():
            assert "repetida 5×" in texto and texto.count("no-instavel") >= 2


# ============================================================================ produtor: canal de voz


def jogador_de_voz(canal: int):
    return SimpleNamespace(
        guild=SimpleNamespace(me=SimpleNamespace(voice=None)),
        last_channel=SimpleNamespace(id=canal, mention=f"<#{canal}>"),
        keep_connected=False,
        node=SimpleNamespace(identifier="N1"),
        bot=SimpleNamespace(is_ready=lambda: True),
        is_closing=True,
        destroy=AsyncMock(),
        guild_id=1,
    )


def fechou(codigo: int, jogador):
    return wavelink.WebsocketClosed({"player": jogador, "reason": "motivo", "code": codigo, "guildID": 1})


@pytest.mark.asyncio
class TestProdutorDeVoz:
    async def test_two_different_close_codes_both_log(self, destinos):
        jogador = jogador_de_voz(10)

        await PlayerEventsMixin.hook(jogador, fechou(4006, jogador))
        await PlayerEventsMixin.hook(jogador, fechou(4014, jogador))

        for texto in destinos.textos():
            assert "code 4006" in texto and "code 4014" in texto and "repetida" not in texto

    async def test_the_same_code_in_different_channels_aggregates(self, destinos):
        for canal in (10, 11, 12):
            jogador = jogador_de_voz(canal)
            await PlayerEventsMixin.hook(jogador, fechou(4006, jogador))

        for texto in destinos.textos():
            assert texto.count("code 4006") == 2 and "repetida 2×" in texto  # a 1ª e a linha do resumo
