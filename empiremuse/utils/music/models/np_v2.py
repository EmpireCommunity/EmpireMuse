"""O passo do ``invoke_np`` para a skin em Components V2 (``muse_jukebox``): monta o payload V2 com o disco e decide os anexos.

- **Disco:** o ``DiscService`` devolve o GIF e o PNG parado de um rótulo (capa, ou "sem capa" em call privada). Os dois sobem
  juntos (``attachments=[]`` + 2 arquivos) só quando o disco da mensagem muda (faixa nova, mensagem nova); pausa e retomada
  só trocam a referência da Thumbnail (``.gif`` <-> ``.png``), e a barra que anda a cada ciclo vai sem arquivo nenhum.
- **Plano B:** vaga ocupada = a capa por URL na Thumbnail (ou o "sem capa" fixo) e o updater tenta de novo.
- **Rádio R:** o logo da estação (PNG do ``LogoService``) no lugar do disco, nunca o disco nem o avatar do bot.
- **Nunca cai em V1:** se a montagem levanta, a mensagem fica como está (o próximo ciclo tenta de novo).
- O que a mensagem mostra de anexo só vale depois que a edição saiu (``confirm``): uma edição que falhou não marca o disco como enviado.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import disnake

from empiremuse.rules.card import CardState
from empiremuse.rules.cover import COVER_WAIT_S
from empiremuse.rules.text import external_text
from empiremuse.utils.branding import asset_url
from empiremuse.utils.music import card_facts
from empiremuse.utils.music.skins.muse.muse_jukebox import NO_COVER_ASSET, Thumb
from empiremuse.utils.render import disc_only
from empiremuse.utils.render.disc_only import Disc, DiscImages, disc_service
from empiremuse.utils.safe_error import safe_error

logger = logging.getLogger(__name__)

ATTACHMENT_PREFIX = "attachment://"
RADIO_FALLBACK_ASSET = "global.png"
LOGO_NAME = "logo"
ALT_TITLE_MAX = 80
ANIMATED_STATES = frozenset({CardState.PLAYING, CardState.STREAM})

_SENT: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # player -> o nome do anexo que a mensagem tem


def sent_name(player: Any) -> Optional[str]:
    return _SENT.get(player)


def forget(player: Any) -> None:
    """A mensagem do player é outra (nova, ou a skin mudou): o anexo antigo não vale."""
    _SENT.pop(player, None)


def is_v2_payload(data: Any) -> bool:
    """O payload é Components V2 (um ``Container`` no topo)? Um payload de embed (skin V1) não é."""
    return any(isinstance(item, disnake.ui.Container) for item in (data or {}).get("components") or ())


def mark_v2(message: Any) -> None:
    """A mensagem acabou de virar V2 por edição: o objeto que o player guarda ainda tem as flags antigas, e o adaptador precisa
    saber (senão a próxima edição de embed, como o ocioso, levaria 400)."""
    flags = getattr(message, "flags", None)
    if flags is not None:
        flags.is_components_v2 = True


def confirm(player: Any, req: Any) -> None:
    """Depois da edição: o disco só vale como enviado se a edição saiu, e uma mensagem V1 editada com payload V2 passa a ser V2."""
    if req.published and getattr(req, "v2", False):
        for message in (getattr(req.interaction, "message", None), getattr(player, "message", None)):
            if message is not None:
                mark_v2(message)
    name = getattr(req, "disc", None)
    if name is None:
        return
    if req.published:
        _SENT[player] = name
    else:
        _SENT.pop(player, None)


def _file(data: bytes, filename: str, alt: str) -> disnake.File:
    return disnake.File(io.BytesIO(data), filename=filename, description=alt)


def disc_files(name: str, images: DiscImages, alt: str) -> list[disnake.File]:
    """O GIF e o PNG parado (um arquivo só quando o GIF caiu para o PNG)."""
    if images.gif == images.png:
        return [_file(images.png, f"{name}.png", alt)]
    return [_file(images.gif, f"{name}.gif", alt), _file(images.png, f"{name}.png", alt)]


def _disc_media(disc: Disc, spec: Any) -> str:
    """A referência do anexo: o GIF girando só tocando e com a animação ligada; senão o PNG parado."""
    images = disc.images
    animated = spec.motion and spec.state in ANIMATED_STATES and images is not None and images.gif != images.png
    return f"{ATTACHMENT_PREFIX}{disc.name}.{'gif' if animated else 'png'}"


def _alt(player: Any, spec: Any, *, plain: bool) -> str:
    if plain or spec.track is None:
        return player.tr("jbx.disc_alt_plain")
    return player.tr("jbx.disc_alt", titulo=external_text(spec.track.title, ALT_TITLE_MAX, markdown=False))


async def _radio_logo(
    spec: Any, read: Optional[Callable[[str], Awaitable[Optional[bytes]]]]
) -> tuple[Optional[bytes], bool]:
    """O PNG do logo da estação e se ainda pode chegar. Sem logo: ``(None, pending)`` e vai o ``global.png`` por URL."""
    key = spec.radio.logo_key if spec.radio is not None else None
    if key is None or read is None:
        return None, False
    fetch = asyncio.ensure_future(read(key))
    fetch.add_done_callback(lambda task: task.cancelled() or task.exception())
    try:
        return await asyncio.wait_for(asyncio.shield(fetch), COVER_WAIT_S), False
    except asyncio.TimeoutError:
        return None, True
    except Exception as error:  # noqa: BLE001 - o logo é conveniência: só o tipo vai ao log
        logger.warning("Logo da estação não obtido (%s)", safe_error(error))
        return None, False


class Picked:
    """O que o passo escolheu para a imagem: a ``Thumb``, os arquivos novos (ou vazio), o nome a gravar e se redesenha."""

    __slots__ = ("thumb", "files", "name", "again")

    def __init__(self, thumb: Thumb, files: list[disnake.File], name: Optional[str], again: bool) -> None:
        self.thumb, self.files, self.name, self.again = thumb, files, name, again


async def pick(player: Any, spec: Any, station_logo: Any, cover_hosts: object, shown: Optional[str]) -> Picked:
    """A Thumbnail do estado: o logo da estação (R), o disco (música) ou o plano B (vaga ocupada)."""
    if spec.radio is not None:
        fallback = Thumb(asset_url(RADIO_FALLBACK_ASSET), player.tr("jbx.disc_alt_plain"))
        if spec.hidden:
            return Picked(fallback, [], None, False)
        png, pending = await _radio_logo(spec, station_logo)
        if png is None:
            return Picked(fallback, [], None, pending)
        name = f"{LOGO_NAME}-{hashlib.sha256(png).hexdigest()[:8]}"
        alt = player.tr("jbx.station_logo_alt", nome=external_text(spec.radio.name, ALT_TITLE_MAX, markdown=False))
        files = [] if shown == name else [_file(png, f"{name}.png", alt)]
        return Picked(Thumb(f"{ATTACHMENT_PREFIX}{name}.png", alt), files, name, pending)
    plain = spec.hidden or spec.track is None
    disc = await disc_service(cover_hosts).disc(spec.cover, plain=plain)
    alt = _alt(player, spec, plain=plain or disc.plain)
    if disc.images is None:  # vaga ocupada: o plano B, a capa por URL (ou o "sem capa" fixo)
        media = spec.cover if (spec.cover and not plain) else asset_url(NO_COVER_ASSET)
        return Picked(Thumb(media, alt), [], None, True)
    files = [] if shown == disc.name else disc_files(disc.name, disc.images, alt)
    return Picked(Thumb(_disc_media(disc, spec), alt), files, disc.name, disc.pending)


async def render(
    self: Any,
    req: Any,
    skin: Any,
    *,
    redraw: bool,
    new_message: bool,
    key_of: Callable[[Any], Any],
    defer: Callable[[Any], Awaitable[None]],
    disc_motion: Callable[..., Awaitable[Any]],
    station_logo: Any,
) -> bool:
    """``True`` = nada a editar. Senão ``req.data`` fica pronto (payload V2, e os arquivos se o disco mudou)."""
    if new_message:
        forget(self)
    if req.interaction is not None:
        await defer(req.interaction)  # antes do primeiro I/O (leitura do banco, disco): o prazo da interação é de 3 s
    try:
        settings = card_facts.CardSettings(
            skin=skin.name,
            theme="black",
            locale=str(getattr(self.tr, "locale", "pt-BR")),
            disc_motion=await disc_motion(self, fresh=redraw),
        )
        spec = card_facts.build(self, settings)
        picked = await pick(self, spec, station_logo, self.bot.config["COVER_HOSTS"], sent_name(self))
        if card_facts.build(self, settings) != spec:
            # a faixa mudou enquanto o disco era feito: o texto novo com o disco velho nunca sai; o updater redesenha
            self.update = True
            return True
        data = skin.build(self, time.time(), picked.thumb)
    except Exception as error:  # noqa: BLE001 - o player nunca fica sem mensagem, e a C nunca cai em V1: não edita
        logger.warning("Jukebox V2 não montou (%s)", safe_error(error))
        self.update = True  # sem isto uma transmissão (sem ciclo periódico) ficaria sem mensagem até outro evento
        await defer(req.interaction)
        return True
    key = key_of(data)
    if picked.again:
        self.update = True  # capa a caminho, logo adiado ou vaga ocupada: o updater redesenha
    if key == self.last_data and not picked.files and not new_message:
        await defer(req.interaction)
        return True
    self.last_data = key
    req.disc = picked.name
    req.v2 = True
    if picked.files:
        data["files"] = picked.files
        data["attachments"] = []
    req.data = data
    return False


def disc_cached(name: str) -> Optional[DiscImages]:
    service = disc_only._SERVICE
    return service.cache.get(name) if service is not None else None


def ensure_files(player: Any, req: Any, data: dict) -> None:
    """Uma mensagem NOVA com a Thumbnail num anexo que só a mensagem antiga tinha: refaz os arquivos do cache do disco (o
    envio não herda anexo). Sem os arquivos no cache, o disco é pedido de novo no próximo ciclo."""
    name = getattr(req, "disc", None)
    if name is None or data.get("files"):
        return
    images = disc_cached(name)
    if images is None:
        player.last_data = {}
        player.update = True
        forget(player)
        return
    data["files"] = disc_files(name, images, "")


def refresh_files(data: dict) -> bool:
    """Remonta os ``File`` do payload V2 dos mesmos bytes (um envio anterior os deixa no fim do buffer)."""
    files = data.get("files")
    if not files:
        return False
    fresh = []
    for file in files:
        read = getattr(getattr(file, "fp", None), "getvalue", None)
        if not callable(read):
            return False
        fresh.append(disnake.File(io.BytesIO(read()), filename=file.filename, description=file.description))
    data["files"] = fresh
    return True
