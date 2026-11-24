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
from collections.abc import Mapping
from typing import Any, NamedTuple


class Surface(StrEnum):
    PLAYER = "player"
    RADIO = "radio"


class Kind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    NATIVE = "native"  # Components V2 do próprio Discord (``muse_jukebox``)


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


DEFAULT_JUKEBOX_SKIN = "muse_jukebox"  # a Jukebox em Components V2, nos dois modos do player
DEFAULT_RADIO_LOOK = "text"  # a Rádio Texto, nas rádios 24 h
LEGACY_SKIN_DEFAULT = "default"  # o padrão de antes do 2.3, gravado pelo /skin antigo ao salvar os dois modos
LEGACY_RADIO_SKINS = ("black", "white")  # o que o código anterior aceita no campo ``skin`` da rádio
LIGHT_THEME = "white"

# Q1 (decidida): o "default" gravado pelo /skin antigo NÃO conta como escolha. Quem só tem esse "default" gravado passa
# ao padrão novo, como quem nunca escolheu; qualquer outra skin gravada fica.
SAVED_DEFAULT_IS_A_CHOICE = False


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


def _classic_preview(skin_id: str, *, static: bool = False) -> str:
    """As prévias das Clássicas são as de antes, uma por modo (``skin_normal_*`` e ``skin_static_*``)."""
    return _preview("static" if static else "normal", skin_id)


def _classic(skin_id: str) -> SkinEntry:
    return SkinEntry(
        skin_id,
        Surface.PLAYER,
        Kind.TEXT,
        FAMILY_CLASSIC,
        None,
        "",
        "skin.classic",
        _classic_preview(skin_id),
        False,
    )


JUKEBOX_SKINS: tuple[SkinEntry, ...] = (
    _muse("muse_jukebox", Kind.NATIVE, None, "muse_jukebox"),
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
_PLAYER_BY_ID = {entry.id: entry for entry in JUKEBOX_SKINS + CLASSIC_SKINS}


def player_preview(skin_id: str, *, static: bool = False) -> str | None:
    """O asset da prévia de uma skin do player (a fonte única: o ``/skins`` lê daqui); ``None`` fora do registro."""
    entry = _PLAYER_BY_ID.get(skin_id)
    if entry is None:
        return None
    return _classic_preview(skin_id, static=static) if entry.family == FAMILY_CLASSIC else entry.preview


# ---------------------------------------------------------------------- rádio 24 h


# Apelidos de leitura (nada é reescrito no banco): as skins de imagem que saem valem como a Jukebox em V2 e, na rádio 24 h,
# o Retrato vale como o visor do mesmo tema. Um apelido conta como ESCOLHA (a marca ``*_chosen`` segue valendo).
SKIN_ALIASES: Mapping[str, str] = {
    f"muse_{family}_{theme}": "muse_jukebox"
    for family in ("expanded", "slim", "radio", "radio_portrait")
    for theme in ("black", "white")
}
RADIO_LOOK_ALIASES: Mapping[str, str] = {"portrait_black": "black", "portrait_white": "white"}


def resolve_skin_alias(value: object) -> object:
    """O id gravado de uma skin de imagem que saiu vale como ``muse_jukebox``; qualquer outro valor passa como está."""
    return SKIN_ALIASES.get(value, value) if isinstance(value, str) else value


def resolve_radio_alias(value: object) -> object:
    """``portrait_black``/``portrait_white`` gravados valem como ``black``/``white``; o resto passa como está."""
    return RADIO_LOOK_ALIASES.get(value, value) if isinstance(value, str) else value


def valid_radio_look(value: object) -> bool:
    return isinstance(value, str) and value in _RADIO_BY_ID


def radio_look(look: object, guild_radio_skin: object) -> str:
    """A skin efetiva de uma rádio: a própria, senão a de todas as rádios do servidor, senão ``DEFAULT_RADIO_LOOK``."""
    for candidate in (resolve_radio_alias(look), resolve_radio_alias(guild_radio_skin)):
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


SKIN_FIELDS = ("skin", "static_skin", "player_skin", "player_skin_static", "skin_static")


def chosen_key(field: str) -> str:
    """A marca gravada ao lado de um campo de skin pelo ``/skin``, pelo painel e pela sessão do 2.3 (o formato antigo não a tem)."""
    return f"{field}_chosen"


def is_chosen(doc: object, field: str) -> bool:
    """O valor do campo foi gravado como escolha pelo 2.3? Sem a marca (formato antigo), não."""
    return isinstance(doc, Mapping) and doc.get(chosen_key(field)) is True


def with_choice_marks(changes: Mapping[str, object]) -> dict[str, object]:
    """As alterações com a marca de escolha em cada campo de skin gravado: um ``"default"`` gravado assim é escolha."""
    return {**changes, **{chosen_key(field): True for field in changes if field in SKIN_FIELDS}}


def jukebox_choice(saved: object, chosen: bool = False) -> object:
    """O valor gravado da skin da Jukebox que conta como escolha; ``None`` = nunca escolhido (cai no padrão do registro).

    Q1 (decidida: não): sem ``SAVED_DEFAULT_IS_A_CHOICE`` o ``"default"`` gravado pelo ``/skin`` antigo é lido como ``None`` e
    cai no padrão novo. Com a marca de escolha (``chosen``, ver ``is_chosen``) o ``"default"`` é a Clássica escolhida e fica. Qualquer outro valor passa como está (o ``check_skin`` decide).
    """
    if saved == LEGACY_SKIN_DEFAULT and not chosen and not SAVED_DEFAULT_IS_A_CHOICE:
        return None
    return resolve_skin_alias(saved)


def jukebox_skin(saved: object, default: str, chosen: bool = False) -> str:
    """A skin que um leitor usa ou mostra para o valor gravado: a escolha (``jukebox_choice``, Q1), senão ``default``.

    Todo leitor da skin gravada da Jukebox passa por aqui ou por ``jukebox_choice`` (criação do player, ``/skin``, painel,
    sessão retomada); assim o Q1 muda só em ``SAVED_DEFAULT_IS_A_CHOICE``."""
    choice = jukebox_choice(saved, chosen)
    return choice if isinstance(choice, str) and choice else default


def saved_player_skins(server: Mapping[str, Any], global_data: Mapping[str, Any]) -> tuple[object, object]:
    """As skins gravadas que o player novo usa (antes do ``check_skin``), com o Q1 aplicado em CADA nível antes do recuo.

    No modo global, a skin global vale se for uma escolha; senão a do servidor. Assim, com o Q1 = não, um ``"default"``
    global gravado não esconde a skin escolhida no servidor. ``server`` tem ``skin`` e ``static_skin`` (e as marcas de escolha)."""
    skin = jukebox_choice(server.get("skin"), is_chosen(server, "skin"))
    static = jukebox_choice(server.get("static_skin"), is_chosen(server, "static_skin"))
    if global_data.get("global_skin"):
        skin = jukebox_choice(global_data.get("player_skin"), is_chosen(global_data, "player_skin")) or skin
        static = (
            jukebox_choice(global_data.get("player_skin_static"), is_chosen(global_data, "player_skin_static"))
            or static
        )
    return skin, static


def global_skin_changes(shown: tuple[str, str], picked: tuple[object, object]) -> dict[str, object]:
    """O que o ``/skin`` grava no modo global: só os campos que o usuário trocou (``shown`` = o que a tela mostrou).

    Salvar sem trocar mantém a ausência de escolha global; assim a skin herdada do servidor não vira escolha explícita."""
    fields = ("player_skin", "player_skin_static")
    return {field: new for field, old, new in zip(fields, shown, picked) if new != old}


def disc_motion_on(value: object) -> bool:
    """A animação do disco está ligada? ``None`` (nunca escolhido) ou qualquer valor que não seja ``False`` = ligada."""
    return value is not False
