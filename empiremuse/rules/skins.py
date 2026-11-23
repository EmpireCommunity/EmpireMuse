"""Registro das skins (2.3) e as regras puras de qual skin vale. Sem I/O, sem import de módulo de skin.

- **Registro:** ``REGISTRY`` lista toda skin de fábrica, na ordem do ``/skins`` (primeiro as do EmpireMuse, depois as
  Clássicas). As da Jukebox (família ``muse``) valem nos dois modos do player; as Clássicas continuam carregadas pelo
  diretório, como antes, e aqui só aparecem na ordem. O carregador (``utils/client.py::load_skins``) só importa ids daqui
  ou do diretório, nunca um valor vindo do banco.
- **Rádio 24 h:** ``RADIO_LOOKS`` é a única lista que valida o campo ``look`` da rádio e o ``radio_skin`` do servidor.
  ``radio_look`` escolhe a primeira válida entre a da rádio, a de todas as rádios e o padrão; valor desconhecido nunca
  levanta. ``legacy_radio_skin`` dá o valor do campo antigo ``skin`` (só ``black``/``white``), que o código anterior
  continua lendo depois de um rollback.
- **Jukebox:** ``jukebox_choice`` decide se um valor gravado conta como escolha (Q1); ``None`` cai no padrão
  ``DEFAULT_JUKEBOX_SKIN`` pelo ``check_skin`` de sempre.
- **Animação do disco:** ``disc_motion_on`` (``None`` = nunca escolhido = ligada).
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple


class Surface(StrEnum):
    PLAYER = "player"
    RADIO = "radio"


class Kind(StrEnum):
    TEXT = "text"
    IMAGE = "image"


FAMILY_MUSE = "muse"
FAMILY_CLASSIC = "classic"
FAMILY_RADIO = "radio"


class SkinEntry(NamedTuple):
    id: str
    surface: Surface
    kind: Kind
    family: str  # FAMILY_MUSE | FAMILY_CLASSIC | FAMILY_RADIO
    theme: str | None  # "black" | "white" | None
    name_key: str  # chave do catálogo; vazia = o próprio id (Clássicas)
    desc_key: str
    preview: str  # caminho do asset da prévia
    concept: bool  # True = a prévia é a imagem conceitual (rodapé "Imagem conceitual")


DEFAULT_JUKEBOX_SKIN = "muse_expanded_black"  # D2, nos dois modos
DEFAULT_RADIO_LOOK = "portrait_black"  # D2 das rádios
LEGACY_SKIN_DEFAULT = "default"  # o padrão de antes do 2.3, gravado pelo /skin antigo ao salvar os dois modos
LEGACY_RADIO_SKINS = ("black", "white")  # o que o código anterior aceita no campo ``skin`` da rádio
LIGHT_THEME = "white"

# Q1 (pendente com o dono): o "default" gravado pelo /skin antigo conta como escolha? Recomendação: sim (fica).
# Para virar "não" (só quem nunca escolheu E quem tem "default" gravado passam à Expandida), trocar para False.
SAVED_DEFAULT_IS_A_CHOICE = True


def _preview(family: str, skin_id: str) -> str:
    return f"skins/skin_{family}_{skin_id}_preview.png"


def _muse(skin_id: str, kind: Kind, theme: str | None, desc: str) -> SkinEntry:
    return SkinEntry(
        skin_id,
        Surface.PLAYER,
        kind,
        FAMILY_MUSE,
        theme,
        f"skin.name.{skin_id}",
        f"skin.desc.{desc}",
        _preview(FAMILY_MUSE, skin_id),
        True,
    )


def _radio(skin_id: str, kind: Kind, theme: str | None, desc: str, *, concept: bool = True) -> SkinEntry:
    return SkinEntry(
        skin_id,
        Surface.RADIO,
        kind,
        FAMILY_RADIO,
        theme,
        f"skin.name.radio.{skin_id}",
        f"skin.desc.radio.{desc}",
        _preview(FAMILY_RADIO, skin_id),
        concept,
    )


def _classic(skin_id: str) -> SkinEntry:
    return SkinEntry(
        skin_id,
        Surface.PLAYER,
        Kind.TEXT,
        FAMILY_CLASSIC,
        None,
        "",
        "skin.classic",
        _preview(FAMILY_CLASSIC, skin_id),
        False,
    )


JUKEBOX_SKINS: tuple[SkinEntry, ...] = (
    _muse("muse_expanded_black", Kind.IMAGE, "black", "muse_expanded"),
    _muse("muse_expanded_white", Kind.IMAGE, "white", "muse_expanded"),
    _muse("muse_slim_black", Kind.IMAGE, "black", "muse_slim"),
    _muse("muse_slim_white", Kind.IMAGE, "white", "muse_slim"),
    _muse("muse_radio_black", Kind.IMAGE, "black", "muse_radio"),
    _muse("muse_radio_white", Kind.IMAGE, "white", "muse_radio"),
    _muse("muse_radio_portrait_black", Kind.IMAGE, "black", "muse_radio_portrait"),
    _muse("muse_radio_portrait_white", Kind.IMAGE, "white", "muse_radio_portrait"),
    _muse("muse_text", Kind.TEXT, None, "muse_text"),
)

# As Clássicas (D3), na ordem do /skins; continuam carregadas do diretório (``IGNORE_SKINS`` vale para elas).
CLASSIC_NORMAL = (
    "default",
    "default_progressbar",
    "classic",
    "embed_link",
    "lite",
    "mini",
    "miniplayer",
    "micro_controller",
    "micro_nc",
    "minimalist",
)
CLASSIC_STATIC = ("default", "default_progressbar", "classic", "embed_link", "mini")
CLASSIC_SKINS: tuple[SkinEntry, ...] = tuple(_classic(name) for name in dict.fromkeys(CLASSIC_NORMAL + CLASSIC_STATIC))

RADIO_SKINS: tuple[SkinEntry, ...] = (
    _radio("portrait_black", Kind.IMAGE, "black", "portrait"),
    _radio("portrait_white", Kind.IMAGE, "white", "portrait"),
    _radio("text", Kind.TEXT, None, "text"),
    _radio("black", Kind.IMAGE, "black", "visor", concept=False),
    _radio("white", Kind.IMAGE, "white", "visor", concept=False),
)

REGISTRY: tuple[SkinEntry, ...] = JUKEBOX_SKINS + CLASSIC_SKINS + RADIO_SKINS
JUKEBOX_IDS: tuple[str, ...] = tuple(entry.id for entry in JUKEBOX_SKINS)
RADIO_LOOKS: tuple[str, ...] = tuple(entry.id for entry in RADIO_SKINS)
_RADIO_BY_ID = {entry.id: entry for entry in RADIO_SKINS}


# ---------------------------------------------------------------------- rádio 24 h


def valid_radio_look(value: object) -> bool:
    return isinstance(value, str) and value in _RADIO_BY_ID


def radio_look(look: object, guild_radio_skin: object) -> str:
    """A skin efetiva de uma rádio: a própria, senão a de todas as rádios do servidor, senão ``DEFAULT_RADIO_LOOK``."""
    for candidate in (look, guild_radio_skin):
        if valid_radio_look(candidate):
            return str(candidate)
    return DEFAULT_RADIO_LOOK


def legacy_radio_skin(look: object) -> str:
    """O campo antigo ``skin`` da rádio: ``white`` quando a skin é clara, senão ``black`` (sempre um valor que o rc anterior aceita)."""
    entry = _RADIO_BY_ID.get(look) if isinstance(look, str) else None
    return LIGHT_THEME if entry is not None and entry.theme == LIGHT_THEME else LEGACY_RADIO_SKINS[0]


def look_from_legacy(skin: object) -> str | None:
    """Documento de rádio sem ``look`` (de antes do 2.3): ``white`` continua branco; ``black`` era o padrão de criação, então
    vira ``None`` e segue a de todas as rádios (D2)."""
    return LIGHT_THEME if skin == LIGHT_THEME else None


# ---------------------------------------------------------------------- Jukebox e disco


def jukebox_choice(saved: object) -> object:
    """O valor gravado da skin da Jukebox que conta como escolha; ``None`` = nunca escolhido (cai no padrão do registro).

    Q1: com ``SAVED_DEFAULT_IS_A_CHOICE`` o ``"default"`` gravado fica (quem salvou a Clássica padrão a mantém); sem ele, o
    ``"default"`` gravado é lido como ``None``. Qualquer outro valor passa como está (o ``check_skin`` decide).
    """
    if saved == LEGACY_SKIN_DEFAULT and not SAVED_DEFAULT_IS_A_CHOICE:
        return None
    return saved


def disc_motion_on(value: object) -> bool:
    """A animação do disco está ligada? ``None`` (nunca escolhido) ou qualquer valor que não seja ``False`` = ligada."""
    return value is not False
