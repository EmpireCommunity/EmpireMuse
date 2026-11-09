# ruff: noqa: F811
"""Webhook de erro: a falha do envio libera o limite por servidor e o webhook não menciona ninguém."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest
from disnake.ext import commands

from empiremuse.cogs import error_handler
from empiremuse.cogs.error_handler import ErrorHandler
from tests.cogs.test_error_handler import _interacao, handler

pytestmark = pytest.mark.critical


class FalhaNoWebhook(Exception):
    pass


class TestFalhaNaoTravaOServidor:
    def preparar(self, h: ErrorHandler) -> None:
        h.webhook_max_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=True)
        h.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = "https://discord.com/api/webhooks/1/x"

    @pytest.mark.asyncio
    async def test_a_failing_webhook_on_an_interaction_error_releases_the_guild(self, handler, monkeypatch):
        self.preparar(handler)
        monkeypatch.setattr(error_handler, "send_message", AsyncMock())
        inter = _interacao()
        handler.send_webhook = AsyncMock(side_effect=[FalhaNoWebhook("400"), None])

        await handler.process_interaction_error(inter, RuntimeError("boom"))
        await asyncio.wait_for(handler.process_interaction_error(inter, RuntimeError("boom")), timeout=2)

        assert handler.send_webhook.await_count == 2


class TestWebhookSemMencoes:
    @pytest.mark.asyncio
    async def test_the_webhook_never_mentions_anyone(self, handler, monkeypatch):
        enviados: list[dict] = []

        class Sessao:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Webhook:
            async def send(self, **kwargs):
                enviados.append(kwargs)

        monkeypatch.setattr(error_handler, "ClientSession", Sessao)
        monkeypatch.setattr(error_handler.disnake.Webhook, "from_url", lambda *a, **k: Webhook())
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = "https://discord.com/api/webhooks/1/x"
        handler.bot.user = SimpleNamespace(
            name="Bot",
            display_avatar=SimpleNamespace(replace=lambda **k: SimpleNamespace(url="https://cdn.exemplo/a.png")),
        )

        await ErrorHandler.send_webhook(handler, content="@everyone <@&1> <@2>", embed=disnake.Embed(title="x"))

        (enviado,) = enviados
        mencoes = enviado["allowed_mentions"]
        assert mencoes.everyone is False and mencoes.roles is False and mencoes.users is False
