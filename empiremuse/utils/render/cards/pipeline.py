"""Do ``CardSpec`` projetado ao anexo da mensagem do player: capa, formato, vaga de desenho, quadros e codificação.

- **Uma instância por processo** (``card_pipeline()``): a capa (``CoverService``), o cache de quadros e a vaga são
  compartilhados pelos players dos 5 bots.
- **Fora da vaga:** a capa (até ``COVER_WAIT_S``), a escolha do formato (``rules.card.choose_format``) e o cache de
  quadros (sem trava: só no laço de eventos). **Dentro da vaga (thread):** o desenho, os ladrilhos e a codificação.
- **Teto do GIF antes de alocar:** ``layout.gif_fits`` (cartão e caixa do disco medidos) decide antes de qualquer
  ladrilho; GIF acima de ``GIF_MAX_BYTES`` ou que falha ao montar vira PNG.
- **Um desenho por player:** cada pedido leva o número do pedido mais novo do player; quem entra na vaga já velho sai
  sem desenhar, e quem sai dela já velho descarta o desenho sem gravar o estado (``Rendered.stale``).
- **Vaga ocupada (``RenderBusy``):** nada é desenhado; quem chamou mantém o anexo que está na mensagem (só se a call
  não mudou de privacidade; senão ``key`` = ``None`` e a mensagem sai sem imagem) e o próximo ciclo redesenha
  (``upgrade_due``).
- **GIF mantido sob pressão:** com o mesmo ``CardSpec`` e a mesma capa, um GIF que já está na mensagem não é trocado
  por PNG.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import weakref
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable, NamedTuple, Optional

from PIL import Image

from empiremuse.rules.card import CardFormat, CardSpec, RenderedKey, choose_format, cover_digest
from empiremuse.rules.cover import COVER_WAIT_S
from empiremuse.utils.render.budget import GIF_FRAMES, GIF_MAX_BYTES
from empiremuse.utils.render.cards import expanded, slim, visor_jukebox
from empiremuse.utils.render.cards.layout import ARC_STOPS, EXPANDED, SLIM, CardArt, Translate, gif_fits, theme_for
from empiremuse.utils.render.fonts import FontBook
from empiremuse.utils.render.frames import DiscFrames, FramesCache, frames_key
from empiremuse.utils.render.gif import build_palette, encode_gif, encode_png, quantize_tiles
from empiremuse.utils.render.slot import RenderBusy, RenderSlot, shared_slot
from empiremuse.utils.render.state import RENDER_STATES, RenderStates
from empiremuse.utils.safe_error import safe_error

logger = logging.getLogger(__name__)

RENDERERS: dict[str, ModuleType] = {"expanded": expanded, "slim": slim, "visor_jukebox": visor_jukebox}
CARD_SIZES: dict[str, tuple[int, int]] = {
    "expanded": EXPANDED.size,
    "slim": SLIM.size,
    "visor_jukebox": visor_jukebox.VISOR_SIZE,
}
BOT_LOGO = Path(__file__).resolve().parents[4] / "assets" / "brand" / "logo-256.png"
FILE_STEM = "card"

StationLogo = Callable[[str], Awaitable[Optional[bytes]]]


class Rendered(NamedTuple):
    """``data`` = os bytes do anexo novo, ou ``None`` = manter o que está na mensagem (chave igual, vaga ocupada ou
    pedido velho). ``key`` = a chave do que a mensagem mostra depois disto (``None`` = nenhum anexo ainda)."""

    key: Optional[RenderedKey]
    data: Optional[bytes] = None
    busy: bool = False
    stale: bool = False

    @property
    def filename(self) -> str:
        return f"{FILE_STEM}.{self.key.fmt.value}" if self.key is not None else ""


class _Ticket:
    """O número do pedido mais novo de um player. Lido da thread da vaga: um ``int`` trocado inteiro."""

    __slots__ = ("latest", "__weakref__")

    def __init__(self) -> None:
        self.latest = 0


class _Drawn(NamedTuple):
    data: Optional[bytes]  # ``None`` = o pedido ficou velho antes de desenhar
    fmt: CardFormat
    frames: Optional[DiscFrames]  # quadros novos para o cache (render frio)


def _read_bot_logo() -> Optional[bytes]:
    try:
        return BOT_LOGO.read_bytes()
    except OSError:
        logger.warning("Logo do bot não encontrado para os cartões")
        return None


def palette_colors(theme: str) -> tuple[tuple[int, int, int], ...]:
    """As cores fixas da paleta do GIF: os tokens do tema e o degradê do arco (o resto vem do disco)."""
    return tuple(theme_for(theme)) + tuple(color for _stop, color in ARC_STOPS)


def _consume(task: "asyncio.Future[Any]") -> None:
    if not task.cancelled():
        task.exception()


def keeps_gif(shown: Optional[RenderedKey], want: RenderedKey) -> bool:
    """Um GIF já na mensagem com o mesmo ``CardSpec`` e a mesma capa fica, mesmo que agora o formato escolhido seja PNG
    (pressão na vaga ou intervalo do GIF frio): trocar só pioraria a imagem e custaria um desenho e um envio."""
    return shown is not None and shown.fmt is CardFormat.GIF and shown._replace(fmt=want.fmt) == want


def file_box(box: tuple[int, int, int, int], scale: int) -> tuple[int, int, int, int]:
    factor = max(1, int(scale))
    x0, y0, x1, y1 = box
    return (x0 * factor, y0 * factor, x1 * factor, y1 * factor)


class CardPipeline:
    def __init__(
        self,
        covers: Any,
        *,
        frames: Optional[FramesCache] = None,
        slot: Optional[RenderSlot] = None,
        book: Optional[FontBook] = None,
        states: RenderStates = RENDER_STATES,
        clock: Callable[[], float] = time.monotonic,
        bot_logo: Optional[bytes] = None,
    ) -> None:
        self.covers = covers
        self.frames = frames if frames is not None else FramesCache()
        self._slot = slot
        self._book = book
        self.states = states
        self._clock = clock
        self._bot_logo = bot_logo if bot_logo is not None else _read_bot_logo()
        self._tickets: weakref.WeakKeyDictionary[Any, _Ticket] = weakref.WeakKeyDictionary()

    @property
    def slot(self) -> RenderSlot:
        return self._slot if self._slot is not None else shared_slot()

    @property
    def book(self) -> FontBook:
        if self._book is None:
            self._book = FontBook()
        return self._book

    def _ticket(self, player: Any) -> tuple[_Ticket, int]:
        ticket = self._tickets.get(player)
        if ticket is None:
            ticket = self._tickets[player] = _Ticket()
        ticket.latest += 1
        return ticket, ticket.latest

    async def _art(self, spec: CardSpec, station_logo: Optional[StationLogo]) -> tuple[CardArt, bool]:
        """Os pixels de fora do desenho e se a capa ainda pode chegar (``pending``)."""
        cover = await self.covers.pixels(spec.cover) if spec.cover is not None else None
        pending = bool(cover is not None and cover.pending)
        logo = self._bot_logo
        if spec.radio is not None and spec.radio.logo_key and station_logo is not None:
            fetch = asyncio.ensure_future(station_logo(spec.radio.logo_key))
            fetch.add_done_callback(_consume)  # a busca que passa do prazo e falha depois não deixa erro sem dono
            try:
                logo = await asyncio.wait_for(asyncio.shield(fetch), COVER_WAIT_S) or logo
            except asyncio.TimeoutError:
                pending = True  # o logo da estação chega depois (o serviço segue buscando): o próximo ciclo redesenha
            except Exception as error:  # noqa: BLE001 - o logo é conveniência: só o tipo vai ao log
                logger.warning("Logo da estação não obtido para o cartão (%s)", safe_error(error))
        pixels = cover.pixels if cover is not None else None
        return CardArt(cover=pixels, logo=logo), pending

    def _art_digest(self, art: CardArt) -> Optional[str]:
        """A identidade da imagem do rótulo na chave do anexo: a capa, ou o logo da estação; ``None`` = o logo do bot. Um
        logo de estação que chega depois do prazo muda a chave, e o próximo ciclo o desenha."""
        if art.cover is not None:
            return cover_digest(art.cover)
        if art.logo is not None and art.logo is not self._bot_logo:
            return cover_digest(art.logo)
        return None

    async def render(
        self,
        player: Any,
        card: str,
        spec: CardSpec,
        tr: Translate,
        *,
        station_logo: Optional[StationLogo] = None,
    ) -> Rendered:
        """O anexo de ``spec`` (já projetado pela skin) para a mensagem de ``player``. Nunca levanta ``RenderBusy``."""
        renderer = RENDERERS[card]
        ticket, number = self._ticket(player)
        art, pending = await self._art(spec, station_logo)
        state = self.states.get(player)
        box = renderer.disc_box(spec)
        label = cover_digest(art.cover or art.logo) or ""
        fkey = frames_key(spec, label, GIF_FRAMES) if label else None
        cached = self.frames.get(fkey) if fkey is not None else None
        now = self._clock()
        choice = choose_format(
            motion=spec.motion,
            animatable=label != "" and gif_fits(CARD_SIZES[card], box, spec.scale),
            pressure=self.slot.pressure(),
            cold=cached is None,
            now=now,
            last_cold_gif_at=state.last_cold_gif_at,
        )
        upgrade_due = choice.upgrade_due or pending
        want = RenderedKey(spec, self._art_digest(art), choice.fmt)
        shown = state.last
        if shown is not None and (want == shown or keeps_gif(shown, want)):
            self.states.replace(player, upgrade_due=upgrade_due if want == shown else pending)
            return Rendered(shown)
        job = (renderer, spec, art, tr, choice.fmt, box, cached, ticket, number)
        try:
            drawn: _Drawn = await self.slot.run(self._draw, *job, label=spec.skin)
        except RenderBusy:
            self.states.replace(player, upgrade_due=True)
            same_privacy = shown is not None and shown.spec.hidden == spec.hidden
            return Rendered(shown if same_privacy else None, busy=True)
        if drawn.frames is not None and fkey is not None:
            self.frames.put(fkey, drawn.frames)
        if drawn.data is None or ticket.latest != number:
            return Rendered(self.states.get(player).last, stale=True)  # um pedido mais novo deste player manda
        key = want._replace(fmt=drawn.fmt)
        cold_gif = drawn.fmt is CardFormat.GIF and cached is None
        self.states.replace(
            player,
            last=key,
            upgrade_due=upgrade_due,
            last_cold_gif_at=now if cold_gif else state.last_cold_gif_at,
        )
        return Rendered(key, drawn.data)

    def _draw(
        self,
        renderer: ModuleType,
        spec: CardSpec,
        art: CardArt,
        tr: Translate,
        fmt: CardFormat,
        box: Optional[tuple[int, int, int, int]],
        cached: Optional[DiscFrames],
        ticket: _Ticket,
        number: int,
    ) -> _Drawn:
        """Na thread da vaga. O pedido velho sai antes de desenhar; o GIF que passa do teto vira PNG."""
        if ticket.latest != number:
            return _Drawn(None, fmt, None)
        image: Image.Image = renderer.draw(spec, art, tr, self.book)
        if fmt is not CardFormat.GIF or box is None:
            return _Drawn(encode_png(image), CardFormat.PNG, None)
        try:
            frames = cached or self._frames(renderer, spec, art, box)
            data = encode_gif(image, frames)
        except Exception as error:  # noqa: BLE001 - a imagem já desenhada ainda dá o PNG
            logger.warning("GIF do cartão não saiu, vai o PNG (skin %s: %s)", spec.skin, safe_error(error))
            return _Drawn(encode_png(image), CardFormat.PNG, None)
        fresh = None if cached else frames
        if len(data) > GIF_MAX_BYTES:
            return _Drawn(encode_png(image), CardFormat.PNG, fresh)
        return _Drawn(data, CardFormat.GIF, fresh)

    @staticmethod
    def _frames(renderer: ModuleType, spec: CardSpec, art: CardArt, box: tuple[int, int, int, int]) -> DiscFrames:
        tiles = renderer.disc_tiles(spec, art, GIF_FRAMES)
        palette = build_palette(tiles, palette_colors(spec.theme))
        return quantize_tiles(tiles, palette, file_box(box, spec.scale))


def attachment_file(rendered: Rendered, description: str) -> Any:
    """O ``disnake.File`` do anexo novo (``None`` quando não há bytes novos)."""
    import disnake

    if rendered.data is None or rendered.key is None:
        return None
    return disnake.File(io.BytesIO(rendered.data), filename=rendered.filename, description=description)


_PIPELINE: Optional[CardPipeline] = None


def card_pipeline(cover_hosts: object) -> CardPipeline:
    """A instância do processo, criada no primeiro uso com o ``COVER_HOSTS`` já validado pelo ``config_loader``."""
    global _PIPELINE
    if _PIPELINE is None:
        from empiremuse.utils.music.covers import CoverService

        _PIPELINE = CardPipeline(CoverService.from_config(cover_hosts))
    return _PIPELINE


async def close_card_pipeline() -> None:
    """No encerramento do pool: cancela as buscas de capa em voo (cada worker é morto e coletado)."""
    global _PIPELINE
    pipe, _PIPELINE = _PIPELINE, None
    if pipe is not None:
        await pipe.covers.aclose()
