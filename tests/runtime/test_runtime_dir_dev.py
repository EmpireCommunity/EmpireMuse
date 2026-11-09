"""F3-7 (D1 a D3) — pasta de execução de desenvolvimento ``.runtime/``.

Sem ``EMPIREMUSE_HOME``, o bot gravava Lavalink.jar, JDK, ``local_database/``, caches e ``.env`` no cwd, que em
desenvolvimento é a raiz do repositório. Agora, se ``PROJECT_DIR/.runtime`` existir como diretório, ela vira o
``RUNTIME_DIR`` e o cwd do processo. O ``GIT_DIR`` padrão continua ``RUNTIME_DIR/.git``,
para que o repositório acompanhe o diretório de execução.
Produção não muda: Docker e Pterodactyl definem ``EMPIREMUSE_HOME``; a Discloud não recebe ``.runtime/``.

Os testes trocam ``paths.PROJECT_DIR`` por uma pasta do ``tmp_path`` e nunca tocam a ``.runtime`` real.
"""

import os

import pytest

from empiremuse import config_loader, paths


@pytest.fixture
def projeto(monkeypatch, tmp_path):
    """Projeto falso: PROJECT_DIR no tmp_path, cwd numa pasta à parte, sem EMPIREMUSE_HOME nem GIT_DIR."""
    raiz = (tmp_path / "projeto").resolve()
    raiz.mkdir()
    cwd = (tmp_path / "cwd").resolve()
    cwd.mkdir()
    monkeypatch.setattr(paths, "PROJECT_DIR", raiz)
    monkeypatch.delenv("EMPIREMUSE_HOME", raising=False)
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.chdir(cwd)  # também garante que o chdir do enter_runtime_dir não vaza para outros testes
    return raiz, cwd


@pytest.fixture
def runtime_dev(projeto):
    raiz, _ = projeto
    pasta = raiz / ".runtime"
    pasta.mkdir()
    return pasta


# --------------------------------------------------------------------------------------------------------------
# D1 — get_runtime_dir()
# --------------------------------------------------------------------------------------------------------------


class TestGetRuntimeDir:
    @pytest.mark.smoke
    def test_runtime_dir_dinamico_acompanha_a_runtime(self, projeto, runtime_dev):
        assert paths.RUNTIME_DIR == runtime_dev


# --------------------------------------------------------------------------------------------------------------
# D2 — enter_runtime_dir()
# --------------------------------------------------------------------------------------------------------------


class TestEnterRuntimeDir:
    @pytest.fixture
    def main_module(self):
        import empiremuse.__main__ as main_module

        return main_module

    @pytest.mark.smoke
    def test_entra_na_runtime_quando_ela_e_a_escolhida(self, projeto, runtime_dev, main_module):
        main_module.enter_runtime_dir()

        assert os.path.samefile(os.getcwd(), runtime_dev)


# --------------------------------------------------------------------------------------------------------------
# D3 — GIT_DIR padrão
# --------------------------------------------------------------------------------------------------------------


class TestGitDirPadrao:
    def test_com_a_runtime_de_desenvolvimento_continua_sob_o_runtime_dir(self, projeto, runtime_dev):
        assert config_loader.load_config()["GIT_DIR"] == str(runtime_dev / ".git")

    def test_com_empiremuse_home_continua_sob_o_home(self, monkeypatch, tmp_path, runtime_dev):
        home = (tmp_path / "container").resolve()
        home.mkdir()
        monkeypatch.setenv("EMPIREMUSE_HOME", str(home))

        assert config_loader.load_config()["GIT_DIR"] == str(home / ".git")
