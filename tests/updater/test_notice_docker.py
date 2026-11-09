"""A25 (item 18, P4) — o aviso pós-boot do fluxo Docker: ``notice.deliver_docker_notice`` e o ramo docker de ``on_pool_ready``.

O cliente do proxy é um fake (estados do auxiliar em sequência); o ``sleep`` e a versão da imagem são injetados (nada dorme, sem relógio real).
Bots, canais e usuários reaproveitam os fakes de ``test_notice``. O aviso é único: o pendente some ANTES do envio; o auxiliar é sempre removido
no fim (erro ignorado).
"""

import dataclasses
import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from empiremuse.rules.release import Version
from empiremuse.updater import notice
from empiremuse.updater.git_flow import UpdateError
from empiremuse.updater.state import PendingUpdate, pending_path, read_pending, write_pending
from empiremuse import paths
from tests.support.alvo_git import ALVO_GIT
from tests.updater.test_notice import AGORA, CANAL, CHANGELOG, bot_falso, pool_com, sem_mencoes

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

DE, PARA = Version(2, 0, 0), Version(2, 1, 0)
DIGEST = "sha256:" + "ab" * 32
DESCONHECIDO = "resultado desconhecido; confira o servidor"


def pendente_docker(**campos) -> PendingUpdate:
    base = {
        "flow": "docker",
        "from_version": DE,
        "to_version": PARA,
        "bot_id": 111,
        "channel_id": CANAL,
        "requested_by": 333,
        "created_at": AGORA - 120,
        "digest": DIGEST,
        "boots": 0,
        "status": "ready",
    }
    return PendingUpdate(**{**base, **campos})


def estado(*, rodando: bool, codigo: int = 0) -> Any:
    from empiremuse.updater.proxy_client import HelperState

    return HelperState("running" if rodando else "exited", rodando, codigo, "ini", "fim")


class ClienteFalso:
    def __init__(self, sequencia: list[Any], runtime=None) -> None:
        self.sequencia = sequencia
        self.runtime = runtime
        self.remocoes = 0
        self.eventos: list[str] = []
        self.linhas: list[str] = []
        self.falha_nos_logs: Exception | None = None
        self.falha_na_remocao: Exception | None = None
        self.status_no_primeiro_estado: str | None = None

    async def helper_state(self):
        if self.runtime is not None and self.status_no_primeiro_estado is None:
            lido = read_pending(self.runtime, AGORA)
            self.status_no_primeiro_estado = lido.status if lido else "ausente"
        return self.sequencia.pop(0) if len(self.sequencia) > 1 else self.sequencia[0]

    async def remove_helper(self):
        self.remocoes += 1
        self.eventos.append("remove")
        if self.falha_na_remocao:
            raise self.falha_na_remocao

    async def helper_logs(self, tail: int = 20):
        self.eventos.append("logs")
        if self.falha_nos_logs:
            raise self.falha_nos_logs
        return list(self.linhas)


class Pausas:
    def __init__(self) -> None:
        self.valores: list[float] = []

    async def __call__(self, segundos: float) -> None:
        self.valores.append(segundos)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    pasta = tmp_path / "runtime"
    pasta.mkdir()
    projeto = tmp_path / "projeto"
    projeto.mkdir()
    (projeto / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(paths, "PROJECT_DIR", projeto)
    return pasta


@pytest.fixture
def docker_flow():
    return importlib.import_module("empiremuse.updater.docker_flow")


async def entregar_docker(pool, cliente, runtime, versao, **extra):
    pausas = Pausas()
    await notice.deliver_docker_notice(
        pool,
        cliente,
        runtime_dir=runtime,
        now=lambda: AGORA,
        image_version_fn=lambda: versao,
        sleep=pausas,
        **{"timeout": 900, "interval": 5, **extra},
    )
    return pausas


# ============================================================================ deliver_docker_notice


async def test_success_is_a_green_embed_with_the_changelog_and_removes_the_helper(runtime):
    bot = bot_falso()
    cliente = ClienteFalso([estado(rodando=False, codigo=0)])
    write_pending(runtime, pendente_docker())

    await entregar_docker(pool_com(bot), cliente, runtime, PARA)

    chamada = bot.canal.send.await_args
    embed = chamada.kwargs["embed"]
    assert "Atualizado de 2.0.0 para 2.1.0" in embed.description and "novidade da 2.1.0" in embed.description
    assert embed.colour.value == notice.COLOR_OK and sem_mencoes(chamada)
    assert cliente.remocoes == 1 and not pending_path(runtime).exists()


@pytest.mark.parametrize("codigo", [70, 71, 66, 67, 68, 65, 64])
async def test_a_helper_that_failed_with_the_old_version_still_running_is_a_red_embed_with_the_meaning(
    runtime, docker_flow, codigo
):
    bot = bot_falso()
    cliente = ClienteFalso([estado(rodando=False, codigo=codigo)])
    write_pending(runtime, pendente_docker())

    await entregar_docker(pool_com(bot), cliente, runtime, DE)

    embed = bot.canal.send.await_args.kwargs["embed"]
    assert embed.colour.value == notice.COLOR_FAIL and docker_flow.describe_exit(codigo) in embed.description
    assert cliente.remocoes == 1 and not pending_path(runtime).exists()


async def test_a_running_helper_is_waited_for_with_the_injected_sleep(runtime):
    bot = bot_falso()
    cliente = ClienteFalso([estado(rodando=True), estado(rodando=True), estado(rodando=False, codigo=0)])
    write_pending(runtime, pendente_docker())

    pausas = await entregar_docker(pool_com(bot), cliente, runtime, PARA)

    assert pausas.valores == [5, 5]
    assert bot.canal.send.await_args.kwargs["embed"].colour.value == notice.COLOR_OK


async def test_a_helper_still_running_at_the_timeout_is_an_unknown_result(runtime):
    bot = bot_falso()
    cliente = ClienteFalso([estado(rodando=True)])
    write_pending(runtime, pendente_docker())

    pausas = await entregar_docker(pool_com(bot), cliente, runtime, DE, timeout=20, interval=5)

    embed = bot.canal.send.await_args.kwargs["embed"]
    assert sum(pausas.valores) >= 20
    assert embed.colour.value == notice.COLOR_UNKNOWN and DESCONHECIDO in embed.description
    assert cliente.remocoes == 1 and not pending_path(runtime).exists()


@pytest.mark.parametrize(
    ("sequencia", "versao"),
    [([None], PARA), ([estado(rodando=False, codigo=0)], None), ([estado(rodando=False, codigo=0)], Version(9, 9, 9))],
    ids=["auxiliar-ausente", "versao-ilegivel", "versao-inesperada"],
)
async def test_anything_that_cannot_be_confirmed_is_a_yellow_unknown_embed(runtime, sequencia, versao):
    bot = bot_falso()
    write_pending(runtime, pendente_docker())

    await entregar_docker(pool_com(bot), ClienteFalso(sequencia), runtime, versao)

    embed = bot.canal.send.await_args.kwargs["embed"]
    assert embed.colour.value == notice.COLOR_UNKNOWN and DESCONHECIDO in embed.description


async def test_the_pending_update_is_gone_before_the_send_so_the_delivery_is_single(runtime):
    bot = bot_falso()
    estados: list[bool] = []
    original = bot.canal.send

    async def enviar(*args, **kwargs):
        estados.append(pending_path(runtime).exists())
        return await original(*args, **kwargs)

    bot.canal.send = enviar
    write_pending(runtime, pendente_docker())

    await entregar_docker(pool_com(bot), ClienteFalso([estado(rodando=False, codigo=0)]), runtime, PARA)

    assert estados == [False]


# ============================================================================ on_pool_ready, ramo docker


def pool_saudavel(*bots, socket: str):
    pool = pool_com(*bots)
    pool.config = {"UPDATE_PROXY_SOCKET": socket}
    return pool


async def test_on_pool_ready_marks_ready_then_runs_the_docker_branch_with_the_configured_socket(runtime, monkeypatch):
    bot = bot_falso()
    cliente = ClienteFalso([estado(rodando=False, codigo=0)], runtime)
    caminhos: list[str] = []

    def fabrica(caminho: str) -> ClienteFalso:
        caminhos.append(caminho)
        return cliente

    monkeypatch.setattr(notice, "ProxyClient", fabrica)
    monkeypatch.setenv("EMPIREMUSE_VERSION", "2.1.0")
    write_pending(runtime, pendente_docker(status="pending"))

    await notice.on_pool_ready(
        pool_saudavel(bot, socket="/run/empiremuse/updater.sock"), runtime_dir=runtime, now=lambda: AGORA
    )

    assert caminhos == ["/run/empiremuse/updater.sock"]
    assert cliente.status_no_primeiro_estado == "ready"  # gravado antes de qualquer consulta ou envio
    assert "Atualizado de 2.0.0 para 2.1.0" in bot.canal.send.await_args.kwargs["embed"].description
    assert not pending_path(runtime).exists()


async def test_on_pool_ready_without_a_proxy_socket_sends_the_unknown_notice_and_clears_the_pending_update(
    runtime, monkeypatch
):
    """Sem socket não há cliente: o aviso sai como "resultado desconhecido" e o pendente não bloqueia o /update por 24 h."""
    bot = bot_falso()

    def nunca(caminho):
        raise AssertionError("sem socket configurado nenhum ProxyClient pode ser criado")

    monkeypatch.setattr(notice, "ProxyClient", nunca)
    estados: list[bool] = []
    original = bot.canal.send

    async def enviar(*args, **kwargs):
        estados.append(pending_path(runtime).exists())
        return await original(*args, **kwargs)

    bot.canal.send = enviar
    write_pending(runtime, pendente_docker(status="pending"))

    await notice.on_pool_ready(pool_saudavel(bot, socket=""), runtime_dir=runtime, now=lambda: AGORA)

    embed = original.await_args.kwargs["embed"]
    assert embed.colour.value == notice.COLOR_UNKNOWN and DESCONHECIDO in embed.description
    assert estados == [False] and not pending_path(runtime).exists()  # apagado ANTES do envio


# ============================================================================ log do auxiliar por DM também no aviso pós-boot (70/71 não passam pelo watch_helper)


def pool_com_config(*bots, **config):
    pool = pool_com(*bots)
    pool.config = config
    from empiremuse.rules.owner_shell import collect_secret_values

    pool.secret_values = lambda: collect_secret_values(config)
    return pool


async def test_helper_log_uses_pool_secrets_including_normalized_tokens(runtime):
    from empiremuse.rules.bot_tokens import load_bot_tokens
    from empiremuse.utils.client import BotPool
    from tests.rules.test_bot_tokens import token

    bot = bot_falso()
    pool = pool_com_config(bot, TOKEN='"' + token() + '"')
    pool._token_load = load_bot_tokens(pool.config)
    pool.token_secrets = lambda: BotPool.token_secrets(pool)
    pool.secret_values = lambda: BotPool.secret_values(pool)
    cliente = ClienteFalso([estado(rodando=False, codigo=70)])
    cliente.linhas = ["empiremuse-updater: " + token()]
    write_pending(runtime, pendente_docker())

    await entregar_docker(pool, cliente, runtime, DE)

    call = bot.usuario.send.await_args
    text = (call.args[0] if call.args else call.kwargs.get("content") or "") + (
        call.kwargs["embed"].description if call.kwargs.get("embed") else ""
    )
    assert token() not in text and "***" in text


@pytest.mark.parametrize("versao", [DE, None], ids=["voltou-para-a-anterior", "versao-ilegivel"])
async def test_a_failed_helper_sends_the_masked_log_by_dm_before_the_helper_is_removed(runtime, versao):
    bot = bot_falso()
    cliente = ClienteFalso([estado(rodando=False, codigo=70)])
    cliente.linhas = ["empiremuse-updater: up 70", "empiremuse-updater: token TOKEN-SECRETO-123 vazou"]
    write_pending(runtime, pendente_docker())

    await entregar_docker(pool_com_config(bot, TOKEN="TOKEN-SECRETO-123"), cliente, runtime, versao)

    bot.usuario.send.assert_awaited_once()
    chamada = bot.usuario.send.await_args
    texto = (chamada.args[0] if chamada.args else chamada.kwargs.get("content") or "") + (
        chamada.kwargs["embed"].description if chamada.kwargs.get("embed") else ""
    )
    assert "empiremuse-updater: up 70" in texto and "TOKEN-SECRETO-123" not in texto
    assert cliente.eventos == ["logs", "remove"]  # o log é lido ANTES de o auxiliar ser apagado


async def test_a_failing_log_read_does_not_stop_the_notice_or_the_removal(runtime):
    bot = bot_falso()
    cliente = ClienteFalso([estado(rodando=False, codigo=71)])
    cliente.falha_nos_logs = UpdateError("proxy de atualização inacessível")
    write_pending(runtime, pendente_docker())

    await entregar_docker(pool_com_config(bot), cliente, runtime, DE)

    bot.canal.send.assert_awaited_once()
    bot.usuario.send.assert_not_awaited()
    assert cliente.eventos == ["logs", "remove"] and not pending_path(runtime).exists()


async def test_the_post_boot_dm_also_fits_the_limit_keeps_the_end_and_masks_before_cutting(runtime):
    segredo = "".join(chr(65 + (i * 7 + i // 5) % 26) for i in range(600))
    linhas = (
        [f"antes {n:02d} " + "a" * 190 for n in range(10)]
        + ["x" * 50 + segredo + "y" * 50]
        + [f"depois {n:02d} " + "b" * 290 for n in range(5)]
    )
    bot = bot_falso()
    cliente = ClienteFalso([estado(rodando=False, codigo=71)])
    cliente.linhas = linhas
    write_pending(runtime, pendente_docker())

    await entregar_docker(pool_com_config(bot, TOKEN=segredo), cliente, runtime, DE)

    chamada = bot.usuario.send.await_args
    dm = (chamada.args[0] if chamada.args else chamada.kwargs.get("content") or "") + (
        chamada.kwargs["embed"].description if chamada.kwargs.get("embed") else ""
    )
    assert len(dm) <= 1900 and "depois 04" in dm and linhas[0] not in dm
    assert segredo not in dm and all(segredo[i : i + 12] not in dm for i in range(0, 588, 12))


# ============================================================================ D13: ``from_version`` rc nos avisos (A29 h)


def pendente_do_rc(**campos) -> PendingUpdate:
    from tests.support.versao_rc import atual_rc

    return pendente_docker(from_version=atual_rc("2.1.0-rc.2"), **campos)


async def test_an_rc_pending_update_is_kept_by_the_read_so_the_notice_is_not_lost(runtime):
    """Sem a D13 a leitura descartava o pendente com ``from_version`` rc e o dono ficava sem aviso."""
    write_pending(runtime, pendente_do_rc())
    bot = bot_falso()

    await entregar_docker(pool_com(bot), ClienteFalso([estado(rodando=False, codigo=0)]), runtime, PARA)

    bot.canal.send.assert_awaited_once()
