"""A24/A25 (item 18, P4) — ``empiremuse/updater/docker_flow.py``: significado do código de saída, plano, início e acompanhamento do auxiliar.

O ``ProxyClient`` é um fake em memória que grava a ordem das chamadas; o relógio e o ``sleep`` são injetados (nada dorme). O pendente é o arquivo real em
``tmp_path``. Releases/digest/CHANGELOG são trocados no módulo (nos dois lugares possíveis, para não depender do estilo de import).
"""

import asyncio
import importlib
from collections.abc import Callable
from typing import Any

import pytest

from empiremuse.rules.owner_shell import MASK
from empiremuse.rules.release import Version, changelog_sections
from empiremuse.updater import releases
from empiremuse.updater.git_flow import UpdateError
from empiremuse.updater.state import PendingUpdate, UpdateRequest, pending_path, read_pending, write_pending

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

AGORA = 1_800_000_000.0
DE, PARA = Version(2, 0, 0), Version(2, 1, 0)
DIGEST = "sha256:" + "ab" * 32
PEDIDO = UpdateRequest(bot_id=111, channel_id=222, requested_by=333)
ANDAMENTO = "Já há uma atualização em andamento."


@pytest.fixture
def docker_flow():
    return importlib.import_module("empiremuse.updater.docker_flow")


def plano(docker_flow, changelog: str = "- novidade") -> Any:
    return docker_flow.DockerPlan(current=DE, target=PARA, digest=DIGEST, changelog=changelog)


# ============================================================================ describe_exit

SIGNIFICADOS = {
    64: "versão alvo inválida; nada mudou",
    65: (
        "o compose do projeto não é o esperado (formato da linha `image:` sem suporte) ou as permissões do projeto não são "
        "seguras (dono, modo ou link simbólico); nada mudou. Veja o guia de auto-update"
    ),
    66: "não consegui baixar a imagem; nada mudou",
    67: "a versão alvo não é maior que a instalada; nada mudou",
    68: "a imagem baixada não corresponde à versão anunciada; nada mudou",
    70: "a atualização falhou e o bot voltou para a versão anterior",
    71: "a atualização falhou e não consegui voltar; confira o servidor",
    0: "atualização concluída",
}
DESCONHECIDO = "resultado desconhecido; confira o servidor"


@pytest.mark.parametrize(("codigo", "texto"), list(SIGNIFICADOS.items()), ids=[str(c) for c in SIGNIFICADOS])
def test_each_helper_exit_code_has_its_exact_meaning(docker_flow, codigo, texto):
    assert docker_flow.describe_exit(codigo) == texto == docker_flow.HELPER_EXIT_MEANINGS[codigo]


@pytest.mark.parametrize("codigo", [1, 2, 69, 72, 124, 127, 255, -1, None])
def test_any_other_code_or_none_is_unknown(docker_flow, codigo):
    assert docker_flow.describe_exit(codigo) == DESCONHECIDO


def test_no_meaning_leaks_a_host_path_uid_or_file_name(docker_flow):
    for texto in docker_flow.HELPER_EXIT_MEANINGS.values():
        assert "/" not in texto and "docker-compose" not in texto and "uid" not in texto.lower()


# ============================================================================ plan_docker_update


def trocar(monkeypatch: pytest.MonkeyPatch, docker_flow, nome: str, valor: Callable[..., Any]) -> None:
    monkeypatch.setattr(releases, nome, valor)
    if hasattr(docker_flow, nome):
        monkeypatch.setattr(docker_flow, nome, valor)


def release(versao: Version) -> releases.Release:
    return releases.Release(versao, f"v{versao}", f"https://exemplo.invalido/{versao}/image-digest.txt")


@pytest.fixture
def github(monkeypatch, docker_flow):
    """Releases 2.0.0, 2.1.0 e 2.2.0 publicadas; registra o que foi consultado."""
    consultas: dict[str, list[Any]] = {"digest": [], "changelog": []}

    async def lista(session):
        return [release(Version(2, 2, 0)), release(Version(2, 1, 0)), release(Version(2, 0, 0))]

    async def digest(session, rel):
        consultas["digest"].append(rel.tag)
        return DIGEST

    async def changelog(session, tag):
        consultas["changelog"].append(tag)
        return "## [2.2.0]\n- nova\n\n## [2.1.0]\n- meio\n\n## [2.0.0]\n- base\n"

    for nome, fn in (("list_releases", lista), ("fetch_image_digest", digest), ("fetch_changelog", changelog)):
        trocar(monkeypatch, docker_flow, nome, fn)
    return consultas


async def test_the_plan_targets_the_newest_release_with_its_digest_and_trimmed_changelog(docker_flow, github):
    resultado = await docker_flow.plan_docker_update(object(), DE)

    assert (resultado.current, resultado.target, resultado.digest) == (DE, Version(2, 2, 0), DIGEST)
    esperado = changelog_sections(
        "## [2.2.0]\n- nova\n\n## [2.1.0]\n- meio\n\n## [2.0.0]\n- base\n", DE, Version(2, 2, 0), 3500
    )
    assert resultado.changelog == esperado and github["digest"] == ["v2.2.0"] and github["changelog"] == ["v2.2.0"]


async def test_a_changelog_failure_does_not_drop_the_plan(docker_flow, github, monkeypatch):
    async def falha(session, tag):
        raise UpdateError("sem changelog")

    trocar(monkeypatch, docker_flow, "fetch_changelog", falha)

    resultado = await docker_flow.plan_docker_update(object(), DE)

    assert resultado.target == Version(2, 2, 0) and resultado.changelog == ""


@pytest.mark.parametrize("quem", ["list_releases", "fetch_image_digest"])
async def test_release_or_digest_errors_rise_as_update_errors(docker_flow, github, monkeypatch, quem):
    async def falha(*args):
        raise UpdateError("Não consegui consultar.")

    trocar(monkeypatch, docker_flow, quem, falha)

    with pytest.raises(UpdateError, match="Não consegui consultar"):
        await docker_flow.plan_docker_update(object(), DE)


# ============================================================================ start_docker_update


class ClienteFalso:
    """Registra a ordem das chamadas; ``falhas[nome]`` (exceção) faz a chamada levantar; ``estado`` é o que ``helper_state`` devolve."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.eventos: list[str] = []
        self.argumentos: dict[str, Any] = {}
        self.falhas: dict[str, BaseException] = {}
        self.estado: Any = None
        self.arquivo_no_create: PendingUpdate | None | bool = False
        self.linhas: list[str] = []

    def _passo(self, nome: str) -> None:
        self.eventos.append(nome)
        if nome in self.falhas:
            raise self.falhas[nome]

    async def helper_state(self):
        self._passo("state")
        return self.estado

    async def remove_helper(self):
        self._passo("remove")

    async def create_helper(self, alvo: str):
        self.argumentos["create"] = alvo
        self.arquivo_no_create = read_pending(self.runtime, AGORA)
        self._passo("create")

    async def start_helper(self):
        self._passo("start")

    async def helper_logs(self, tail: int = 20):
        self.argumentos["tail"] = tail
        self._passo("logs")
        return list(self.linhas)


def estado(*, rodando: bool, codigo: int = 0) -> Any:
    from empiremuse.updater.proxy_client import HelperState

    return HelperState("running" if rodando else "exited", rodando, codigo, "ini", "fim")


@pytest.fixture
def cliente(tmp_path):
    return ClienteFalso(tmp_path)


async def iniciar(docker_flow, cliente, tmp_path):
    return await docker_flow.start_docker_update(cliente, plano(docker_flow), PEDIDO, tmp_path, now=lambda: AGORA)


def pendente_esperado(**campos) -> PendingUpdate:
    base = {
        "flow": "docker",
        "from_version": DE,
        "to_version": PARA,
        "bot_id": 111,
        "channel_id": 222,
        "requested_by": 333,
        "created_at": AGORA,
        "digest": DIGEST,
        "boots": 0,
        "status": "pending",
    }
    return PendingUpdate(**{**base, **campos})


async def test_the_pending_update_is_on_disk_before_the_create_and_the_target_carries_the_digest(
    docker_flow, cliente, tmp_path
):
    devolvido = await iniciar(docker_flow, cliente, tmp_path)

    assert cliente.arquivo_no_create == pendente_esperado() == devolvido
    assert cliente.argumentos["create"] == f"{PARA}@{DIGEST}"
    assert cliente.eventos == ["state", "create", "start"]
    assert read_pending(tmp_path, AGORA) == devolvido


async def test_a_stopped_helper_is_removed_before_anything_is_written(docker_flow, cliente, tmp_path):
    cliente.estado = estado(rodando=False)

    await iniciar(docker_flow, cliente, tmp_path)

    assert cliente.eventos == ["state", "remove", "create", "start"]


async def test_a_running_helper_refuses_the_update_without_writing_anything(docker_flow, cliente, tmp_path):
    cliente.estado = estado(rodando=True)

    with pytest.raises(UpdateError) as erro:
        await iniciar(docker_flow, cliente, tmp_path)

    assert str(erro.value) == ANDAMENTO and cliente.eventos == ["state"] and not pending_path(tmp_path).exists()


async def test_a_failing_removal_of_the_stopped_helper_writes_nothing(docker_flow, cliente, tmp_path):
    cliente.estado = estado(rodando=False)
    cliente.falhas["remove"] = UpdateError("não removeu")

    with pytest.raises(UpdateError, match="não removeu"):
        await iniciar(docker_flow, cliente, tmp_path)

    assert cliente.eventos == ["state", "remove"] and not pending_path(tmp_path).exists()


async def test_a_failing_write_of_the_pending_update_runs_no_create(docker_flow, cliente, tmp_path, monkeypatch):
    def falha(*args, **kwargs):
        raise OSError(28, "sem espaço")

    monkeypatch.setattr("empiremuse.updater.state.write_pending", falha)
    if hasattr(docker_flow, "write_pending"):
        monkeypatch.setattr(docker_flow, "write_pending", falha)

    with pytest.raises(OSError):
        await iniciar(docker_flow, cliente, tmp_path)

    assert "create" not in cliente.eventos and not pending_path(tmp_path).exists()


@pytest.mark.parametrize("passo", ["create", "start"])
async def test_a_failing_create_or_start_removes_the_helper_then_clears_the_pending_update(
    docker_flow, cliente, tmp_path, passo
):
    cliente.falhas[passo] = UpdateError("falhou aqui")

    with pytest.raises(UpdateError, match="falhou aqui"):
        await iniciar(docker_flow, cliente, tmp_path)

    assert cliente.eventos[-1] == "remove" and not pending_path(tmp_path).exists()


@pytest.mark.parametrize("passo", ["create", "start"])
async def test_a_cancellation_during_create_or_start_still_cleans_up_and_stays_a_cancellation(
    docker_flow, cliente, tmp_path, passo
):
    cliente.falhas[passo] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await iniciar(docker_flow, cliente, tmp_path)

    assert cliente.eventos[-1] == "remove" and not pending_path(tmp_path).exists()


async def test_when_the_cleanup_removal_fails_the_pending_update_stays_and_the_original_error_rises(
    docker_flow, cliente, tmp_path
):
    cliente.falhas["start"] = UpdateError("falhou o start")
    cliente.falhas["remove"] = UpdateError("a remoção também falhou")

    with pytest.raises(UpdateError, match="falhou o start"):
        await iniciar(docker_flow, cliente, tmp_path)

    assert read_pending(tmp_path, AGORA) == pendente_esperado()


# ============================================================================ watch_helper


class Relatorio:
    def __init__(self) -> None:
        self.canal: list[str] = []
        self.dms: list[str] = []

    async def channel(self, text: str) -> None:
        self.canal.append(text)

    async def dm(self, text: str) -> None:
        self.dms.append(text)


class Relogio:
    def __init__(self) -> None:
        self.pausas: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.pausas.append(segundos)


class ClienteQueObserva(ClienteFalso):
    """``helper_state`` devolve ``sequencia`` em ordem (o último valor se repete)."""

    def __init__(self, runtime, sequencia: list[Any]) -> None:
        super().__init__(runtime)
        self.sequencia = sequencia

    async def helper_state(self):
        self._passo("state")
        return self.sequencia.pop(0) if len(self.sequencia) > 1 else self.sequencia[0]


@pytest.fixture
def pendente(tmp_path):
    p = pendente_esperado()
    write_pending(tmp_path, p)
    return p


async def vigiar(docker_flow, cliente, pendente, tmp_path, **extra):
    relatorio, relogio = Relatorio(), Relogio()
    resultado = await docker_flow.watch_helper(
        cliente, pendente, tmp_path, relatorio, sleep=relogio, **{"timeout": 900, "interval": 5, **extra}
    )
    return resultado, relatorio, relogio


async def test_a_missing_helper_reports_unknown_clears_the_pending_update_and_returns_none(
    docker_flow, tmp_path, pendente
):
    cliente = ClienteQueObserva(tmp_path, [None])

    resultado, relatorio, _ = await vigiar(docker_flow, cliente, pendente, tmp_path)

    assert resultado is None and relatorio.canal == [DESCONHECIDO] and not pending_path(tmp_path).exists()


async def test_a_successful_helper_is_reported_removed_and_cleared_after_waiting_for_it(
    docker_flow, tmp_path, pendente
):
    cliente = ClienteQueObserva(tmp_path, [estado(rodando=True), estado(rodando=True), estado(rodando=False, codigo=0)])

    resultado, relatorio, relogio = await vigiar(docker_flow, cliente, pendente, tmp_path)

    assert resultado == 0 and relogio.pausas == [5, 5]
    assert relatorio.canal == [SIGNIFICADOS[0]] and relatorio.dms == []
    assert cliente.eventos[-1] == "remove" and not pending_path(tmp_path).exists()


async def test_a_failed_helper_tells_the_channel_only_the_meaning_and_the_dm_the_masked_log_lines(
    docker_flow, tmp_path, pendente
):
    from tests.rules.test_bot_tokens import token

    cliente = ClienteQueObserva(tmp_path, [estado(rodando=False, codigo=66)])
    cliente.linhas = ["empiremuse-updater: pull", "token TOKEN-SECRETO vazou " + token()] + [
        f"linha {n}" for n in range(30)
    ]

    resultado, relatorio, _ = await vigiar(
        docker_flow, cliente, pendente, tmp_path, secrets=frozenset({"TOKEN-SECRETO"})
    )

    assert resultado == 66 and relatorio.canal == [SIGNIFICADOS[66]]
    assert cliente.argumentos["tail"] == 20
    (mensagem,) = relatorio.dms
    assert "empiremuse-updater: pull" in mensagem and "TOKEN-SECRETO" not in mensagem and MASK in mensagem
    assert (
        len([linha for linha in mensagem.splitlines() if linha.strip()]) <= 20 + 2
    )  # as linhas, mais no máximo um cabeçalho curto
    assert token() not in mensagem
    assert cliente.eventos[-1] == "remove" and not pending_path(tmp_path).exists()


@pytest.mark.parametrize("etapa", ["compose", "compose-perm"])
async def test_exit_65_has_the_same_channel_text_for_both_causes_and_the_step_only_in_the_dm(
    docker_flow, tmp_path, pendente, etapa
):
    cliente = ClienteQueObserva(tmp_path, [estado(rodando=False, codigo=65)])
    cliente.linhas = [f"empiremuse-updater: {etapa}"]

    resultado, relatorio, _ = await vigiar(docker_flow, cliente, pendente, tmp_path)

    assert resultado == 65 and relatorio.canal == [SIGNIFICADOS[65]]
    assert (
        f"empiremuse-updater: {etapa}" not in relatorio.canal[0] and f"empiremuse-updater: {etapa}" in relatorio.dms[0]
    )


async def test_a_helper_still_running_at_the_timeout_reports_unknown_and_keeps_everything(
    docker_flow, tmp_path, pendente
):
    cliente = ClienteQueObserva(tmp_path, [estado(rodando=True)])

    resultado, relatorio, relogio = await vigiar(docker_flow, cliente, pendente, tmp_path, timeout=20, interval=5)

    assert resultado is None and relatorio.canal == [DESCONHECIDO] and sum(relogio.pausas) >= 20
    assert "remove" not in cliente.eventos and read_pending(tmp_path, AGORA) == pendente


async def test_a_client_error_inside_the_loop_reports_the_message_and_keeps_the_pending_update(
    docker_flow, tmp_path, pendente
):
    cliente = ClienteQueObserva(tmp_path, [estado(rodando=True)])
    cliente.falhas["state"] = UpdateError("proxy de atualização inacessível")

    resultado, relatorio, _ = await vigiar(docker_flow, cliente, pendente, tmp_path)

    assert resultado is None and relatorio.canal == ["proxy de atualização inacessível"]
    assert read_pending(tmp_path, AGORA) == pendente


# ============================================================================ DM do log: cabe no limite do Discord, mascara antes de truncar


LIMITE_DM = 1900


def lixo_unico(n: int) -> str:
    return "".join(chr(65 + (i * 7 + i // 5) % 26) for i in range(n))


async def mandar_log(docker_flow, tmp_path, pendente, linhas: list[str], secrets=frozenset()):
    cliente = ClienteQueObserva(tmp_path, [estado(rodando=False, codigo=70)])
    cliente.linhas = linhas
    _, relatorio, _ = await vigiar(docker_flow, cliente, pendente, tmp_path, secrets=secrets)
    return relatorio.dms


async def test_a_long_log_goes_in_one_dm_within_the_discord_limit_keeping_the_end(docker_flow, tmp_path, pendente):
    linhas = [f"linha {n:02d} " + "y" * 290 for n in range(20)]  # > 4000 caracteres

    dms = await mandar_log(docker_flow, tmp_path, pendente, linhas)

    (dm,) = dms
    assert len(dm) <= LIMITE_DM
    assert linhas[-1][-100:] in dm and linhas[0] not in dm
    corpo = dm.split("\n", 1)[1]
    assert corpo.startswith("…")


async def test_a_secret_that_would_straddle_the_cut_never_shows_not_even_partially(docker_flow, tmp_path, pendente):
    segredo = lixo_unico(600)
    linhas = (
        [f"antes {n:02d} " + "a" * 190 for n in range(10)]
        + ["x" * 50 + segredo + "y" * 50]
        + [f"depois {n:02d} " + "b" * 290 for n in range(5)]
    )

    (dm,) = await mandar_log(docker_flow, tmp_path, pendente, linhas, secrets=frozenset({segredo}))

    assert len(dm) <= LIMITE_DM and "depois 04" in dm
    assert segredo not in dm
    assert all(segredo[inicio : inicio + 12] not in dm for inicio in range(0, 600 - 12, 12))  # nem um pedaço
