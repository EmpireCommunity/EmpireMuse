"""``GET /painel/radios/logo/<uuid>.png``: o logo de uma estação, servido pela própria origem do painel (item 8, R4).

O navegador nunca sai da nossa origem (a CSP ``img-src`` não muda): a página monta o ``src`` a partir do uuid
validado, e esta rota devolve o PNG já re-codificado que o diretório (``pool.radio.directory.logo_png``, Turbine)
guarda em memória para as estações que a página acabou de mostrar (busca, cascata, principal e reserva por
``by_uuid``), ou o ícone padrão do painel quando não há logo. Só com sessão; a chave é só o uuid canônico da rota
(qualquer parâmetro é ignorado); o cliente nunca vê a URL do logo nem o motivo de uma falha.
"""

from __future__ import annotations

from typing import Any

import tornado.iostream
import tornado.web

from empiremuse.panel.handlers.servers import AuthenticatedHandler
from empiremuse.panel.services.radio_logo import (  # noqa: F401 - a lógica mora no serviço; os nomes seguem aqui
    DEFAULT_ICON,
    LOGO_PATH,
    logo_path,
    lookup_logo,
)
from empiremuse.rules.radio_stations import valid_uuid


LOGO_ROUTE = LOGO_PATH + r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.png"


class RadioLogoHandler(AuthenticatedHandler):
    def prepare(self) -> None:
        if self.current_session is None:  # imagem sem sessão: recusada, sem redirecionar para o login
            raise tornado.web.HTTPError(404)

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        """Erro sem a página HTML (ela gravaria o cookie do XSRF): só o status, sem corpo e sem cache."""
        self.clear()
        self.set_status(status_code)
        self.set_header("Cache-Control", "no-store")
        self.finish()

    async def get(self, uuid: str) -> None:  # type: ignore[override]
        if not valid_uuid(uuid):
            raise tornado.web.HTTPError(404)
        png, max_age = await self._lookup(uuid)
        self.set_header("Content-Type", "image/png")
        self.set_header("Cross-Origin-Resource-Policy", "same-origin")
        self.set_header("Cache-Control", f"private, max-age={max_age}")
        self.set_header("Content-Length", str(len(png)))
        try:
            await self.flush()
            self.write(png)
            self.finish()
        except tornado.iostream.StreamClosedError:
            return

    async def _lookup(self, uuid: str) -> tuple[bytes, int]:
        """``(png, max-age)``: o logo do diretório, ou o ícone padrão (falha, sem logo ou sem ficha)."""
        return await lookup_logo(self.application.settings.get("pool"), uuid)
