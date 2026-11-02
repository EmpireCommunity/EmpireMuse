"""Item 7 (P3, Gambit) — o player, o nó e a busca no log: B12, B14, B14b e os eventos de nó do ``music_cog/nodes.py``.

``_he_dispatch`` roda com um player falso (``SimpleNamespace``) e eventos reais do ``wavelink``; o ``configure_logging`` é REAL (console em
``StringIO``). Nada dorme: o ``asyncio.sleep`` do módulo é dublado e o relógio do agregador é injetado.
"""

import asyncio
import io
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import empiremuse.wavelink as wavelink
from empiremuse.i18n.service import get_i18n
from empiremuse.logs.context import log_context
from empiremuse.logs.metrics import MetricsCollector
from empiremuse.logs.setup import configure_logging, flush_logging, tick_logging
from empiremuse.logs.settings import LogSettings
from empiremuse.music_cog.nodes import NodesMixin
from empiremuse.utils.client import BotPool
from empiremuse.utils.music.models import event_steps
from empiremuse.utils.music.models.event_steps import MIRROR_NOT_FOUND_CAUSES, _he_dispatch
from empiremuse.wavelink.node import Node
from empiremuse.wavelink.websocket import WebSocket
from tests.logs.apoio import Relogio

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

TITULO = "TITULO-DA-FAIXA-Q7"
CANAL_VOZ = 777000111
FONTE = "youtube"
DURACAO_MS = 215_000
INFO, DEBUG = logging.INFO, logging.DEBUG


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(event_steps.asyncio, "sleep", AsyncMock())


class Console:
    def __init__(self, nivel=INFO, guildas=()) -> None:
        self.saida = io.StringIO()
        self.relogio = Relogio()
        configure_logging(
            LogSettings(nivel, False, False, frozenset(guildas), 300), [], stream=self.saida, clock=self.relogio
        )

    @property
    def linhas(self) -> list[str]:
        return [linha for linha in self.saida.getvalue().splitlines() if linha and not linha.startswith("  │")]

    def do_player(self) -> list[str]:
        return [linha for linha in self.linhas if "empiremuse.player" in linha]


def faixa(titulo=TITULO, fonte=FONTE):
    return SimpleNamespace(
        title=titulo,
        single_title=titulo,
        uri="https://exemplo.invalido/musica",
        search_uri=None,
        url="https://exemplo.invalido/musica",
        duration=DURACAO_MS,
        source_name=fonte,
        info={"sourceName": fonte},
        thumb=None,
        track_loops=0,
        autoplay=False,
    )


def no(identificador="N1"):
    return SimpleNamespace(identifier=identificador, version=4)


def jogador(guild_id: int, *, metrics=None, titulo=TITULO):
    atual = faixa(titulo)
    player = SimpleNamespace(
        guild=SimpleNamespace(
            id=guild_id,
            name="Servidor",
            me=SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(id=CANAL_VOZ))),
        ),
        channel_id=CANAL_VOZ,
        tr=get_i18n().for_locale("pt-BR"),
        node=no(),
        bot=SimpleNamespace(
            dispatch=MagicMock(),
            user="bot",
            identifier="bot1",
            pool=SimpleNamespace(metrics=metrics or MetricsCollector()),
            get_cog=lambda nome: None,
        ),
        text_channel=None,
        locked=False,
        current=atual,
        last_track=atual,
        position=61_000,
        queue=[],
        update=False,
        failed_tracks=[],
        keep_connected=False,
        set_command_log=MagicMock(),
        track_end=AsyncMock(),
        process_next=AsyncMock(),
        process_save_queue=AsyncMock(),
        update_stage_topic=AsyncMock(),
        report_error=AsyncMock(),
    )
    return player


def evento(classe, player, **dados):
    base = {"track": player.current, "player": player, "node": player.node}
    return classe({**base, **dados})


def inicio(player):
    return evento(wavelink.TrackStart, player)


def fim(player, motivo="FINISHED"):
    return evento(wavelink.TrackEnd, player, reason=motivo)


def travou(player):
    return evento(wavelink.TrackStuck, player, thresholdMs=5000)


def excecao(player, *, causa="java.lang.RuntimeException: falha qualquer", severidade="common", no_do_evento=None):
    return evento(
        wavelink.TrackException,
        player,
        node=no_do_evento or no("OUTRO"),
        error="falha",
        exception={"cause": causa, "message": "Something went wrong", "severity": severidade},
    )


# ============================================================================ B12 — início e fim de faixa


class TestInicioEFim:
    async def test_five_hundred_guilds_starting_and_ending_tracks_at_info_give_zero_player_lines(self):
        console = Console(INFO)

        for guild in range(1, 501):
            player = jogador(guild)
            await _he_dispatch(player, inicio(player))
            await _he_dispatch(player, fim(player))

        assert console.do_player() == [] and console.linhas == []

    async def test_with_one_guild_in_debug_only_its_lines_come_out_with_the_title(self):
        alvo = 7_000_000_000_000_007
        console = Console(INFO, {alvo})

        for guild in [alvo, *range(1, 200)]:
            player = jogador(guild)
            await _he_dispatch(player, inicio(player))
            await _he_dispatch(player, fim(player))

        linhas = console.do_player()
        assert len(linhas) == 2 and all(str(alvo) in linha for linha in linhas)
        assert any(TITULO in linha for linha in linhas)

    async def test_the_title_never_shows_up_at_info_or_above(self):
        console = Console(INFO)
        player = jogador(1)

        await _he_dispatch(player, inicio(player))
        await _he_dispatch(player, travou(player))
        await _he_dispatch(player, excecao(player))

        assert TITULO not in console.saida.getvalue()


# ============================================================================ B12 — travou e exceção


class TestFalhasDeFaixa:
    async def test_a_stuck_track_is_a_warning_without_the_title_but_with_guild_channel_duration_and_source(self):
        console = Console(INFO)
        metricas = MetricsCollector()
        player = jogador(123456789012345678, metrics=metricas)

        await _he_dispatch(player, travou(player))

        (linha,) = console.do_player()
        assert "WARNING" in linha and TITULO not in linha
        assert "123456789012345678" in linha and str(CANAL_VOZ) in linha and FONTE in linha
        assert any(forma in linha for forma in ("215", "3:35", "03:35"))
        assert dict(metricas.snapshot_and_reset(1.0).errors) == {"track_stuck": 1}

    async def test_an_exception_is_a_warning_without_the_title_with_guild_and_source(self):
        console = Console(INFO)
        metricas = MetricsCollector()
        player = jogador(123456789012345678, metrics=metricas)

        await _he_dispatch(player, excecao(player))

        linhas = console.do_player()
        assert linhas and all("WARNING" in linha and TITULO not in linha for linha in linhas)
        assert any("123456789012345678" in linha and FONTE in linha for linha in linhas)
        assert dict(metricas.snapshot_and_reset(1.0).errors) == {"track_exception": 1}

    async def test_a_fault_severity_exception_is_an_error(self):
        console = Console(INFO)
        player = jogador(1)

        await _he_dispatch(player, excecao(player, severidade="fault"))

        assert any("ERROR" in linha for linha in console.do_player())

    @pytest.mark.parametrize("causa", MIRROR_NOT_FOUND_CAUSES)
    async def test_a_mirror_not_found_cause_is_debug_only(self, causa):
        console = Console(INFO)
        player = jogador(1)

        await _he_dispatch(player, excecao(player, causa=causa))

        assert console.do_player() == []

    async def test_the_exception_text_from_lavalink_is_not_printed_to_the_real_stdout(self, capsys):
        Console(INFO)
        player = jogador(1)

        await _he_dispatch(player, excecao(player))

        assert "Erro ao reproduzir a música" not in capsys.readouterr().out


# ============================================================================ B14 — um intervalo com 500 players saudáveis


class TestVolume:
    async def test_an_interval_with_five_hundred_healthy_players_gives_at_most_one_plus_nodes_lines(self):
        console = Console(INFO)
        pool = BotPool.__new__(BotPool)
        pool.config = {"METRICS_INTERVAL": 300}
        pool.bots, pool.guild_bots = [], {}
        pool.metrics = MetricsCollector()
        pool.logs_task = None

        for guild in range(1, 501):
            player = jogador(guild, metrics=pool.metrics)
            await _he_dispatch(player, inicio(player))
            await _he_dispatch(player, fim(player))

        class Parar(BaseException):
            pass

        voltas = []

        async def sleep(segundos):
            voltas.append(segundos)
            console.relogio.avancar(segundos)
            if len(voltas) >= 6:
                raise Parar

        with pytest.raises(Parar):
            await pool.logs_loop(clock=console.relogio, sleep=sleep)

        nos = 0
        assert len(console.linhas) <= 1 + nos
        assert "faixas iniciadas 500" in console.saida.getvalue()


# ============================================================================ B14b — busca do usuário e "Unknown op" (V-L10)

CONSULTA = "CONSULTA-SECRETA-DO-USUARIO-Z3 ytsearch"


def no_de_busca():
    node = Node.__new__(Node)
    node.identifier, node.version, node.password = "N1", 4, "senha-do-no-busca-1"
    node.rest_uri = "http://127.0.0.1:2333"

    class Resposta:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {"loadType": "NO_MATCHES", "data": {}}

    node.session = SimpleNamespace(get=lambda *a, **k: Resposta())
    node._client = SimpleNamespace(bot=SimpleNamespace(pool=SimpleNamespace(playlist_cache={})))
    return node


class TestBuscaDoUsuario:
    async def test_a_search_at_info_leaves_no_line_with_the_query(self):
        console = Console(INFO)

        assert await no_de_busca().get_tracks(CONSULTA) == []

        assert "CONSULTA-SECRETA" not in console.saida.getvalue()

    async def test_a_search_inside_another_guild_does_not_show_the_query(self):
        console = Console(INFO, {6_000_000_000_000_006})

        with log_context(guild=8_000_000_000_000_008):
            await no_de_busca().get_tracks(CONSULTA)

        assert "CONSULTA-SECRETA" not in console.saida.getvalue()


class TestOpDesconhecido:
    PAYLOAD = {"op": "operacao-nova", "guildId": "1", "segredo": "PAYLOAD-SECRETO-K9"}

    async def test_unknown_op_is_a_warning_with_the_op_and_without_the_payload(self):
        console = Console(INFO)

        await WebSocket.__new__(WebSocket).process_data(dict(self.PAYLOAD))

        saida = console.saida.getvalue()
        assert "WARNING" in saida and "operacao-nova" in saida and "PAYLOAD-SECRETO-K9" not in saida

    async def test_an_op_with_control_characters_stays_on_one_line(self):
        console = Console(INFO)

        await WebSocket.__new__(WebSocket).process_data({"op": "x\n2026-10-27 ERROR falso\x1b[2J"})

        saida = console.saida.getvalue()
        assert "\x1b" not in saida and len(console.linhas) == 1

    async def test_a_failing_event_handler_is_logged_with_its_traceback_and_not_printed(self, capfd):
        console = Console(INFO)
        websocket = WebSocket.__new__(WebSocket)
        player = jogador(1)
        websocket._node = SimpleNamespace(
            players={1: player}, on_event=AsyncMock(side_effect=ValueError("handler quebrou")), identifier="N1"
        )

        await websocket.process_data({"op": "event", "type": "TrackStartEvent", "guildId": "1", "track": None})

        saida = console.saida.getvalue()
        assert "ValueError" in saida and "Traceback (most recent call last)" in saida and "ERROR" in saida
        assert "handler quebrou" not in capfd.readouterr().err


# ============================================================================ nodes.py — nó conectado, caiu, todos indisponíveis


def no_fisico(identificador="no-1", *, disponivel=True):
    return SimpleNamespace(
        _websocket=SimpleNamespace(is_connected=True),
        stats=SimpleNamespace(uptime=10**9),
        info={"sourceManagers": ["youtube", "soundcloud"]},
        identifier=identificador,
        version=4,
        search_providers=["ytsearch"],
        partial_providers=[],
        native_sources=set(),
        original_providers={"ytsearch"},
        only_use_native_search_providers=True,
        open=lambda: None,
        is_available=disponivel,
        players={},
        session_id=None,
        uid=1,
        _retry_dt=datetime.fromtimestamp(0, timezone.utc),
        _retry_count=0,
        rest_uri="http://127.0.0.1:2333",
        headers={},
    )


def cog_de_nos(*nos_do_bot):
    from empiremuse.cogs.music import Music

    cog = Music.__new__(Music)
    cog.bot = SimpleNamespace(
        user="bot",
        loop=SimpleNamespace(create_task=lambda coro: coro.close()),
        wavelink_node_reconnect_tasks={},
        music=SimpleNamespace(nodes={n.identifier: n for n in nos_do_bot}),
        pool=SimpleNamespace(metrics=MetricsCollector()),
        config={"LAVALINK_RECONNECT_RETRIES": 1},
        wait_until_ready=AsyncMock(),
    )
    return cog


class TestEventosDeNo:
    async def test_a_node_that_becomes_ready_is_an_info_line_with_identifier_and_version(self, capsys):
        console = Console(INFO)
        node = no_fisico("no-conectado")
        cog = cog_de_nos(node)

        await NodesMixin.node_ready(cog, node)

        (linha,) = [linha for linha in console.linhas if "no-conectado" in linha]
        assert "INFO" in linha and "empiremuse.wavelink" in linha and "4" in linha
        assert "está pronto para uso" not in capsys.readouterr().out

    async def test_a_dropped_node_is_a_warning_with_the_identifier(self, capsys):
        console = Console(INFO)
        node = no_fisico("no-caiu")
        cog = cog_de_nos(node)

        await NodesMixin.node_reconnect(cog, node)

        (linha,) = [linha for linha in console.linhas if "no-caiu" in linha]
        assert "WARNING" in linha and "empiremuse.wavelink" in linha
        assert "Conexão perdida" not in capsys.readouterr().out

    async def test_repeated_drops_of_the_same_node_are_aggregated(self):
        console = Console(INFO)
        node = no_fisico("no-instavel")
        cog = cog_de_nos(node)

        for _ in range(6):
            node._retry_dt = datetime.fromtimestamp(0, timezone.utc)
            node._retry_count = 0
            await NodesMixin.node_reconnect(cog, node)
        flush_logging()

        linhas = [linha for linha in console.linhas if "no-instavel" in linha]
        assert len(linhas) == 2 and "repetida 5×" in "".join(console.linhas)

    async def test_all_nodes_unavailable_is_one_error_per_transition(self):
        console = Console(INFO)
        brutos: list[logging.LogRecord] = []

        class Bruto(logging.Handler):
            def emit(self, record):
                if record.name.startswith("empiremuse.wavelink") and record.levelno >= logging.ERROR:
                    brutos.append(record)

        logging.getLogger().addHandler(Bruto())
        a, b = no_fisico("no-a"), no_fisico("no-b")
        cog = cog_de_nos(a, b)

        a.is_available = False
        await NodesMixin.node_connection_closed(cog, a)
        assert brutos == []  # ainda resta o no-b

        b.is_available = False
        await NodesMixin.node_connection_closed(cog, b)
        await NodesMixin.node_connection_closed(cog, b)  # outro evento com todos já fora: não é uma transição nova
        assert len(brutos) == 1

        a.is_available = True
        await NodesMixin.node_ready(cog, a)  # um nó voltou: a próxima queda total é outra transição
        console.relogio.avancar(61)
        a.is_available = False
        await NodesMixin.node_connection_closed(cog, a)
        assert len(brutos) == 2
        assert len([linha for linha in console.linhas if "ERROR" in linha]) == 2  # janela do agregador já fechada


# ============================================================================ integração — um evento do player com o pipeline real
