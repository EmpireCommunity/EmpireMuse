"""Apoio dos testes do item 7 (P1, ``empiremuse.logs``): valores falsos no formato real, registros e relógio injetado.

Os segredos falsos são montados em tempo de execução (``.join``/multiplicação): nenhum literal parece um token de verdade.
Cada teste usa valores próprios: o ``register_secret`` acumula no processo e não há API para esquecer um valor.
"""

import logging
import re
import sys
import threading
from collections.abc import Callable

SGR = re.compile(r"\x1b\[([0-9;]*)m")
ESPERA_MAXIMA = 10  # só impede a suíte de pendurar se algo travar; o que se verifica é ter terminado sozinho


def token_discord(variante: str = "a") -> str:
    """Só o FORMATO de um token do Discord (26 + 6 + 28 caracteres); o valor muda com ``variante``."""
    return ".".join([("M" + variante) * 13, (variante.upper() + "x") * 3, (variante + "Q") * 14])


def token_webhook(variante: str = "w") -> str:
    return "-".join([variante * 8, "Tk", variante * 12, "x_9"])


class Relogio:
    """Relógio falso e monótono: o tempo só anda quando o teste manda."""

    def __init__(self, agora: float = 1000.0) -> None:
        self.agora = agora

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


def registro(
    mensagem: object = "mensagem",
    *args: object,
    nivel: int = logging.INFO,
    nome: str = "empiremuse.teste",
    exc_info: object = None,
    stack_info: str | None = None,
    **extra: object,
) -> logging.LogRecord:
    """``LogRecord`` como o ``Logger`` o cria (``extra`` vira atributo), sem passar por handler nenhum."""
    return logging.getLogger(nome).makeRecord(
        nome, nivel, __file__, 1, mensagem, args or (), exc_info, extra=extra or None, sinfo=stack_info
    )


def arquivo_do_log(pasta_de_logs):
    """O arquivo de log do dia (``disnake-AAAA-MM-DD.log``) numa pasta ``.logs``: o nome muda com a data, então vem por padrão."""
    achados = sorted(pasta_de_logs.glob("disnake-*.log"))
    return achados[-1] if achados else pasta_de_logs / "disnake-ausente.log"


def excecao(texto: str, tipo: type[BaseException] = ValueError) -> tuple:
    """``exc_info`` real de uma exceção levantada e capturada (com traceback)."""
    try:
        raise tipo(texto)
    except tipo:
        return sys.exc_info()


def sequencias_sgr(linha: str) -> set[str]:
    """Todos os parâmetros SGR (``31``, ``1``...) das sequências ANSI de ``linha``."""
    return {parte for sequencia in SGR.findall(linha) for parte in sequencia.split(";") if parte}


def sem_cor(texto: str) -> str:
    return SGR.sub("", texto)


class Espiao:
    """Objeto cujo ``__str__``/``__repr__`` anotam a chamada: prova que nada foi formatado."""

    def __init__(self) -> None:
        self.chamadas = 0

    def __str__(self) -> str:
        self.chamadas += 1
        return "espiao"

    __repr__ = __str__


def em_thread(alvo: Callable[[], object]) -> threading.Thread:
    """Roda ``alvo`` numa ``threading.Thread`` de verdade e espera terminar."""
    fio = threading.Thread(target=alvo)
    fio.start()
    fio.join(timeout=ESPERA_MAXIMA)
    assert not fio.is_alive()
    return fio
