"""Item 7 (P1, Turbine + Gambit) — integração com o ``configure_logging`` REAL: dois handlers (console e arquivo em ``tmp_path``).

B2 ponta a ponta (token no traceback e no ``stack_info``, logger ``aiohttp`` propagado), B3 (linha forjada por f-string), B5 (cor só no
console) e B7 (1000 WARNING iguais = 999 por destino, não 1998). Relógio do agregador injetado; nada dorme.
"""

import io
from types import SimpleNamespace
import logging
import re

import pytest

from empiremuse.logs.setup import configure_logging, flush_logging, tick_logging
from empiremuse.logs.settings import LogSettings
from tests.logs.apoio import arquivo_do_log, Relogio, excecao, registro, sem_cor, token_discord, token_webhook

pytestmark = pytest.mark.critical

ENQUADRAMENTO = "  │ "
CABECALHO_CONSOLE = re.compile(r"^\d{2}:\d{2}:\d{2}\s")
CABECALHO_ARQUIVO = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\s")


class Destinos:
    def __init__(self, console: io.StringIO, arquivo) -> None:
        self.console, self.arquivo = console, arquivo

    @property
    def texto_console(self) -> str:
        return self.console.getvalue()

    @property
    def texto_arquivo(self) -> str:
        return self.arquivo.read_text(encoding="utf-8")

    def ambos(self) -> list[str]:
        return [self.texto_console, self.texto_arquivo]


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def montar(tmp_path, relogio):
    def _montar(segredos=(), *, cor=False, nivel=logging.INFO, guildas=()) -> Destinos:
        console = io.StringIO()
        configure_logging(
            LogSettings(nivel, cor, True, frozenset(guildas), 300),
            segredos,
            stream=console,
            logs_dir=tmp_path / ".logs",
            clock=relogio,
        )
        return Destinos(console, arquivo_do_log(tmp_path / ".logs"))

    return _montar


def cabecalhos(texto: str) -> list[str]:
    return [linha for linha in texto.splitlines() if linha and not linha.startswith(ENQUADRAMENTO.rstrip())]


# ============================================================================ B2 — redação ponta a ponta


class TestRedacaoPonteAPonta:
    def test_the_token_in_the_message_the_args_and_the_traceback_never_reaches_any_destination(self, montar):
        token = token_discord("i")
        destinos = montar([token])
        log = logging.getLogger("empiremuse.teste")

        log.error("login com %s falhou", token)
        log.error(f"login com {token} falhou de novo")
        try:
            raise RuntimeError(f"Improper token {token}")
        except RuntimeError:
            log.exception("exceção no login")

        for texto in destinos.ambos():
            assert token not in texto and "***" in texto and "Improper token" in texto

    def test_the_token_in_stack_info_is_masked(self, montar):
        token = token_discord("j")
        destinos = montar([token])
        record = registro(
            "pilha", nivel=logging.ERROR, nome="empiremuse.teste", stack_info=f'  File "x.py"\n    login("{token}")'
        )

        logging.getLogger("empiremuse.teste").handle(record)

        for texto in destinos.ambos():
            assert token not in texto and "login(" in texto

    def test_a_propagated_aiohttp_record_is_redacted_in_both_destinations(self, montar):
        senha = "senha-no-aiohttp-prop-1"
        destinos = montar([senha])

        logging.getLogger("aiohttp.client").error("Authorization: Bot %s falhou", senha)
        logging.getLogger("aiohttp.client").info("isto é INFO de biblioteca")

        for texto in destinos.ambos():
            assert senha not in texto and "falhou" in texto and "INFO de biblioteca" not in texto

    @pytest.mark.parametrize(
        ("sensivel", "mensagem"),
        [
            (token_webhook("k"), "POST https://discord.com/api/webhooks/123456789012345678/{}"),
            ("senha-do-mongo-777", "conexão mongodb+srv://usuario:{}@cluster0.exemplo.net/db"),
            ("chave-lastfm-9", "GET /2.0/?method=x&api_key={}&format=json"),
            ("sessao-lastfm-8", "GET /2.0/?method=y&sk={}"),
            ("valor-do-header-5", "Authorization: Bot {}"),
            ("valor-no-repr-6", "headers={{'Content-Type': 'x', 'Authorization': '{}'}}"),
            ("refresh-no-json-4", '{{"refresh_token": "{}"}}'),
            ("senha-do-redis-3", "redis://default:{}@cache:6379/0"),
        ],
    )
    def test_every_documented_pattern_is_masked_in_both_destinations(self, montar, sensivel, mensagem):
        destinos = montar()

        logging.getLogger("empiremuse.teste").warning(mensagem.format(sensivel))
        logging.getLogger("empiremuse.teste").warning("%s", mensagem.format(sensivel) + " de novo")

        for texto in destinos.ambos():
            assert sensivel not in texto and "***" in texto


# ============================================================================ B3 — injeção e enquadramento ponta a ponta


class TestInjecao:
    FORJADA = "x\n2026-10-27 12:00:00 ERROR falso"

    def test_a_fstring_with_a_line_break_gives_one_header_and_framed_continuations(self, montar):
        destinos = montar()

        logging.getLogger("empiremuse.teste").error(f"falha em {self.FORJADA}")

        console, arquivo = destinos.ambos()
        assert len(cabecalhos(console)) == 1 and CABECALHO_CONSOLE.match(cabecalhos(console)[0])
        assert len(cabecalhos(arquivo)) == 1 and CABECALHO_ARQUIVO.match(cabecalhos(arquivo)[0])
        for texto in (console, arquivo):
            assert f"{ENQUADRAMENTO}2026-10-27 12:00:00 ERROR falso" in texto

    def test_an_exception_message_with_a_forged_line_is_framed_in_the_traceback(self, montar):
        destinos = montar()

        try:
            raise RuntimeError(self.FORJADA)
        except RuntimeError:
            logging.getLogger("empiremuse.teste").exception("falhou")

        for texto in destinos.ambos():
            assert len(cabecalhos(texto)) == 1
            assert "Traceback (most recent call last)" in texto and texto.count("\n") > 3
            assert all(linha.startswith(ENQUADRAMENTO.rstrip()) for linha in texto.splitlines()[1:])

    def test_controls_and_bidi_never_reach_the_outputs(self, montar):
        destinos = montar(cor=False)

        logging.getLogger("empiremuse.teste").warning("v=%s", "\x1b[2J\x9b‮⁦fim\x00")
        logging.getLogger("empiremuse.teste").warning("t=\x1b[31mvermelho\x9b")

        for texto in destinos.ambos():
            assert not any(c in texto for c in ("\x1b", "\x9b", "‮", "⁦", "\x00")) and "fim" in texto


# ============================================================================ B5 — cor só no console


# ============================================================================ B7 — agregação com os DOIS handlers


class TestAgregacao:
    def test_a_thousand_equal_warnings_give_one_full_record_and_one_summary_of_999_in_each_destination(
        self, montar, relogio
    ):
        destinos = montar()
        log = logging.getLogger("empiremuse.wavelink")

        for n in range(1000):
            try:
                raise ConnectionError("nó fora")
            except ConnectionError:
                log.warning("nó %s caiu", n, exc_info=True)
        relogio.avancar(61)
        tick_logging()

        for texto in destinos.ambos():
            assert texto.count("Traceback (most recent call last)") == 1
            assert texto.count("repetida 999×") == 1 and "1998" not in texto and texto.count("↻") == 1
            assert "nó 0 caiu" in texto
            assert len(cabecalhos(texto)) == 2

    def test_flush_emits_the_pending_counts_in_both_destinations(self, montar):
        destinos = montar()
        for _ in range(3):
            logging.getLogger("empiremuse.wavelink").warning("pendente")

        flush_logging()

        for texto in destinos.ambos():
            assert "repetida 2×" in texto


# ============================================================================ K8g — código de erro ponta a ponta


def falha_com_codigo(codigo: str, mensagem: str = "boom") -> None:
    """Mesmo caminho de chamada a cada vez (mesmas linhas ``File … line N, in f``), como um comando que falha sempre no mesmo ponto."""
    try:
        _levanta(mensagem)
    except ValueError:
        logging.getLogger("empiremuse.errors").error(
            "Erro inesperado em cmd [%s]", codigo, exc_info=True, extra={"error_code": codigo}
        )


def _levanta(mensagem: str) -> None:
    raise ValueError(mensagem)


def ponto_a() -> None:
    _levanta("x")


class TestCodigoDeErroPonteAPonta:
    def test_two_coded_errors_of_the_same_path_give_one_traceback_and_one_pointer_line_in_each_destination(
        self, montar
    ):
        destinos = montar()

        falha_com_codigo("ERR-AAAAAA", "boom A")
        falha_com_codigo("ERR-BBBBBB", "boom B")

        for texto in destinos.ambos():
            assert texto.count("Traceback (most recent call last)") == 1
            assert texto.count("ERR-AAAAAA") == 2 and texto.count("ERR-BBBBBB") == 1
            assert texto.count("traceback igual ao de ERR-AAAAAA") == 1
            assert "ValueError: boom B — traceback igual ao de ERR-AAAAAA" in texto
            assert len(cabecalhos(texto)) == 2

    def test_a_burst_of_sixty_coded_errors_leaves_one_traceback_forty_nine_lines_and_the_summary(self, montar, relogio):
        destinos = montar()

        for n in range(60):
            falha_com_codigo(f"ERR-{n:06X}")
        relogio.avancar(61)
        tick_logging()

        for texto in destinos.ambos():
            assert texto.count("Traceback (most recent call last)") == 1
            assert texto.count("traceback igual ao de ERR-000000") == 49 + 1  # 49 linhas + o resumo
            assert texto.count("repetida 10× em 60 s (códigos omitidos; traceback igual ao de ERR-000000)") == 1
            assert all(f"ERR-{n:06X}" in texto for n in range(50))

    def test_a_forged_code_in_extra_cannot_inject_a_line_or_a_pointer(self, montar):
        destinos = montar()
        log = logging.getLogger("empiremuse.errors")

        for codigo in ("ERR-AAAAAA", "x\nERR-FORJADO"):
            try:
                _levanta("boom")
            except ValueError:
                log.error("Erro inesperado em cmd", exc_info=True, extra={"error_code": codigo})
        flush_logging()

        for texto in destinos.ambos():
            assert "ERR-FORJADO" not in texto and len(cabecalhos(texto)) == 2


# ============================================================================ V-K8-1 — a impressão digital vem do traceback ESTRUTURADO


def mensagem_forjada(n: int) -> str:
    """Mensagem de exceção com uma linha que PARECE um quadro do traceback, diferente a cada erro."""
    return f'falha {n}\n  File "x{n}.py", line {n}, in f{n}'


def falha_sem_codigo(mensagem: str) -> None:
    try:
        _levanta(mensagem)
    except ValueError:
        logging.getLogger("empiremuse.errors").error("Erro inesperado em cmd", exc_info=True)


class TestImpressaoDigitalEstruturada:
    def test_a_hundred_errors_of_one_path_with_forged_frame_lines_in_the_message_still_aggregate(self, montar, relogio):
        destinos = montar()

        for n in range(100):
            falha_sem_codigo(mensagem_forjada(n))
        relogio.avancar(61)
        tick_logging()

        for texto in destinos.ambos():
            assert texto.count("Traceback (most recent call last)") == 1
            assert texto.count("repetida 99× em 60 s") == 1 and texto.count("↻") == 1
            assert len(cabecalhos(texto)) == 2
            assert "x57.py" not in texto, "um erro suprimido não pode aparecer no log"

    def test_a_message_that_copies_the_frame_lines_of_another_path_does_not_merge_with_it(self, montar):
        destinos = montar()
        log = logging.getLogger("empiremuse.errors")
        try:
            ponto_a()
        except ValueError:
            log.error("Erro inesperado em cmd", exc_info=True)
        try:
            _levanta('x\n  File "' + __file__ + '", line 1, in ponto_a')
        except ValueError:
            log.error("Erro inesperado em cmd", exc_info=True)

        for texto in destinos.ambos():
            assert texto.count("Traceback (most recent call last)") == 2 and "repetida" not in texto


# ============================================================================ V-126-1 — a chave do agregador nunca guarda o texto cru


def agregador_instalado():
    raiz = logging.getLogger()
    for handler in raiz.handlers:
        for filtro in handler.filters:
            if type(filtro).__name__ == "RepeatAggregator":
                return filtro
    raise AssertionError("o configure_logging não instalou um RepeatAggregator")


def textos_da_chave(agregador) -> list[str]:
    """Todo texto que a chave de cada balde carrega (a chave é uma tupla; o que não for ``str`` é ignorado)."""
    return [parte for chave in agregador._buckets for parte in chave if isinstance(parte, str)]


class AposOsFiltros(logging.Handler):
    """Handler instalado DEPOIS do ``configure_logging``: vê o registro já processado pelos filtros dos outros handlers."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.atributos: list[dict] = []

    def emit(self, record):
        self.atributos.append({k: v for k, v in vars(record).items() if k.startswith("empiremuse_")})


class TestChaveSemSegredo:
    SEG = "senha-na-chave-do-balde-V126-q"

    @pytest.fixture
    def instalado(self, montar):
        destinos = montar([self.SEG])
        depois = AposOsFiltros()
        logging.getLogger().addHandler(depois)
        return SimpleNamespace(destinos=destinos, depois=depois, agregador=agregador_instalado())

    def test_a_secret_and_control_characters_in_a_line_without_args_never_reach_the_bucket_key(self, instalado):
        logging.getLogger("empiremuse.wavelink.local").warning(f"Failed to bind {self.SEG} \x1b[2J\rfim")

        for texto in textos_da_chave(instalado.agregador):
            assert self.SEG not in texto and "\x1b" not in texto and "\r" not in texto
        assert textos_da_chave(instalado.agregador), "o teste não prova nada sem chave no balde"

    def test_no_empiremuse_attribute_of_the_processed_record_carries_the_raw_secret(self, instalado):
        logging.getLogger("empiremuse.wavelink.local").warning(f"Failed to bind {self.SEG} \x1b[2J\rfim")

        (atributos,) = instalado.depois.atributos
        for nome, valor in atributos.items():
            if isinstance(valor, str):
                assert self.SEG not in valor and "\x1b" not in valor and "\r" not in valor, nome

    def test_a_secret_straddling_the_256th_character_leaves_no_fragment_in_the_key(self, instalado):
        linha = "a" * 250 + self.SEG + "b" * 100

        logging.getLogger("empiremuse.wavelink.local").warning(linha)

        for texto in textos_da_chave(instalado.agregador):
            assert self.SEG[:6] not in texto and self.SEG[-6:] not in texto
