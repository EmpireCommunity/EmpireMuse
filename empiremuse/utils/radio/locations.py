"""A árvore de local das rádios (item 8, R4): ⭐ e 6 regiões → país → estado → cidade, de um arquivo FIXO distribuído com o bot.

Contrato em ``.local/contrato-radio-r4.md`` (§1, §2 e §5). Os dados (``data/locations.json``) vêm do GeoNames (CC BY 4.0), do CLDR e do Radio Browser e
são gerados uma vez por ``tools/radio_geo/build.py``; nada é buscado em execução. A carga acontece UMA vez, na subida, com o esquema validado: se falhar,
o filtro some (``RadioHub.locations`` fica ``None``) e a busca por nome da R3 continua.

Quem usa (o Discord, em ``cogs/radio``, e a página Rádios, do Prism) vê só quatro coisas: ``children`` (as opções do nível, sempre até 24), ``node`` (o
nó de um id vindo do usuário, ou ``None``), ``path`` (do topo até o nó) e ``label``. Um id desconhecido, mal formado ou de outro formato é ``None``,
nunca erro.

Rótulos: país pelo Babel; as 6 regiões, os grupos de estados e o ⭐ pelo i18n (``radio.local.*``); estado e cidade com o nome do dado, sem traduzir.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.radio_geo import (
    AREAS,
    MAX_OPTIONS,
    StateIndex,
    area_id,
    country_id,
    guild_country,
    parse_node_id,
    valid_coordinates,
)

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).parent / "data" / "locations.json"
SCHEMA_VERSION = 1
LABEL_LIMIT = 100
MAX_FILE_BYTES = 8 * 1024 * 1024  # o arquivo tem cerca de meio MB: acima disto algo está errado


class LocationDataError(Exception):
    """O arquivo de dados não existe, não é JSON ou não passa no esquema; ``reason`` é um código estável (nunca texto do arquivo)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LocOption(NamedTuple):
    """Uma opção de select: ``id`` do nó, ``label`` e ``description`` (até 100 caracteres) e ``count`` de estações (``0`` = não sei, ainda se lista)."""

    id: str
    label: str
    description: str
    count: int


class LocNode(NamedTuple):
    """Um nó da árvore. ``final`` = não há nível abaixo (mostra a lista de estações). Os demais campos alimentam ``Directory.region``."""

    id: str
    kind: str  # region | country | state | city
    name: str  # região: o slug; país: a sigla; estado e cidade: o nome do dado
    parent: str | None
    final: bool
    cc: str | None = None
    lat: float | None = None
    lon: float | None = None
    admin1: tuple[str, ...] = ()  # subdivisões que o nó cobre (para filtrar o texto de estado das estações)
    terms: tuple[str, ...] = ()  # termos de ``state=`` para o Radio Browser
    exact: str | None = None  # sigla para ``stateExact=true``, se a geração achou uma
    city: str | None = None  # nome da cidade para ``name=``


Translate = Callable[[str], Translator]


def _default_translate(locale: str) -> Translator:
    from empiremuse.i18n import get_i18n

    return get_i18n().for_locale(locale)


@lru_cache(maxsize=64)
def _territories(locale: str) -> Mapping[str, str]:
    """Os nomes dos países do idioma (Babel). Idioma desconhecido cai no ``en-US``; sem Babel, vazio (o rótulo vira a sigla)."""
    try:
        from babel import Locale

        try:
            return Locale.parse(locale.replace("-", "_")).territories
        except Exception:  # noqa: BLE001 - UnknownLocaleError, ValueError: cai no inglês
            return Locale.parse("en_US").territories
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------- esquema


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise LocationDataError(reason)


def _validate(data: object) -> Mapping[str, Any]:
    """Confere o esquema inteiro (tipos, ids, tetos e referências) e devolve o documento; qualquer defeito levanta ``LocationDataError``."""
    _need(isinstance(data, dict), "document")
    doc: dict[str, Any] = data  # type: ignore[assignment]
    _need(doc.get("version") == SCHEMA_VERSION, "version")
    for key in ("areas", "countries", "states", "cities", "admin1", "index"):
        _need(isinstance(doc.get(key), dict), f"missing:{key}")
    areas, countries, states, cities = doc["areas"], doc["countries"], doc["states"], doc["cities"]
    _need(list(areas) == list(AREAS), "areas")
    seen_countries: set[str] = set()
    for slug, members in areas.items():
        _need(isinstance(members, list) and 0 < len(members) <= MAX_OPTIONS, f"area:{slug}")
        for cc in members:
            parsed = parse_node_id(country_id(cc)) if isinstance(cc, str) else None
            _need(parsed is not None and cc in countries and cc not in seen_countries, f"area_country:{cc}")
            seen_countries.add(cc)
    _need(seen_countries == set(countries), "countries")
    used_states: set[str] = set()
    for cc, entry in countries.items():
        _need(isinstance(entry, dict) and isinstance(entry.get("n"), int) and entry["n"] >= 0, f"country:{cc}")
        listed = entry.get("states")
        _need(isinstance(listed, list) and len(listed) <= MAX_OPTIONS, f"country_states:{cc}")
        for sid in listed:
            parsed = parse_node_id(sid)
            _need(
                parsed is not None
                and parsed.kind == "state"
                and parsed.cc == cc
                and sid in states
                and sid not in used_states,
                f"state_ref:{sid}",
            )
            used_states.add(sid)
    _need(used_states == set(states), "states")
    used_cities: set[str] = set()
    for sid, state in states.items():
        _need(isinstance(state, dict) and isinstance(state.get("name"), str) and state["name"], f"state:{sid}")
        _need(state.get("kind") in ("single", "region_rest", "neighbors"), f"state_kind:{sid}")
        _need(isinstance(state.get("n"), int) and state["n"] >= 0, f"state_n:{sid}")
        codes = state.get("a1")
        _need(isinstance(codes, list) and codes and all(isinstance(c, str) and c for c in codes), f"state_a1:{sid}")
        listed = state.get("cities")
        _need(isinstance(listed, list) and len(listed) <= MAX_OPTIONS, f"state_cities:{sid}")
        for xid in listed:
            parsed = parse_node_id(xid)
            _need(
                parsed is not None and parsed.kind == "city" and xid in cities and xid not in used_cities,
                f"city_ref:{xid}",
            )
            used_cities.add(xid)
            _need(cities[xid].get("a1") in codes, f"city_a1:{xid}")
    _need(used_cities == set(cities), "cities")
    for xid, city in cities.items():
        _need(isinstance(city, dict) and isinstance(city.get("name"), str) and city["name"], f"city:{xid}")
        _need(valid_coordinates(city.get("lat"), city.get("lon")), f"city_coordinates:{xid}")
    index = doc["index"]
    for key in ("aliases", "cities", "names"):
        _need(isinstance(index.get(key), dict), f"index:{key}")
    return doc


# ---------------------------------------------------------------------- a árvore


class LocationTree:
    def __init__(self, data: object, *, translate: Translate = _default_translate) -> None:
        self._doc = _validate(data)
        self._translate = translate
        self._areas: dict[str, list[str]] = self._doc["areas"]
        self._countries: dict[str, dict[str, Any]] = self._doc["countries"]
        self._states: dict[str, dict[str, Any]] = self._doc["states"]
        self._cities: dict[str, dict[str, Any]] = self._doc["cities"]
        self._admin1: dict[str, dict[str, dict[str, Any]]] = self._doc["admin1"]
        self._parents: dict[str, str] = {}
        for sid, state in self._states.items():
            self._parents[sid] = country_id(sid.split(":")[1])
            for xid in state["cities"]:
                self._parents[xid] = sid
        for slug, members in self._areas.items():
            for cc in members:
                self._parents[country_id(cc)] = area_id(slug)
        index = self._doc["index"]
        self.state_index = StateIndex(
            aliases={cc: dict(v) for cc, v in index["aliases"].items()},
            cities={cc: dict(v) for cc, v in index["cities"].items()},
            names=dict(index["names"]),
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH, *, translate: Translate = _default_translate) -> LocationTree:
        """Lê e valida o arquivo. ``LocationDataError`` se não existe, é grande demais, não é JSON ou não passa no esquema."""
        file = Path(path)
        try:
            if file.stat().st_size > MAX_FILE_BYTES:
                raise LocationDataError("size")
            data = json.loads(file.read_text(encoding="utf-8"))
        except LocationDataError:
            raise
        except (OSError, ValueError, RecursionError) as error:
            raise LocationDataError("read") from error
        return cls(data, translate=translate)

    # ------------------------------------------------------------------ nós

    @property
    def countries(self) -> frozenset[str]:
        return frozenset(self._countries)

    def node(self, node_id: object) -> LocNode | None:
        """O nó de um id (que veio do usuário) ou ``None`` se o formato ou a árvore não o conhecem."""
        parsed = parse_node_id(node_id)
        if parsed is None or not isinstance(node_id, str):
            return None
        parent = self._parents.get(node_id)
        if parsed.kind == "region":
            return LocNode(node_id, "region", parsed.code, None, False) if parsed.code in self._areas else None
        if parsed.kind == "country":
            entry = self._countries.get(parsed.code)
            if entry is None:
                return None
            return LocNode(node_id, "country", parsed.code, parent, not entry["states"], cc=parsed.code)
        if parsed.kind == "state":
            return self._state_node(node_id, parent)
        city = self._cities.get(node_id)
        if city is None or parent is None:
            return None
        return self._city_node(node_id, city, parent, self._states[parent])

    def _terms(self, cc: str, code: str) -> tuple[tuple[str, ...], str | None]:
        entry = self._admin1.get(cc, {}).get(code, {})
        terms = entry.get("terms")
        sigla = entry.get("sigla")
        return (
            tuple(t for t in terms if isinstance(t, str)) if isinstance(terms, list) else (),
            sigla if isinstance(sigla, str) and sigla else None,
        )

    def _city_node(self, node_id: str, city: Mapping[str, Any], parent: str, state: Mapping[str, Any]) -> LocNode:
        cc = parent.split(":")[1]
        terms, exact = self._terms(cc, city["a1"])
        return LocNode(
            node_id,
            "city",
            city["name"],
            parent,
            True,
            cc=cc,
            lat=float(city["lat"]),
            lon=float(city["lon"]),
            admin1=tuple(state["a1"]),
            terms=terms,
            exact=exact,
            city=city["name"],
        )

    def _state_node(self, node_id: str, parent: str | None) -> LocNode | None:
        state = self._states.get(node_id)
        if state is None or parent is None:
            return None
        cc = parent.split(":")[1]
        cities = state["cities"]
        first = self._cities[cities[0]] if cities else None
        terms, exact = self._terms(cc, state["a1"][0])
        return LocNode(
            node_id,
            "state",
            state["name"],
            parent,
            len(cities) <= 1,  # estado com uma cidade só (ou nenhuma) pula o nível de cidade
            cc=cc,
            lat=float(first["lat"]) if first else None,
            lon=float(first["lon"]) if first else None,
            admin1=tuple(state["a1"]),
            terms=terms,
            exact=exact,
            city=first["name"] if first else None,
        )

    def path(self, node_id: object) -> list[LocNode]:
        """Do topo até o nó (o próprio nó por último); vazio se o id não existe."""
        found = self.node(node_id)
        chain: list[LocNode] = []
        while found is not None:
            chain.append(found)
            found = self.node(found.parent) if found.parent else None
        return chain[::-1]

    # ------------------------------------------------------------------ rótulos e opções

    def _country_name(self, cc: str, locale: str) -> str:
        return fit(_territories(locale).get(cc) or cc, LABEL_LIMIT)

    def label(self, node: LocNode, locale: str) -> str:
        """O rótulo do nó no idioma (para as migalhas do caminho)."""
        tr = self._translate(locale)
        if node.kind == "region":
            return tr(f"radio.local.area.{node.name}")
        if node.kind == "country":
            return self._country_name(node.name, locale)
        state = self._states.get(node.id)
        return self._state_label(state, tr) if state is not None else fit(node.name, LABEL_LIMIT)

    def _state_label(self, state: Mapping[str, Any], tr: Translator) -> str:
        kind = state["kind"]
        if kind == "region_rest":
            return fit(tr("radio.local.region_rest", region=tr(f"radio.local.region.{state['group']}")), LABEL_LIMIT)
        if kind == "neighbors":
            return fit(tr("radio.local.neighbors", name=state["name"]), LABEL_LIMIT)
        return fit(state["name"], LABEL_LIMIT)

    def _count_text(self, tr: Translator, count: int) -> str:
        return fit(tr("radio.local.stations", count=count), LABEL_LIMIT) if count > 0 else ""

    def children(self, node_id: str | None, locale: str, *, guild_locale: str | None = None) -> list[LocOption]:
        """As opções do nível abaixo de ``node_id`` (até 24; ``None`` = o topo, onde o ⭐ vem primeiro). Nó final, id inválido ou desconhecido: ``[]``."""
        tr = self._translate(locale)
        if node_id is None:
            return self._top(tr, locale, guild_locale)
        node = self.node(node_id)
        if node is None or node.final:
            return []
        if node.kind == "region":
            return [self._country_option(cc, tr, locale) for cc in self._areas[node.name]]
        if node.kind == "country":
            return [self._state_option(sid, self._states[sid], tr) for sid in self._countries[node.name]["states"]][
                :MAX_OPTIONS
            ]
        state = self._states[node.id]
        return [LocOption(xid, fit(self._cities[xid]["name"], LABEL_LIMIT), "", 0) for xid in state["cities"]][
            :MAX_OPTIONS
        ]

    def _top(self, tr: Translator, locale: str, guild_locale: str | None) -> list[LocOption]:
        options: list[LocOption] = []
        home = guild_country(guild_locale, self._countries) if guild_locale else None
        if home is not None:
            label = fit(tr("radio.local.guild_country", country=self._country_name(home, locale)), LABEL_LIMIT)
            options.append(LocOption(country_id(home), label, "", self._countries[home]["n"]))
        for slug in AREAS:
            members = self._areas[slug]
            count = sum(self._countries[cc]["n"] for cc in members)
            options.append(
                LocOption(
                    area_id(slug),
                    fit(tr(f"radio.local.area.{slug}"), LABEL_LIMIT),
                    fit(tr("radio.local.n_countries", count=len(members)), LABEL_LIMIT),
                    count,
                )
            )
        return options

    def _country_option(self, cc: str, tr: Translator, locale: str) -> LocOption:
        count = self._countries[cc]["n"]
        return LocOption(country_id(cc), self._country_name(cc, locale), self._count_text(tr, count), count)

    def _state_option(self, sid: str, state: Mapping[str, Any], tr: Translator) -> LocOption:
        members = state.get("members")
        description = (
            fit(", ".join(members), LABEL_LIMIT)
            if isinstance(members, list) and members
            else self._count_text(tr, state["n"])
        )
        return LocOption(sid, self._state_label(state, tr), description, state["n"])
