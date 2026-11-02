"""``GET /painel/radios/logo/<uuid>.png``: o logo de uma estação, servido pela própria origem do painel (item 8, R4).

O navegador nunca sai da nossa origem (a CSP ``img-src`` não muda): a página monta o ``src`` a partir do uuid
validado, e esta rota devolve o PNG já re-codificado que o diretório (``pool.radio.directory.logo_png``, Turbine)
guarda em memória para as estações que a página acabou de mostrar (busca, cascata, principal e reserva por
``by_uuid``), ou o ícone padrão do painel quando não há logo. Só com sessão; a chave é só o uuid canônico da rota
(qualquer parâmetro é ignorado); o cliente nunca vê a URL do logo nem o motivo de uma falha.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import tornado.iostream
import tornado.web

from empiremuse.panel.handlers.servers import AuthenticatedHandler
from empiremuse.rules.radio_stations import valid_uuid

logger = logging.getLogger(__name__)

LOGO_PATH = "/painel/radios/logo"
LOGO_ROUTE = LOGO_PATH + r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.png"
DEFAULT_ICON = (
    Path(__file__).resolve().parent.parent / "static" / "icons" / "mnauliady" / "media-player" / "sound-wave@2x.png"
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_AGE_LOGO = 86400
MAX_AGE_DEFAULT = 300
MAX_AGE_TRANSIENT = 60  # sem ficha no diretório: o ícone padrão sai na hora e o navegador volta logo
_default: Optional[bytes] = None


def logo_path(uuid: Any) -> Optional[str]:
    """O ``src`` da página: só a partir de um uuid canônico (nunca a URL do favicon)."""
    return f"{LOGO_PATH}/{uuid}.png" if isinstance(uuid, str) and valid_uuid(uuid) else None


def _read_default() -> bytes:
    data = DEFAULT_ICON.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise OSError("ícone padrão não é PNG")
    return data


async def default_icon() -> bytes:
    global _default
    if _default is None:
        _default = await asyncio.to_thread(_read_default)
    return _default


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
        radio = getattr(self.application.settings.get("pool"), "radio", None)
        fetch = getattr(getattr(radio, "directory", None), "logo_png", None)
        transient = False
        if fetch is not None:
            try:
                result = await fetch(uuid)
            except Exception as exc:  # a rota nunca falha por causa do logo; só o tipo vai ao log
                logger.warning("Logo da estação: falha no diretório (%s)", type(exc).__name__)
                result = None
            png = getattr(result, "png", None)
            transient = bool(getattr(result, "transient", False))
            if png is not None and png.startswith(PNG_SIGNATURE):
                return bytes(png), MAX_AGE_LOGO
        return await default_icon(), MAX_AGE_TRANSIENT if transient else MAX_AGE_DEFAULT
