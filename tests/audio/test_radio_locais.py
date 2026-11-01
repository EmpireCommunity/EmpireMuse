"""A árvore de local das rádios (item 8, R4): o carregador valida o esquema, ``children`` nunca passa de 24 opções, ``node`` recusa id forjado e o ⭐
só aparece para país que está na árvore. Os invariantes do arquivo REAL (``data/locations.json``) fecham o contrato (§6)."""

import copy
import json

import pytest

from empiremuse.rules.radio_geo import AREAS, MAX_OPTIONS, parse_node_id
from empiremuse.utils.radio.locations import (
    DEFAULT_PATH,
    LocationDataError,
    LocationTree,
)

pytestmark = pytest.mark.critical


def fake_translate(locale):
    def tr(key, **kw):
        return key + ("|" + "|".join(f"{k}={v}" for k, v in sorted(kw.items())) if kw else "")

    return tr


def tiny() -> dict:
    """Um arquivo mínimo e válido: Brasil com estado único (2 cidades), grupo da região Norte e estado de 1 cidade; os outros países são finais."""
    return {
        "version": 1,
        "areas": {
            "america_norte": ["US"],
            "america_latina": ["BR", "AR"],
            "europa_ocidental_norte": ["DE"],
            "europa_oriental_sul": ["PL"],
            "oriente_medio_africa": ["TR"],
            "asia_pacifico": ["JP"],
        },
        "countries": {
            "BR": {"n": 1603, "states": ["s:BR:29", "s:BR:@norte", "s:BR:07"]},
            "AR": {"n": 30, "states": []},
            "US": {"n": 40, "states": []},
            "DE": {"n": 40, "states": []},
            "PL": {"n": 40, "states": []},
            "TR": {"n": 40, "states": []},
            "JP": {"n": 40, "states": []},
        },
        "states": {
            "s:BR:29": {"kind": "single", "name": "Goiás", "a1": ["29"], "n": 28, "cities": ["x:1", "x:2"]},
            "s:BR:@norte": {
                "kind": "region_rest",
                "name": "norte",
                "group": "br.norte",
                "a1": ["04", "16"],
                "n": 8,
                "cities": ["x:3", "x:4"],
            },
            "s:BR:07": {"kind": "single", "name": "Distrito Federal", "a1": ["07"], "n": 11, "cities": ["x:5"]},
        },
        "cities": {
            "x:1": {"name": "Goiânia", "lat": -16.67861, "lon": -49.25389, "a1": "29"},
            "x:2": {"name": "Anápolis", "lat": -16.32667, "lon": -48.95278, "a1": "29"},
            "x:3": {"name": "Manaus", "lat": -3.10194, "lon": -60.025, "a1": "04"},
            "x:4": {"name": "Belém", "lat": -1.45583, "lon": -48.50444, "a1": "16"},
            "x:5": {"name": "Brasília", "lat": -15.77972, "lon": -47.92972, "a1": "07"},
        },
        "admin1": {
            "BR": {
                "29": {"name": "Goiás", "terms": ["Goiás"], "sigla": "GO"},
                "04": {"name": "Amazonas", "terms": ["Amazonas"]},
                "16": {"name": "Pará", "terms": ["Pará"]},
                "07": {"name": "Distrito Federal", "terms": ["Distrito Federal"]},
            }
        },
        "index": {"aliases": {"BR": {"goias": "29"}}, "cities": {"BR": {"goiania": "29"}}, "names": {"BR": "brazil"}},
    }


def tree(doc=None) -> LocationTree:
    return LocationTree(tiny() if doc is None else doc, translate=fake_translate)


class TestSchema:
    def test_a_valid_document_loads(self):
        assert tree().countries == {"BR", "AR", "US", "DE", "PL", "TR", "JP"}

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(version=2),
            lambda d: d.pop("cities"),
            lambda d: d.update(areas=dict(reversed(list(d["areas"].items())))),  # a ordem do topo é fixa
            lambda d: d["areas"]["asia_pacifico"].append("US"),  # país em duas regiões
            lambda d: d["areas"]["asia_pacifico"].append("XX"),  # região com país que não existe
            lambda d: d["countries"]["BR"]["states"].append("s:BR:99"),
            lambda d: d["countries"]["BR"].update(states=[f"s:BR:{n}" for n in range(25)]),
            lambda d: d["cities"]["x:1"].update(lat=999),
            lambda d: d["cities"]["x:1"].update(a1="04"),  # a cidade não é de nenhuma subdivisão do estado
            lambda d: d["states"]["s:BR:07"]["cities"].append("x:1"),  # cidade em dois estados
            lambda d: d["states"]["s:BR:07"].update(kind="outro"),
            lambda d: d["states"]["s:BR:07"].update(a1=[]),
            lambda d: d["cities"].update(
                {"x:9": {"name": "Solta", "lat": 1, "lon": 1, "a1": "29"}}
            ),  # cidade sem estado
            lambda d: d["index"].pop("names"),
        ],
    )
    def test_a_broken_document_is_refused_with_a_stable_code(self, mutate):
        doc = copy.deepcopy(tiny())
        mutate(doc)

        with pytest.raises(LocationDataError) as raised:
            LocationTree(doc, translate=fake_translate)

        assert raised.value.reason and "Goi" not in raised.value.reason  # o código nunca leva texto do arquivo

    def test_load_reads_a_file_and_refuses_missing_text_and_huge_ones(self, tmp_path):
        good = tmp_path / "ok.json"
        good.write_text(json.dumps(tiny()), encoding="utf-8")
        assert tree_from(good).node("c:BR") is not None
        for name, content in (("bad.json", "{"), ("list.json", "[]"), ("deep.json", "[" * 100000)):
            (tmp_path / name).write_text(content, encoding="utf-8")
            with pytest.raises(LocationDataError):
                tree_from(tmp_path / name)
        with pytest.raises(LocationDataError):
            tree_from(tmp_path / "nao-existe.json")


def tree_from(path) -> LocationTree:
    return LocationTree.load(path, translate=fake_translate)


class TestTop:
    def test_the_star_comes_first_and_only_for_a_country_in_the_tree(self):
        top = tree().children(None, "pt-BR", guild_locale="pt-BR")

        assert [o.id for o in top][:2] == ["c:BR", "r:america_norte"]
        assert len(top) == 7 and top[0].label.startswith("radio.local.guild_country|country=")
        assert [o.id for o in top[1:]] == [f"r:{a}" for a in AREAS]

    def test_without_a_country_the_top_is_just_the_six_regions(self):
        for guild_locale in ("es-419", "no", "fr", None, "lixo"):  # FR existe no mundo, mas não nesta árvore
            top = tree().children(None, "pt-BR", guild_locale=guild_locale)

            assert [o.id for o in top] == [f"r:{a}" for a in AREAS]

    def test_the_count_of_a_region_is_the_sum_of_its_countries(self):
        top = {o.id: o for o in tree().children(None, "pt-BR")}

        assert top["r:america_latina"].count == 1603 + 30


class TestChildren:
    def test_a_region_lists_its_countries_and_a_country_its_states_and_a_state_its_cities(self):
        t = tree()

        assert [o.id for o in t.children("r:america_latina", "pt-BR")] == ["c:BR", "c:AR"]
        assert [o.id for o in t.children("c:BR", "pt-BR")] == ["s:BR:29", "s:BR:@norte", "s:BR:07"]
        assert [o.label for o in t.children("s:BR:29", "pt-BR")] == ["Goiânia", "Anápolis"]

    def test_a_final_node_and_a_forged_id_have_no_children(self):
        t = tree()

        assert t.children("c:AR", "pt-BR") == []  # país com poucas estações: escolha final
        assert t.children("s:BR:07", "pt-BR") == []  # estado com uma cidade só pula o nível de cidade
        assert t.children("x:1", "pt-BR") == []
        for forged in ("c:ZZ", "s:BR:99", "r:nada", "x:99", "c:BR\n", "", 5, ["c:BR"]):
            assert t.children(forged, "pt-BR") == []

    def test_the_group_of_states_is_named_by_the_catalog_and_the_state_by_the_data(self):
        options = {o.id: o for o in tree().children("c:BR", "pt-BR")}

        assert options["s:BR:29"].label == "Goiás"
        assert options["s:BR:@norte"].label.startswith("radio.local.region_rest|region=radio.local.region.br.norte")

    def test_a_label_never_passes_100_characters(self):
        doc = tiny()
        doc["states"]["s:BR:29"]["name"] = "G" * 500
        for option in tree(doc).children("c:BR", "pt-BR"):
            assert len(option.label) <= 100 and len(option.description) <= 100


class TestNode:
    def test_the_finality_of_each_kind(self):
        t = tree()

        assert t.node("r:america_latina").final is False
        assert t.node("c:BR").final is False and t.node("c:AR").final is True
        assert t.node("s:BR:29").final is False  # duas cidades
        assert t.node("s:BR:07").final is True  # uma cidade
        assert t.node("x:1").final is True

    def test_the_fields_the_directory_needs(self):
        city = tree().node("x:1")

        assert (city.kind, city.cc, city.city, city.admin1) == ("city", "BR", "Goiânia", ("29",))
        assert (city.lat, city.lon) == (-16.67861, -49.25389) and city.terms == ("Goiás",) and city.exact == "GO"
        single = tree().node("s:BR:07")  # estado final: consulta pela única cidade dele
        assert single.kind == "state" and single.city == "Brasília" and single.lat == -15.77972

    @pytest.mark.parametrize(
        "forged", ["c:ZZ", "s:BR:99", "r:nada", "x:99", "x:1 ", None, 5, "c:BR" + "x" * 200, b"c:BR"]
    )
    def test_an_unknown_or_forged_id_is_none(self, forged):
        assert tree().node(forged) is None

    def test_the_path_goes_from_the_top_to_the_node(self):
        t = tree()

        assert [n.id for n in t.path("x:3")] == ["r:america_latina", "c:BR", "s:BR:@norte", "x:3"]
        assert [n.kind for n in t.path("c:AR")] == ["region", "country"]
        assert t.path("x:999") == []
        assert [t.label(n, "pt-BR") for n in t.path("x:1")][-2:] == ["Goiás", "Goiânia"]


# ---------------------------------------------------------------------- o arquivo REAL (contrato §6: invariantes de ``locations.json``)


@pytest.fixture(scope="module")
def real() -> LocationTree:
    return LocationTree.load(DEFAULT_PATH, translate=fake_translate)


class TestRealData:
    def test_the_contract_numbers(self, real):
        assert len(real.countries) == 91
        top = real.children(None, "pt-BR")
        assert [o.id for o in top] == [f"r:{a}" for a in AREAS]
        assert all(len(real.children(o.id, "pt-BR")) <= 21 for o in top)  # nenhuma região passa de 21 países

    def test_no_level_has_more_than_24_options_and_every_id_is_reachable(self, real):
        seen_cities: set[str] = set()
        for region in real.children(None, "pt-BR"):
            for country in real.children(region.id, "pt-BR"):
                assert real.node(country.id) is not None
                for state in real.children(country.id, "pt-BR"):
                    assert real.node(state.id).parent == country.id
                    cities = real.children(state.id, "pt-BR")
                    assert len(cities) <= MAX_OPTIONS
                    for city in cities:
                        assert real.node(city.id).parent == state.id and parse_node_id(city.id).kind == "city"
                        seen_cities.add(city.id)
                assert len(real.children(country.id, "pt-BR")) <= MAX_OPTIONS
        assert len(seen_cities) > 5000

    def test_countries_under_50_stations_are_final_and_the_others_have_states(self, real):
        finals = sorted(c for c in real.countries if real.node(f"c:{c}").final)

        assert finals == ["BD", "ET", "HN", "IR", "KZ", "NI", "PA", "SY", "VN"]

    def test_every_state_has_a_city_and_a_valid_query(self, real):
        for cc in real.countries:
            for option in real.children(f"c:{cc}", "pt-BR"):
                node = real.node(option.id)
                assert real.children(node.id, "pt-BR") or node.final
                assert node.admin1 and node.terms, option.id
                if node.final:
                    assert node.lat is not None and node.lon is not None, option.id

    def test_the_star_for_the_country_of_every_discord_locale(self, real):
        for locale in ("pt-BR", "en-US", "es-ES", "fr", "de", "ja", "ko", "uk", "ru", "tr"):
            top = real.children(None, "pt-BR", guild_locale=locale)

            assert len(top) in (6, 7) and (len(top) == 6 or top[0].id.startswith("c:"))
        assert real.children(None, "pt-BR", guild_locale="pt-BR")[0].id == "c:BR"
        assert len(real.children(None, "pt-BR", guild_locale="es-419")) == 6

    def test_the_brazilian_states_use_the_official_regions_and_the_iso_name(self, real):
        labels = {o.id: o.label for o in real.children("c:BR", "pt-BR")}

        assert len(labels) == 24 and "s:BR:@norte" in labels
        assert labels["s:BR:07"] == "Distrito Federal"  # o GeoNames diz "Federal District"
        assert labels["s:BR:27"] == "São Paulo"
