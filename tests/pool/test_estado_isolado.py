"""F3-6 — ``BotPool``: nenhuma instância compartilha estado mutável com outra (achado da F3-2).

``BotPool`` declara ``bots`` no corpo da classe ("valores só de referência"); o ``__init__`` cria os de
cada instância. Declarações de classe mutáveis são uma armadilha (``cls.bots.append`` alteraria todos os pools) e não são
mais necessárias: nenhum teste depende delas (conferido removendo as duas linhas numa cópia: a suíte inteira segue igual).
Estes testes provam o que importa, o comportamento: instâncias independentes. O último é o único RED: a classe não deve
mais declarar contêineres mutáveis (o Backend pode remover as duas declarações).
Sem rede, sem Discord e sem ``.env`` real (``EMPIREMUSE_HOME`` em ``tmp_path``).
"""

import pytest

from empiremuse.utils.client import BotPool


@pytest.fixture
def isolado(monkeypatch, tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(home))
    monkeypatch.chdir(cwd)


class TestInstanciasIndependentes:
    @pytest.mark.smoke
    def test_bots_de_uma_instancia_nao_aparecem_na_outra(self, isolado):
        a, b = BotPool(), BotPool()
        a.bots.append("bot-a")
        assert b.bots == []
