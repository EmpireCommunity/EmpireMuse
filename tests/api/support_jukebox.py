"""Mundo da Jukebox na API (B4): o servidor 100 do ``ChannelWorld`` (canais reais de disnake, banco em memória) atrás da API de
teste. O usuário da sessão é o ``1001`` (administrador); ``2002`` também gerencia o servidor (para provar o isolamento das
tarefas entre usuários do mesmo servidor)."""

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from empiremuse.panel.services.jukebox import JukeboxService

from tests.api.support import ApiTestCase, body_of
from tests.panel.helpers_jukebox import GID, ChannelWorld
from tests.panel.helpers_settings import UID

USER = str(UID)
OTHER_USER = "2002"
BASE = f"/guilds/{GID}/jukebox"


class JukeboxApiCase(ApiTestCase):
    def make_pool(self):
        self.world = ChannelWorld()
        self.world.guild.members[int(OTHER_USER)] = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=True, manage_guild=True)
        )
        return self.world.pool

    def get_app(self):
        app = super().get_app()
        self.oauth.guilds = self.world.raw
        return app

    def setUp(self):
        super().setUp()
        self.io_loop.run_sync(self.world.premigrate)
        self.token = self.new_session(USER)

    def call(self, method, path, body=None, *, session=None, key=None, **extra):
        headers = dict(extra.pop("headers", {}))
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.api(method, path, session=session or self.token, body=body, headers=headers, **extra)

    def state(self, session=None) -> dict:
        return body_of(self.call("GET", BASE, session=session))

    def drain_tasks(self) -> None:
        """Deixa as tarefas em andamento terminarem (sem relógio nem ``sleep``: espera as próprias ``asyncio.Task``)."""

        async def wait():
            pending = list(self.ctx.jukebox_service.runner._running)
            await asyncio.gather(*pending, return_exceptions=True)

        self.io_loop.run_sync(wait)

    @contextmanager
    def held_jobs(self):
        """As tarefas ficam em andamento até o evento ser liberado e **não mexem** no Discord nem no banco (o estado que
        a confirmação conferiu continua o mesmo)."""
        release = asyncio.Event()

        def job_of(_service, _ctx, *_args):
            async def job() -> str:
                await release.wait()
                return "ok"

            return job

        with patch.object(JukeboxService, "_reset_job", job_of), patch.object(JukeboxService, "_setup_job", job_of):
            try:
                yield release
            finally:
                release.set()
                self.drain_tasks()

    def start_task(self, body, **extra):
        return self.call("POST", BASE + "/tasks", body, **extra)


def dump(response) -> str:
    return json.dumps(body_of(response))
