"""Gerador ÚNICO e determinístico da árvore de local das rádios (item 8, R4, contrato §2). Roda uma vez, à mão, e o resultado é commitado.

    uv run python tools/radio_geo/build.py --cache /tmp/radio_geo_cache

Escreve ``empiremuse/utils/radio/data/locations.json`` (a árvore: ⭐ + 6 regiões, 91 países, estado e cidade, cada nível com no máximo 24 opções) e
``locations.meta.json`` (URL, data e sha256 de cada entrada). Não há regeneração agendada, job de CI nem teste que o rode: o script fica no repositório
só como registro de procedência. Os arquivos baixados ficam em ``--cache`` (com a mesma disposição de pastas ``geo/``, ``cldr/``, ``iso/`` e ``dl/``);
o que já está lá não é baixado de novo, então a mesma entrada dá sempre a mesma saída.

Fontes: GeoNames ``cities5000`` e ``admin1CodesASCII`` (CC BY 4.0), CLDR 46 ``territoryContainment`` (Unicode License v3), iso-codes ``iso_3166-2``
(LGPL-2.1+, só na geração) e Radio Browser (domínio público; 2 pedidos agregados e o dump diário, com User-Agent próprio, nunca um laço por país).
"""

# ruff: noqa: T201 - script de linha de comando: imprime o relatório

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import io
import json
import math
import re
import sys
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from empiremuse.rules.radio_geo import (  # noqa: E402 - depois do sys.path
    AREA_OVERRIDES,
    AREA_SUBREGIONS,
    AREAS,
    CITY_SPACING_KM,
    FINAL_COUNTRY_STATIONS,
    MAX_OPTIONS,
    StateIndex,
    distance_km,
    norm,
    parse_node_id,
    resolve_state,
)

HERE = Path(__file__).resolve().parent
OUT = ROOT / "empiremuse" / "utils" / "radio" / "data"
USER_AGENT = "EmpireMuse-radio-geo/1.0 (+https://github.com/EmpireCommunity/EmpireMuse)"
RB_API = "https://de1.api.radio-browser.info"
RB_BACKUPS = "https://backups.radio-browser.info/"
GEONAMES = "https://download.geonames.org/export/dump/"
CLDR = "https://raw.githubusercontent.com/unicode-org/cldr-json/46.0.0/cldr-json/cldr-core/supplemental/territoryContainment.json"
ISO_CODES = "https://raw.githubusercontent.com/Debian/iso-codes/v4.17.0/data/iso_3166-2.json"

MIN_STATIONS = 100  # entra o país com tantas estações...
MIN_STATIONS_BIG = 25  # ...ou com população grande e ao menos tantas
BIG_POPULATION = 20_000_000
BLOCK_MIN_STATIONS, BLOCK_MIN_POPULATION = 100, 15_000_000
# Blocos de países pequenos e coerentes (língua ou cultura); só entram os que somam estações E habitantes (decisão do dono).
BLOCKS = {
    "america_central_hispanica": "GT SV CR HN PA NI",
    "andes_prata": "BO PY",
    "ex_iugoslavia": "MK ME",
    "hk_sg_mo": "HK SG MO",
    "leste_ex_sovietico": "MD BY",
    "caribe_anglofono": "JM TT BB",
    "caribe_hispanico": "PR CU",
    "magrebe": "TN",
    "levante_caucaso": "CY AZ IQ",
}
SKIP_FEATURES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}  # bairros, locais históricos e afins

_ADMIN1_CODE = re.compile(r"[A-Za-z0-9]{1,16}")

City = tuple[int, str, float, float, str, int]  # população, nome, lat, lon, admin1, geonameid


# ---------------------------------------------------------------------- entradas


class Sources:
    """Baixa (uma vez) e lê as entradas; guarda URL, data e sha256 para o ``meta``."""

    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.meta: list[dict[str, str]] = []

    def _get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - URLs fixas acima
            return response.read()

    def file(self, relative: str, url: str, license_: str, *, unzip: str | None = None) -> Path:
        target = self.cache / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            data = self._get(url)
            if unzip:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    data = archive.read(unzip)
            target.write_bytes(data)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if not any(m["file"] == relative for m in self.meta):
            self.meta.append({"file": relative, "url": url, "license": license_, "sha256": digest})
        return target

    def json(self, relative: str, url: str, license_: str) -> Any:
        return json.loads(self.file(relative, url, license_).read_text(encoding="utf-8"))

    def stations_dump(self) -> Path:
        target = self.cache / "dl" / "stations.json.gz"
        if not target.exists():
            listing = self._get(RB_BACKUPS).decode("utf-8", "replace")
            names = sorted(set(re.findall(r"radiobrowser_stations_\d{8}_\d{6}\.json\.gz", listing)))
            if not names:
                raise SystemExit("o dump diário do Radio Browser não foi encontrado")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._get(RB_BACKUPS + names[-1]))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.meta.append(
            {"file": "dl/stations.json.gz", "url": RB_BACKUPS, "license": "public domain", "sha256": digest}
        )
        return target


def country_info(sources: Sources) -> tuple[dict[str, str], dict[str, int]]:
    """``(nome em inglês, população)`` por país, do ``countryInfo.txt`` do GeoNames."""
    path = sources.file("geo/countryInfo.txt", GEONAMES + "countryInfo.txt", "CC BY 4.0")
    names: dict[str, str] = {}
    population: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        names[fields[0]] = fields[4]
        population[fields[0]] = int(fields[7] or 0)
    return names, population


# ---------------------------------------------------------------------- escopo e regiões


def pick_scope(stations: dict[str, int], population: dict[str, int], report: list[str]) -> set[str]:
    base = {
        cc
        for cc, n in stations.items()
        if n >= MIN_STATIONS or (population.get(cc, 0) >= BIG_POPULATION and n >= MIN_STATIONS_BIG)
    }
    kept: set[str] = set()
    for name, members in BLOCKS.items():
        outside = [cc for cc in members.split() if cc not in base and stations.get(cc, 0) >= MIN_STATIONS_BIG]
        total = sum(stations[cc] for cc in outside)
        people = sum(population.get(cc, 0) for cc in outside)
        keep = total >= BLOCK_MIN_STATIONS and people >= BLOCK_MIN_POPULATION
        report.append(
            f"bloco {name}: {outside} estações={total} habitantes={people / 1e6:.1f}M -> {'mantido' if keep else 'fora'}"
        )
        if keep:
            kept.update(outside)
    return base | kept


def pick_areas(scope: set[str], territory: dict) -> dict[str, list[str]]:
    sub_of: dict[str, str] = {}
    for continent in territory["001"]["_contains"]:
        for sub in territory[continent]["_contains"]:
            for cc in territory.get(sub, {}).get("_contains", []):
                sub_of[cc] = sub
    where: dict[str, str] = {}
    for cc in sorted(scope):
        if cc in AREA_OVERRIDES:
            where[cc] = AREA_OVERRIDES[cc]
            continue
        sub = sub_of.get(cc)
        slug = next((a for a in AREAS if sub in AREA_SUBREGIONS[a]), None)
        if slug is None:
            raise SystemExit(f"país sem região: {cc} ({sub})")
        where[cc] = slug
    return {slug: [cc for cc in sorted(scope) if where[cc] == slug] for slug in AREAS}


# ---------------------------------------------------------------------- GeoNames


class Geo:
    def __init__(self, sources: Sources, scope: set[str], manual: dict) -> None:
        self.admin1: dict[str, dict[str, tuple[str, str]]] = collections.defaultdict(
            dict
        )  # cc -> código -> (nome, ascii)
        path = sources.file("geo/admin1CodesASCII.txt", GEONAMES + "admin1CodesASCII.txt", "CC BY 4.0")
        for line in path.read_text(encoding="utf-8").splitlines():
            code, name, ascii_name, _ = line.split("\t")
            cc, a1 = code.split(".", 1)
            if cc in scope and _ADMIN1_CODE.fullmatch(a1):
                self.admin1[cc][a1] = (name, ascii_name)
        self.cities: dict[str, list[City]] = collections.defaultdict(list)
        path = sources.file("geo/cities5000.txt", GEONAMES + "cities5000.zip", "CC BY 4.0", unzip="cities5000.txt")
        for line in path.read_text(encoding="utf-8").splitlines():
            f = line.split("\t")
            if f[8] in scope and f[7] not in SKIP_FEATURES and f[10] in self.admin1.get(f[8], {}):
                self.cities[f[8]].append((int(f[14] or 0), f[1], float(f[4]), float(f[5]), f[10], int(f[0])))
        for lst in self.cities.values():
            lst.sort(key=lambda c: (-c[0], c[1], c[5]))
        self.manual = manual


def build_index(
    geo: Geo, iso_entries: list[dict], rb_names: dict[str, str], aliases_manual: dict, only: dict | None
) -> tuple[StateIndex, dict[str, dict[str, tuple[str, str]]]]:
    """O ``StateIndex`` (apelidos, cidades únicas, nomes) e o mapa ISO (cc -> admin1 -> (nome ISO, sigla)) usado nos rótulos.

    ``only`` (cc -> geonameids) restringe o índice de cidades às que vão para o arquivo; ``None`` usa todas (só para CONTAR estações por estado).
    """
    aliases: dict[str, dict[str, str]] = {cc: {} for cc in geo.admin1}
    iso_link: dict[str, dict[str, tuple[str, str]]] = {cc: {} for cc in geo.admin1}
    for cc, table in geo.admin1.items():
        for code, (name, ascii_name) in table.items():
            for alias in (name, ascii_name):
                aliases[cc].setdefault(norm(alias), code)
    manual_iso = {cc: spec.get("admin1_to_iso", {}) for cc, spec in geo.manual.items()}
    for entry in iso_entries:
        if "parent" in entry:
            continue
        cc, sigla = entry["code"].split("-", 1)
        table = geo.admin1.get(cc)
        if not table:
            continue
        code = next((a for a, iso in manual_iso.get(cc, {}).items() if iso == sigla), None)
        code = code or aliases[cc].get(norm(entry["name"])) or (sigla if sigla in table else None)
        if code is None or code not in table:
            continue
        iso_link[cc].setdefault(code, (entry["name"], sigla))
        aliases[cc].setdefault(norm(entry["name"]), code)
        aliases[cc].setdefault(norm(sigla), code)
    for cc, extra in aliases_manual.items():
        for text, code in extra.items():
            if cc in aliases and code in geo.admin1[cc]:
                aliases[cc][norm(text)] = code
    cities: dict[str, dict[str, str]] = {}
    for cc, lst in geo.cities.items():
        pool = lst if only is None else [c for c in lst if c[5] in only.get(cc, set())]
        seen: dict[str, set[str]] = collections.defaultdict(set)
        for _, name, _, _, a1, _ in pool:
            seen[norm(name)].add(a1)
        cities[cc] = {n: next(iter(a)) for n, a in seen.items() if n and len(a) == 1}
    names = {cc: norm(n) for cc, n in rb_names.items() if cc in geo.admin1}
    return StateIndex(aliases=aliases, cities=cities, names=names), iso_link


# ---------------------------------------------------------------------- estados e cidades


def choose_cities(pool: list[City]) -> list[City]:
    """As maiores por população, sem repetir nome e com ao menos ``CITY_SPACING_KM`` entre si."""
    chosen: list[City] = []
    names: set[str] = set()
    for city in pool:
        if city[1] in names or any(distance_km(city[2], city[3], o[2], o[3]) < CITY_SPACING_KM for o in chosen):
            continue
        names.add(city[1])
        chosen.append(city)
        if len(chosen) == MAX_OPTIONS:
            break
    return chosen


def state_level(cc: str, geo: Geo, weights: dict[str, int], iso: dict, spec: dict | None) -> list[dict]:
    """Os estados de um país (até 24): os menores se juntam à tabela oficial (``spec``: BR e US) ou ao vizinho mais próximo (centroide ponderado)."""
    codes = sorted(a for a in geo.admin1[cc] if any(c[4] == a for c in geo.cities[cc]))
    centroid: dict[str, tuple[float, float]] = {}
    for a in codes:
        pts = [(c[0] + 1, c[2], c[3]) for c in geo.cities[cc] if c[4] == a]
        total = sum(p for p, _, _ in pts)
        centroid[a] = (sum(p * la for p, la, _ in pts) / total, sum(p * lo for p, _, lo in pts) / total)
    group_of: dict[str, str] = {}
    if spec:
        to_iso = spec.get("admin1_to_iso") or {a: a for a in geo.admin1[cc]}
        for slug, members in spec["groups"].items():
            for sigla in members:
                code = next((a for a, i in to_iso.items() if i == sigla), None)
                if code is not None:
                    group_of[code] = slug
    options: dict[str, list[str]] = {a: [a] for a in codes}
    while len(options) > MAX_OPTIONS:
        singles = [k for k, m in options.items() if len(m) == 1 and not k.startswith("@")] or list(options)
        small = min(singles, key=lambda k: (sum(weights.get(x, 0) for x in options[k]), k))
        slug = group_of.get(options[small][0]) if spec else None
        if slug:
            key = "@" + slug
        else:
            here = centroid[options[small][0]]
            target = min(
                (k for k in options if k != small),
                key=lambda k: (
                    (centroid[options[k][0]][0] - here[0]) ** 2 + (centroid[options[k][0]][1] - here[1]) ** 2,
                    k,
                ),
            )
            key = target if target.startswith("@") else "@" + target
            if not target.startswith("@"):
                options[key] = options.pop(target)
        options.setdefault(key, []).extend(options.pop(small))

    def label_of(a: str) -> str:
        return iso.get(cc, {}).get(a, (geo.admin1[cc][a][0], ""))[0]

    result = []
    for key, members in options.items():
        population = {a: sum(c[0] for c in geo.cities[cc] if c[4] == a) for a in members}
        lead = max(sorted(members), key=lambda a: population[a])
        pool = sorted((c for c in geo.cities[cc] if c[4] in members), key=lambda c: (-c[0], c[1], c[5]))
        record: dict[str, Any] = {
            "key": key,
            "a1": sorted(members),
            "n": sum(weights.get(x, 0) for x in members),
            "cities": choose_cities(pool),
        }
        if len(members) == 1 and not key.startswith("@"):
            record.update(kind="single", name=label_of(lead))
        elif spec and key[1:] in spec["groups"]:
            record.update(kind="region_rest", name=key[1:], group=f"{cc.lower()}.{key[1:]}")
        else:
            record.update(kind="neighbors", name=label_of(lead), members=sorted(label_of(a) for a in members))
        result.append(record)
    result.sort(key=lambda r: (-r["n"], r["name"], r["a1"]))
    return result


def nearest_admin1(grid: dict, cc: str, lat: float, lon: float) -> str | None:
    best: tuple[float, str] | None = None
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for cla, clo, a1 in grid.get((cc, int(lat // 1) + dy, int(lon // 1) + dx), ()):
                d = (cla - lat) ** 2 + ((clo - lon) * math.cos(math.radians(lat))) ** 2
                if best is None or (d, a1) < best:
                    best = (d, a1)
    return best[1] if best else None


# ---------------------------------------------------------------------- principal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--cache", type=Path, required=True, help="pasta dos arquivos baixados (reaproveitados se já existirem)"
    )
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--report", action="store_true", help="lista os textos de estado sem casar (para o state_aliases.json)"
    )
    args = parser.parse_args()
    sources = Sources(args.cache)
    manual_regions = json.loads((HERE / "regions.json").read_text(encoding="utf-8"))
    aliases_manual = json.loads((HERE / "state_aliases.json").read_text(encoding="utf-8"))

    countries_path = sources.file("dl/countries.json", f"{RB_API}/json/countries?hidebroken=true", "public domain")
    states_path = sources.file("dl/states.json", f"{RB_API}/json/states?hidebroken=true&limit=100000", "public domain")
    countries = [
        c
        for c in json.loads(countries_path.read_text(encoding="utf-8"))
        if c["iso_3166_1"].isupper() and c["iso_3166_1"] not in ("XX", "UN")
    ]
    stations = {c["iso_3166_1"]: int(c["stationcount"]) for c in countries}
    rb_names = {c["iso_3166_1"]: c["name"] for c in countries}
    territory = sources.json("cldr/tc.json", CLDR, "Unicode License v3")["supplemental"]["territoryContainment"]
    iso_entries = sources.json("iso/iso_3166-2.json", ISO_CODES, "LGPL-2.1+")["3166-2"]
    dump = sources.stations_dump()
    _, population = country_info(sources)

    report: list[str] = []
    scope = pick_scope(stations, population, report)
    areas = pick_areas(scope, territory)
    geo = Geo(sources, scope, manual_regions)

    # o índice completo (todas as cidades) só CONTA estações por estado; o distribuído leva só as cidades escolhidas
    full_index, iso = build_index(geo, iso_entries, rb_names, aliases_manual, None)
    name_to_cc = {name: cc for cc, name in rb_names.items()}
    per_state: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    unmatched: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    state_texts: dict[str, set[str]] = collections.defaultdict(set)
    for row in json.loads(states_path.read_text(encoding="utf-8")):
        cc = name_to_cc.get(row["country"])
        if cc not in geo.admin1:
            continue
        state_texts[cc].add(norm(row["name"]))
        a1 = resolve_state(full_index, cc, row["name"])
        if a1 is None:
            unmatched[cc][row["name"].strip()[:60]] += int(row["stationcount"])
        else:
            per_state[cc][a1] += int(row["stationcount"])
    grid: dict[tuple[str, int, int], list[tuple[float, float, str]]] = collections.defaultdict(list)
    for cc, lst in geo.cities.items():
        for _, _, lat, lon, a1, _ in lst:
            grid[(cc, int(lat // 1), int(lon // 1))].append((lat, lon, a1))
    geo_count: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    with gzip.open(dump, "rt", encoding="utf-8") as handle:
        for item in json.load(handle):
            cc = (item.get("iso_3166_1") or item.get("countrycode") or "").upper()
            lat, lon = item.get("geo_lat"), item.get("geo_long")
            if cc in geo.admin1 and isinstance(lat, int | float) and isinstance(lon, int | float):
                if (a1 := nearest_admin1(grid, cc, lat, lon)) is not None:
                    geo_count[cc][a1] += 1

    doc: dict[str, Any] = {
        "version": 1,
        "areas": areas,
        "countries": {},
        "states": {},
        "cities": {},
        "admin1": {},
        "index": {},
    }
    included: dict[str, set[int]] = collections.defaultdict(set)
    for slug in AREAS:
        areas[slug].sort(key=lambda cc: (-stations[cc], cc))
    for cc in sorted(scope):
        entry: dict[str, Any] = {"n": stations[cc], "states": []}
        doc["countries"][cc] = entry
        if stations[cc] < FINAL_COUNTRY_STATIONS or cc not in geo.admin1:
            continue
        weights = {a: per_state[cc][a] + geo_count[cc][a] for a in geo.admin1[cc]}
        for record in state_level(cc, geo, weights, iso, manual_regions.get(cc)):
            sid = f"s:{cc}:{record['key']}" if record["key"].startswith("@") else f"s:{cc}:{record['a1'][0]}"
            if parse_node_id(sid) is None:
                raise SystemExit(f"id de estado inválido: {sid}")
            ids = []
            for _, name, lat, lon, a1, gid in record["cities"]:
                doc["cities"][f"x:{gid}"] = {"name": name, "lat": round(lat, 5), "lon": round(lon, 5), "a1": a1}
                ids.append(f"x:{gid}")
                included[cc].add(gid)
            state = {k: record[k] for k in ("kind", "name", "a1", "n", "group", "members") if k in record}
            state["cities"] = ids
            doc["states"][sid] = state
            entry["states"].append(sid)
            for code in record["a1"]:
                label, sigla = iso.get(cc, {}).get(code, (geo.admin1[cc][code][0], ""))
                found = sigla if sigla and norm(sigla) in state_texts[cc] else ""
                doc["admin1"].setdefault(cc, {})[code] = {
                    "name": label,
                    "terms": [label],
                    **({"sigla": found} if found else {}),
                }
    index, _ = build_index(geo, iso_entries, rb_names, aliases_manual, included)
    withstates = {cc for cc, entry in doc["countries"].items() if entry["states"]}
    doc["index"] = {
        "aliases": {cc: dict(sorted(v.items())) for cc, v in index.aliases.items() if cc in withstates},
        "cities": {cc: dict(sorted(v.items())) for cc, v in index.cities.items() if cc in withstates},
        "names": {cc: v for cc, v in sorted(index.names.items()) if cc in withstates},
    }
    args.out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    (args.out / "locations.json").write_text(text, encoding="utf-8")
    meta = {
        "generated": date.today().isoformat(),
        "generator": "tools/radio_geo/build.py",
        "sources": sorted(sources.meta, key=lambda m: m["file"]),
        "scope": sorted(scope),
    }
    (args.out / "locations.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print("\n".join(report))
    print(f"países: {len(scope)} · estados: {len(doc['states'])} · cidades: {len(doc['cities'])}")
    if args.report:
        for cc in sorted(unmatched, key=lambda c: -sum(unmatched[c].values()))[:20]:
            print(cc, unmatched[cc].most_common(8))


if __name__ == "__main__":
    main()
