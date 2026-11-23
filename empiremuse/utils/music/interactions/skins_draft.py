"""O rascunho do ``/skins`` (2.3, contrato S6 §5): estado imutável, leitura dos valores dos selects e o plano de gravação.

Puro: sem Discord, sem banco. A view (``skins_view.py``) guarda um ``Draft`` por invocação e troca por um novo a cada escolha;
nada é gravado até o Salvar, e aí ``plan_save`` diz o que mudou e ``plan_is_valid`` revalida tudo contra as listas relidas.

- **Valores dos selects:** id do registro (ou Clássica carregada), ``custom:<nome>`` (personalizada do servidor), ``follow``
  ("Usar a de todas as rádios"), alvo das rádios ``all`` ou ``radio:<bot_id>``. Valor fora das listas é recusado.
- **Só o que mudou é gravado** (como o ``/skin`` de antes e ``rules/skins.global_skin_changes``): salvar sem trocar não
  transforma o padrão em escolha explícita (D2), nem a skin herdada do servidor em escolha global.
- **Rádios:** o par das rádios só entra no plano com a permissão de quem configura rádio, conferida de novo no Salvar.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

from empiremuse.rules.limits import CUSTOM_SKIN_PREFIX
from empiremuse.rules.skins import JUKEBOX_SKINS, global_skin_changes, valid_radio_look

MODE_NORMAL = "normal"
MODE_STATIC = "static"
FOCUS_RADIO = "radio"
TARGET_ALL = "all"
RADIO_PREFIX = "radio:"
CUSTOM_VALUE = "custom:"
FOLLOW = "follow"

SERVER_FIELDS = ("skin", "static_skin")
_FIELD_MODE = {
    "skin": MODE_NORMAL,
    "player_skin": MODE_NORMAL,
    "static_skin": MODE_STATIC,
    "player_skin_static": MODE_STATIC,
}
_FLAGS = ("global_skin", "disc_motion")


class RadioRow(NamedTuple):
    """Uma rádio do servidor da interação, como o ``/skins`` a mostra. ``label`` já vem limpo (``external_text``)."""

    bot_id: int
    label: str
    look: str | None
    rev: int


class Choices(NamedTuple):
    """O que pode ser escolhido agora (relido no Salvar)."""

    normal: tuple[str, ...]  # ids de fábrica do modo normal, na ordem do /skins
    static: tuple[str, ...]  # ids de fábrica do canal da Jukebox
    custom_normal: tuple[str, ...]  # nomes das personalizadas do servidor
    custom_static: tuple[str, ...]
    radios: tuple[RadioRow, ...]  # vazio sem rádio no servidor ou sem a permissão das rádios

    def ids(self, mode: str) -> tuple[str, ...]:
        return self.static if mode == MODE_STATIC else self.normal

    def customs(self, mode: str) -> tuple[str, ...]:
        return self.custom_static if mode == MODE_STATIC else self.custom_normal


@dataclasses.dataclass(frozen=True)
class Draft:
    """O que a tela mostra. ``server`` e ``glob`` = (modo normal, canal da Jukebox) de cada escopo; os selects editam o par do
    modo atual (``global_mode``). ``radio_own`` = (bot_id, skin própria ou ``None``) por rádio. ``focus`` = qual select manda
    na prévia."""

    global_mode: bool
    server: tuple[str, str]
    glob: tuple[str, str]
    disc_motion: bool
    radio_all: str
    radio_own: tuple[tuple[int, str | None], ...] = ()
    target: str = TARGET_ALL
    focus: str = MODE_NORMAL

    def pair(self) -> tuple[str, str]:
        return self.glob if self.global_mode else self.server

    def player(self, mode: str) -> str:
        return self.pair()[1 if mode == MODE_STATIC else 0]

    def with_player(self, mode: str, value: str) -> Draft:
        normal, static = self.pair()
        pair = (normal, value) if mode == MODE_STATIC else (value, static)
        if self.global_mode:
            return dataclasses.replace(self, glob=pair, focus=mode)
        return dataclasses.replace(self, server=pair, focus=mode)

    def own(self, bot_id: int) -> str | None:
        return dict(self.radio_own).get(bot_id)

    def target_bot(self) -> int | None:
        return target_bot(self.target)

    def shown_radio_skin(self) -> str | None:
        """A skin do alvo das rádios como o select a marca: a de todas, ou a própria (``None`` = segue a de todas)."""
        bot_id = self.target_bot()
        return self.radio_all if bot_id is None else self.own(bot_id)

    def effective_radio_skin(self) -> str:
        """A skin que o alvo das rádios usa de fato (a própria, senão a de todas): é a que a prévia mostra."""
        return self.shown_radio_skin() or self.radio_all

    def with_radio_skin(self, look: str | None) -> Draft:
        bot_id = self.target_bot()
        if bot_id is None:
            return dataclasses.replace(self, radio_all=look or self.radio_all, focus=FOCUS_RADIO)
        own = tuple((b, look if b == bot_id else current) for b, current in self.radio_own)
        return dataclasses.replace(self, radio_own=own, focus=FOCUS_RADIO)


def ordered_ids(allowed: Iterable[str], classics: Sequence[str]) -> tuple[str, ...]:
    """As skins de fábrica na ordem do ``/skins``: primeiro as do EmpireMuse (ordem do registro), depois as Clássicas (ordem do
    registro) e por fim qualquer outra carregada do diretório. Só entra o que está em ``allowed`` (carregado e não ignorado)."""
    loaded = tuple(dict.fromkeys(allowed))
    present = set(loaded)
    muse = [entry.id for entry in JUKEBOX_SKINS if entry.id in present]
    classic = [name for name in classics if name in present and name not in muse]
    known = set(muse) | set(classic)
    return tuple(muse + classic + [name for name in loaded if name not in known])


def custom_value(name: str) -> str:
    return CUSTOM_VALUE + name


def stored_custom_name(stored: object) -> str | None:
    """O nome da personalizada num valor gravado (``> custom_skin: <nome>``), ou ``None``."""
    if isinstance(stored, str) and stored.startswith(CUSTOM_SKIN_PREFIX):
        return stored[len(CUSTOM_SKIN_PREFIX) :]
    return None


def player_value(raw: object, ids: Sequence[str], customs: Sequence[str]) -> str | None:
    """Valor de um select do player -> valor gravado (id ou ``> custom_skin: <nome>``); ``None`` se não está nas listas."""
    if not isinstance(raw, str):
        return None
    if raw.startswith(CUSTOM_VALUE):
        name = raw[len(CUSTOM_VALUE) :]
        return CUSTOM_SKIN_PREFIX + name if name in customs else None
    return raw if raw in ids else None


def target_bot(target: str) -> int | None:
    if not target.startswith(RADIO_PREFIX):
        return None
    raw = target[len(RADIO_PREFIX) :]
    return int(raw) if raw.isascii() and raw.isdigit() else None


def target_value(raw: object, radios: Sequence[RadioRow]) -> str | None:
    """O alvo das rádios: ``all`` ou ``radio:<bot_id>`` de uma rádio listada (do servidor da interação); senão ``None``."""
    if raw == TARGET_ALL:
        return TARGET_ALL
    if not isinstance(raw, str):
        return None
    bot_id = target_bot(raw)
    if bot_id is None or not any(row.bot_id == bot_id for row in radios):
        return None
    return f"{RADIO_PREFIX}{bot_id}"


class Invalid:
    """Valor recusado pelo select de skin das rádios (``None`` já significa "usar a de todas as rádios")."""


INVALID = Invalid()


def radio_skin_value(raw: object, target: str) -> str | None | Invalid:
    """Valor do select de skin das rádios: um id de ``RADIO_LOOKS``; ``follow`` só numa rádio específica (vira ``None``)."""
    if raw == FOLLOW and target_bot(target) is not None:
        return None
    return raw if isinstance(raw, str) and valid_radio_look(raw) else INVALID


class RadioLook(NamedTuple):
    bot_id: int
    look: str | None
    rev: int  # o ``rev`` que a tela leu: a gravação é compare-and-set nele


class SavePlan(NamedTuple):
    global_fields: Mapping[str, object]  # documento global ``guilds`` (do pool)
    server_fields: Mapping[str, object]  # ``pool.servers`` (settings)
    radio_looks: tuple[RadioLook, ...]

    @property
    def empty(self) -> bool:
        return not (self.global_fields or self.server_fields or self.radio_looks)


def plan_save(initial: Draft, draft: Draft, radios: Sequence[RadioRow], *, radio_allowed: bool) -> SavePlan:
    """O que o Salvar grava: só o que mudou em relação ao que a tela abriu mostrando.

    - ``global_skin`` quando o modo mudou (desligar também grava: ``False`` persiste);
    - no modo global, as skins globais trocadas; senão, as do servidor trocadas;
    - ``disc_motion`` quando mudou;
    - com ``radio_allowed``: ``radio_skin`` quando mudou e a skin própria de cada rádio trocada. Sem a permissão, nada das
      rádios entra, mesmo que o rascunho traga (valor forjado)."""
    global_fields: dict[str, object] = {}
    server_fields: dict[str, object] = {}
    if draft.global_mode != initial.global_mode:
        global_fields["global_skin"] = draft.global_mode
    if draft.global_mode:
        global_fields.update(global_skin_changes(initial.glob, draft.glob))
    else:
        server_fields = {f: new for f, old, new in zip(SERVER_FIELDS, initial.server, draft.server) if new != old}
    if draft.disc_motion != initial.disc_motion:
        global_fields["disc_motion"] = draft.disc_motion
    looks: tuple[RadioLook, ...] = ()
    if radio_allowed:
        if draft.radio_all != initial.radio_all:
            global_fields["radio_skin"] = draft.radio_all
        before = dict(initial.radio_own)
        revs = {row.bot_id: row.rev for row in radios}
        looks = tuple(
            RadioLook(bot_id, look, revs[bot_id])
            for bot_id, look in draft.radio_own
            if bot_id in before and bot_id in revs and before[bot_id] != look
        )
    return SavePlan(global_fields, server_fields, looks)


def _player_ok(value: object, mode: str, choices: Choices) -> bool:
    name = stored_custom_name(value)
    if name is not None:
        return name in choices.customs(mode)
    return isinstance(value, str) and value in choices.ids(mode)


def _field_ok(name: str, value: object, choices: Choices) -> bool:
    if name in _FIELD_MODE:
        return _player_ok(value, _FIELD_MODE[name], choices)
    if name in _FLAGS:
        return isinstance(value, bool)
    return name == "radio_skin" and valid_radio_look(value)


def plan_is_valid(plan: SavePlan, choices: Choices) -> bool:
    """Revalida o plano contra as listas relidas no Salvar (registro, personalizadas, rádios do servidor). Qualquer valor fora
    delas recusa o plano inteiro, sem escrita."""
    fields = {**plan.global_fields, **plan.server_fields}
    if not all(_field_ok(name, value, choices) for name, value in fields.items()):
        return False
    listed = {row.bot_id for row in choices.radios}
    return all(
        item.bot_id in listed and (item.look is None or valid_radio_look(item.look)) for item in plan.radio_looks
    )
