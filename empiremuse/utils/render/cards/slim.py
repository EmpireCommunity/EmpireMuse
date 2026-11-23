"""Slim (``muse_slim_black`` / ``muse_slim_white``, 480×640 em 1×): a Expandida da rodada 1, um pouco mais achatada.

As mesmas informações, identidade e tokens da Expandida, pelo mesmo pintor (``expanded.draw_card``) com o ``layout.SLIM``:
título numa linha, disco de 196 px, sem gabinete, sem linha do tempo e sem linha de informação. O tempo vai no embed.
"""

from __future__ import annotations

from PIL import Image

from empiremuse.rules.card import CardSpec
from empiremuse.utils.render.cards import disc, expanded
from empiremuse.utils.render.cards.layout import SLIM, CardArt, Translate
from empiremuse.utils.render.fonts import FontBook

SKINS = frozenset({"muse_slim_black", "muse_slim_white"})


def project(spec: CardSpec) -> CardSpec:
    """Sem a call (não há linha de informação) e sem a posição da pausa (não há linha do tempo); fila nas 4 linhas."""
    return expanded.project_card(spec, SLIM, voice=False, paused=False)


def disc_box(spec: CardSpec) -> tuple[int, int, int, int] | None:
    """A caixa do disco em pixels do arquivo (a Slim é desenhada em 1×)."""
    return SLIM.disc


def draw(spec: CardSpec, art: CardArt, tr: Translate, book: FontBook) -> Image.Image:
    return expanded.draw_card(project(spec), art, tr, book, SLIM)


def disc_tiles(spec: CardSpec, art: CardArt, frames: int) -> list[Image.Image]:
    return disc.card_tiles(project(spec), art, SLIM, frames)
