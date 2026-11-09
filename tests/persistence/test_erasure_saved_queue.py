"""Apagar meus dados (E1), contrato §2 linha 23 e T22: a fila salva do membro é ARQUIVO (``saved_queues_json``, ``saved_queues_v1``).

``EMPIREMUSE_HOME`` aponta para ``tmp_path`` (``paths.RUNTIME_DIR`` é dinâmico). Os nomes saem só do inteiro do membro: nada de glob nem de prefixo.
"""

import asyncio
import logging
import os
from pathlib import Path

import pytest
import pytest_asyncio

from empiremuse.rules.erasure import ErasureStatus, saved_queue_file_names, saved_queue_tmp_matches
from empiremuse.utils.music.saved_queue import delete_saved_queues, saved_queue_files
from tests.support.erasure_world import ALVO, PREFIXO_DO_ALVO, SUFIXO_DO_ALVO, gravar_usuario, montar_mundo

pytestmark = pytest.mark.critical


@pytest.fixture
def pastas(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "runtime"))
    base = tmp_path / "runtime" / "local_database"
    json_dir, v1_dir = base / "saved_queues_json" / "users", base / "saved_queues_v1" / "users"
    json_dir.mkdir(parents=True)
    v1_dir.mkdir(parents=True)
    return json_dir, v1_dir


def _escrever(pasta: Path, nome: str, conteudo: str = "x") -> Path:
    caminho = pasta / nome
    caminho.write_text(conteudo)
    return caminho


def _semear_vizinhos(json_dir: Path, v1_dir: Path) -> list[Path]:
    return [
        _escrever(json_dir, f"{PREFIXO_DO_ALVO}.json"),
        _escrever(json_dir, f"{SUFIXO_DO_ALVO}.json"),
        _escrever(json_dir, f"{ALVO}.json.bak"),
        _escrever(json_dir, f"{PREFIXO_DO_ALVO}.json.abc123.tmp"),  # temporário de outro usuário
        _escrever(json_dir, "999.json"),
        _escrever(v1_dir, f"{PREFIXO_DO_ALVO}.pkl"),
        _escrever(v1_dir, f"{SUFIXO_DO_ALVO}.pkl"),
    ]


@pytest.mark.parametrize(
    ("nome", "casa"),
    [
        ("1.json.ab_C9.tmp", True),
        ("1.json.tmp", False),  # sem a parte aleatória
        ("1.json.a.b.tmp", False),
        ("11.json.abc.tmp", False),
        ("21.json.abc.tmp", False),
        ("1.json.abc", False),
        ("1.json.ab/c.tmp", False),
        ("1.json.ábc.tmp", False),
    ],
)
def test_saved_queue_tmp_matches_so_o_temporario_do_proprio_membro(nome, casa):
    assert saved_queue_tmp_matches(nome, 1) is casa


def test_saved_queue_file_names_so_do_inteiro():
    assert saved_queue_file_names(45) == ("45.json", "45.pkl")


def test_t22_apaga_json_pkl_e_temporario_do_alvo_e_so_eles(pastas):
    json_dir, v1_dir = pastas
    vizinhos = _semear_vizinhos(json_dir, v1_dir)
    (json_dir / "subpasta").mkdir()
    alvo = [
        _escrever(json_dir, f"{ALVO}.json"),
        _escrever(v1_dir, f"{ALVO}.pkl"),
        _escrever(json_dir, f"{ALVO}.json.k3y_x.tmp"),
    ]

    assert sorted(saved_queue_files(ALVO)) == sorted(alvo)
    assert delete_saved_queues(ALVO) == 3

    assert not any(arquivo.exists() for arquivo in alvo)
    assert all(arquivo.exists() for arquivo in vizinhos) and (json_dir / "subpasta").is_dir()
    assert delete_saved_queues(ALVO) == 0  # de novo: ausente não é erro


def test_t22_link_simbolico_com_o_nome_do_alvo_e_removido_como_link_e_o_destino_fica(pastas, tmp_path):
    json_dir, _ = pastas
    destino = tmp_path / "fora.txt"
    destino.write_text("nao-tocar")
    (json_dir / f"{ALVO}.json").symlink_to(destino)

    assert delete_saved_queues(ALVO) == 1

    assert not (json_dir / f"{ALVO}.json").is_symlink() and destino.read_text() == "nao-tocar"


def test_t22_pasta_com_o_nome_do_alvo_e_ignorada_e_pasta_ausente_nao_e_erro(pastas, tmp_path, monkeypatch):
    json_dir, _ = pastas
    (json_dir / f"{ALVO}.json").mkdir()
    assert delete_saved_queues(ALVO) == 0 and (json_dir / f"{ALVO}.json").is_dir()

    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "sem-pastas"))
    assert delete_saved_queues(ALVO) == 0


@pytest_asyncio.fixture
async def mundo(tmp_path, pastas):
    mundo = montar_mundo(tmp_path)
    yield mundo
    for tarefa in list(mundo.service._tasks):
        tarefa.cancel()
    await asyncio.gather(*mundo.service._tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_t22_o_servico_apaga_a_fila_conta_no_relatorio_e_deixa_os_vizinhos(mundo, pastas):
    json_dir, v1_dir = pastas
    vizinhos = _semear_vizinhos(json_dir, v1_dir)
    alvo = [_escrever(json_dir, f"{ALVO}.json"), _escrever(v1_dir, f"{ALVO}.pkl")]

    relatorio = await mundo.service.erase_user(ALVO, source="slash")

    assert relatorio.status is ErasureStatus.OK and relatorio.counts["saved_queue"] == 2
    assert not any(arquivo.exists() for arquivo in alvo) and all(arquivo.exists() for arquivo in vizinhos)


@pytest.mark.asyncio
async def test_t22_oserror_ao_apagar_vira_partial_saved_queue_os_outros_sao_apagados_e_sem_texto_de_excecao(
    mundo, pastas, monkeypatch, caplog
):
    json_dir, _ = pastas
    fila = _escrever(json_dir, f"{ALVO}.json")
    await gravar_usuario(mundo, ALVO, token="T" * 50)
    real = Path.unlink

    def unlink_falha(self, *args, **kwargs):
        if self.name == fila.name:
            raise PermissionError("detalhe-do-disco-secreto")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_falha)

    with caplog.at_level(logging.INFO):
        relatorio = await mundo.service.erase_user(ALVO, source="slash")

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("saved_queue",)
    assert await mundo.database.find_doc(ALVO, db_name="users", collection="global") is None  # o resto saiu
    assert fila.exists()
    assert "detalhe-do-disco-secreto" not in caplog.text + repr(relatorio)


@pytest.mark.asyncio
async def test_t22_arquivo_que_volta_depois_do_primeiro_apagamento_e_apagado_na_passada_1(mundo, pastas, monkeypatch):
    json_dir, _ = pastas
    fila = _escrever(json_dir, f"{ALVO}.json")
    import empiremuse.utils.music.saved_queue as saved_queue

    original = saved_queue.delete_saved_queues
    voltou = []

    def apaga_e_um_salvar_concorrente_recria(user_id):
        removidos = original(user_id)
        if not voltou:
            voltou.append(True)
            _escrever(json_dir, fila.name, "de-volta")
        return removidos

    monkeypatch.setattr(saved_queue, "delete_saved_queues", apaga_e_um_salvar_concorrente_recria)

    relatorio = await mundo.service.erase_user(ALVO, source="slash")

    assert relatorio.status is ErasureStatus.OK and not fila.exists() and relatorio.counts["saved_queue"] == 2


@pytest.mark.asyncio
async def test_t22_arquivo_que_sempre_volta_vira_partial(mundo, pastas, monkeypatch):
    json_dir, _ = pastas
    fila = _escrever(json_dir, f"{ALVO}.json")
    import empiremuse.utils.music.saved_queue as saved_queue

    original = saved_queue.delete_saved_queues

    def apaga_e_recria(user_id):
        removidos = original(user_id)
        _escrever(json_dir, fila.name, "de-volta")
        return removidos

    monkeypatch.setattr(saved_queue, "delete_saved_queues", apaga_e_recria)

    relatorio = await mundo.service.erase_user(ALVO, source="slash")

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("saved_queue",)
