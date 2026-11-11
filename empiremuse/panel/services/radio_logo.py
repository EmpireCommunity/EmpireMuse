"""O logo de uma estação de rádio como PNG já re-codificado (camada de serviço, sem HTTP).

Compartilhado pelo painel genérico (``GET /painel/radios/logo/<uuid>.png``) e pela API (``GET /radios/{r}/logo``). Quem
chama já fez a autorização; aqui só se busca no diretório (``pool.radio.directory.logo_png``, Turbine) o PNG que ele guarda
em memória, ou se cai no ícone padrão do painel. O cliente nunca vê a URL do logo nem o motivo de uma falha.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from empiremuse.rules.radio_stations import valid_uuid

logger = logging.getLogger(__name__)

LOGO_PATH = "/painel/radios/logo"
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


async def lookup_logo(pool: Any, uuid: Optional[str]) -> tuple[bytes, int]:
    """``(png, max-age)``: o logo do diretório, ou o ícone padrão (uuid inválido, falha, sem logo ou sem ficha)."""
    radio = getattr(pool, "radio", None)
    fetch = getattr(getattr(radio, "directory", None), "logo_png", None)
    transient = False
    if fetch is not None and isinstance(uuid, str) and valid_uuid(uuid):
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
