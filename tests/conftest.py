"""Fixtures globais da suíte.

Isolamento do ambiente real: ``config_loader.load_config()`` chama ``dotenv_values()`` sem caminho, e o python-dotenv
procura o ``.env`` a partir do DIRETÓRIO DO MÓDULO (não do cwd). Sem este fixture, qualquer teste que chame
``load_config()`` lê o ``.env`` REAL do desenvolvedor (tokens, segredos do painel) mesmo com ``monkeypatch.chdir``, o que
torna o teste não determinístico e pode imprimir segredos na saída de uma falha. Testes que precisam de um ``.env``
próprio substituem ``config_loader.dotenv_values`` por um leitor do ``tmp_path``.

Isolamento da ``.runtime/`` de desenvolvimento (F3-7, D5): a pasta ``PROJECT_DIR/.runtime`` muda a decisão de
``paths.get_runtime_dir()`` e guarda o ``.env`` real do desenvolvedor. Ela existe no checkout principal, mas não nas
worktrees, então a suíte se comportaria de um jeito num lugar e de outro no outro. A guarda a faz parecer ausente.
"""

import logging
from pathlib import Path
import sys
import threading

import pytest

from tests.support.runtime_guard import DEV_RUNTIME_DIRNAME, REAL_PROJECT_DIR, hide_path


@pytest.fixture(autouse=True)
def _isolate_real_dotenv(monkeypatch):
    for mod in ("empiremuse.config_loader", "config_loader"):
        try:
            __import__(mod)
        except ImportError:
            continue
        monkeypatch.setattr(f"{mod}.dotenv_values", lambda *args, **kwargs: {})


@pytest.fixture(autouse=True)
def _pt_br_fallback_locale(request, monkeypatch):
    """O idioma de reserva do resolvedor nos testes é ``pt-BR``: os asserts com texto em português valem sem edição (contrato i18n §7).

    Os testes de i18n (``tests/i18n`` e ``tests/rules/test_i18n.py``) ficam com o fallback de PRODUÇÃO (``en-US``) e passam o idioma
    explícito quando precisam de outro; o catálogo ``en-US`` (a referência) nunca muda.
    """
    path = Path(str(request.node.fspath)).as_posix()
    if "/tests/i18n/" in path or path.endswith("/tests/rules/test_i18n.py"):
        return
    monkeypatch.setattr("empiremuse.rules.i18n.FALLBACK_LOCALE", "pt-BR")


@pytest.fixture(autouse=True)
def _hide_dev_runtime(monkeypatch):
    hide_path(monkeypatch, REAL_PROJECT_DIR / DEV_RUNTIME_DIRNAME)


BIBLIOTECAS_COM_NIVEL_FIXO = (
    "disnake",
    "aiohttp",
    "asyncio",
    "tornado",
    "urllib3",
    "pymongo",
    "motor",
    "websockets",
    "PIL",
)


@pytest.fixture(autouse=True)
def _isolate_logging_state():
    """Um teste que chama ``configure_logging`` (direto ou via ``BotPool.setup``) não pode deixar a cadeia de filtros do item 7 no root.

    Os filtros zeram ``exc_info`` e trocam ``msg``/``args``; sem esta restauração o ``caplog`` de um teste que rode DEPOIS enxerga o registro já
    processado. Volta o que existia: handlers (menos os que o próprio pytest põe por fase), nível do root, nível dos loggers de biblioteca e os
    ganchos globais de exceção.
    """
    raiz = logging.getLogger()
    handlers, nivel = list(raiz.handlers), raiz.level
    niveis = {nome: logging.getLogger(nome).level for nome in BIBLIOTECAS_COM_NIVEL_FIXO}
    ganchos = (sys.excepthook, threading.excepthook)
    yield
    for handler in list(raiz.handlers):
        if handler not in handlers and not type(handler).__module__.startswith("_pytest"):
            raiz.removeHandler(handler)
            handler.close()
    raiz.setLevel(nivel)
    for nome, valor in niveis.items():
        logging.getLogger(nome).setLevel(valor)
    sys.excepthook, threading.excepthook = ganchos
