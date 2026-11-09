"""Hardening dos comandos de owner: ambiente mínimo do subprocesso, saída só por DM e mascarada, log e jishaku opcional."""

import asyncio
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.cogs import legacy_cmds
from empiremuse.cogs.legacy_cmds import Owner, run_command
from empiremuse.rules.owner_shell import MASK
from empiremuse.utils.client import load_jishaku_if_enabled
from empiremuse.utils.music.errors import GenericError

pytestmark = pytest.mark.regression

SEGREDO = "segredo-do-env-123"


def run(coro):
    return asyncio.run(coro)


def cog_e_contexto():
    cog = Owner.__new__(Owner)
    cog.bot = SimpleNamespace(
        config={"TOKEN": SEGREDO}, pool=SimpleNamespace(secret_values=lambda: frozenset({SEGREDO}))
    )
    ctx = MagicMock()
    ctx.author.id = 42
    ctx.author.send = AsyncMock()
    ctx.reply = AsyncMock()
    ctx.send = AsyncMock()
    ctx.message.id = 7
    ctx.message.add_reaction = AsyncMock()
    return cog, ctx


def test_o_subprocesso_nao_enxerga_o_env_do_processo(monkeypatch):
    monkeypatch.setenv("TOKEN_RESERVE_9", SEGREDO)

    saida = run(run_command(f'"{sys.executable}" -c "import os; print(sorted(os.environ))"'))

    assert "TOKEN_RESERVE_9" not in saida and SEGREDO not in saida


def test_git_continua_funcionando_com_o_env_minimo():
    assert "git version" in run(run_command("git --version"))


def test_shell_manda_a_saida_mascarada_so_por_dm(monkeypatch):
    cog, ctx = cog_e_contexto()
    monkeypatch.setattr(legacy_cmds, "run_command", AsyncMock(return_value=f"TOKEN={SEGREDO}\n"))

    run(Owner.shell.callback(cog, ctx, command="cat .env"))

    ctx.author.send.assert_awaited_once()
    enviado = ctx.author.send.await_args.kwargs["content"]
    assert SEGREDO not in enviado and MASK in enviado
    ctx.reply.assert_not_awaited()
    ctx.send.assert_not_awaited()


def test_shell_com_dm_bloqueado_avisa_sem_mandar_a_saida_ao_canal(monkeypatch):
    cog, ctx = cog_e_contexto()
    ctx.author.send.side_effect = disnake.Forbidden(MagicMock(status=403, reason="Forbidden"), "DM fechado")
    monkeypatch.setattr(legacy_cmds, "run_command", AsyncMock(return_value=f"TOKEN={SEGREDO}\n"))

    with pytest.raises(GenericError) as erro:
        run(Owner.shell.callback(cog, ctx, command="cat .env"))

    assert SEGREDO not in erro.value.text
    ctx.reply.assert_not_awaited()
    ctx.send.assert_not_awaited()


def test_log_de_execucao_registra_autor_e_comando_sem_segredo(monkeypatch, caplog):
    cog, ctx = cog_e_contexto()
    monkeypatch.setattr(legacy_cmds, "run_command", AsyncMock(return_value="saída qualquer\n"))

    with caplog.at_level(logging.WARNING, logger=legacy_cmds.logger.name):
        run(Owner.shell.callback(cog, ctx, command=f"curl -H 'Authorization: {SEGREDO}' http://x"))

    registro = caplog.text
    assert "usuário=42" in registro and "comando=shell" in registro and "curl" in registro
    assert SEGREDO not in registro and "saída qualquer" not in registro


@pytest.mark.critical
def test_normalized_quoted_token_is_masked_in_shell_and_owner_command_log(monkeypatch, caplog):
    from empiremuse.rules.bot_tokens import load_bot_tokens
    from empiremuse.utils import client
    from tests.rules.test_bot_tokens import token

    monkeypatch.setattr(client.os, "environ", {})
    pool = client.BotPool.__new__(client.BotPool)
    pool.config = {"TOKEN": '"' + token() + '"'}
    pool._token_load = load_bot_tokens(pool.config)
    cog, ctx = cog_e_contexto()
    cog.bot.pool = pool
    monkeypatch.setattr(legacy_cmds, "run_command", AsyncMock(return_value=token()))
    with caplog.at_level(logging.WARNING, logger=legacy_cmds.logger.name):
        run(Owner.shell.callback(cog, ctx, command="echo " + token()))
    assert token() not in caplog.text
    assert "***" in caplog.text
    content = ctx.author.send.await_args.kwargs["content"]
    assert token() not in content and "***" in content


def _bot_jishaku(ligado, owner_ids):
    bot = MagicMock()
    bot.config = {"ENABLE_JISHAKU": ligado, "OWNER_IDS": owner_ids}
    return bot


def test_jishaku_desligado_nao_carrega():
    bot = _bot_jishaku(False, "1||2")

    assert load_jishaku_if_enabled(bot) is False
    bot.load_extension.assert_not_called()


@pytest.mark.parametrize("owner_ids", ["", " || ", "abc||x1"])
def test_jishaku_ligado_sem_owner_ids_nao_carrega(monkeypatch, owner_ids, caplog):
    monkeypatch.delenv("JISHAKU_OWNER_IDS", raising=False)
    bot = _bot_jishaku(True, owner_ids)

    assert load_jishaku_if_enabled(bot) is False
    bot.load_extension.assert_not_called()
    assert "OWNER_IDS" in caplog.text


def test_jishaku_ligado_com_owner_ids_carrega_e_converte_separador(monkeypatch):
    monkeypatch.delenv("JISHAKU_OWNER_IDS", raising=False)
    monkeypatch.delenv("JISHAKU_MASK_SECRETS", raising=False)
    monkeypatch.delenv("JISHAKU_DM_ONLY", raising=False)
    bot = _bot_jishaku(True, "111||abc||222")

    assert load_jishaku_if_enabled(bot) is True
    bot.load_extension.assert_called_once_with("jishaku")
    assert os.environ["JISHAKU_OWNER_IDS"] == "111,222"
    assert os.environ["JISHAKU_MASK_SECRETS"] == "true"
    assert os.environ["JISHAKU_DM_ONLY"] == "true"
