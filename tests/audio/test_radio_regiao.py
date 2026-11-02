"""A busca de estações por local (item 8, R4, contrato §4): até 4 pedidos pelo MESMO balde da R3, o texto de estado conferido pelo nosso resolvedor
(nunca pela substring do diretório), resposta hostil descartada, cache e o balde por servidor. Fetcher, relógio e ``sleep`` falsos, sem espera real."""

import pytest

from empiremuse.rules.radio_geo import StateIndex
from empiremuse.utils.radio.directory import REGION_WAIT_S, Directory, DirectoryError, TokenBucket
from empiremuse.utils.radio.locations import LocNode

pytestmark = pytest.mark.critical

MIRROR = "de1.api.radio-browser.info"
INDEX = StateIndex(
    aliases={"BR": {"goias": "29", "go": "29", "parana": "18", "para": "16"}},
    cities={"BR": {"goiania": "29"}},
    names={"BR": "brazil"},
)
CITY = LocNode(
    "x:1", "city", "Goiânia", "s:BR:29", True, cc="BR", lat=-16.67861, lon=-49.25389,
    admin1=("29",), terms=("Goiás",), exact="GO", city="Goiânia",
)  # fmt: skip
COUNTRY = LocNode("c:ET", "country", "ET", "r:x", True, cc="ET")
NEAR = {"geo_lat": -16.7, "geo_long": -49.2}  # a poucos km da cidade


def uuid(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def station(n, **over):
    return {
        "stationuuid": uuid(n),
        "name": f"Rádio {n}",
        "url_resolved": "http://stream.example:8000/live",
        "codec": "MP3",
        "bitrate": 128,
        "hls": 0,
        "country": "Brasil",
        "countrycode": "BR",
        "clickcount": 10,
        "geo_lat": None,
        "geo_long": None,
        "state": "",
        **over,
    }


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Scripted:
    """Responde por tipo de pedido (geo, estado, sigla exata, nome, país) e registra tudo."""

    def __init__(self, geo=None, state=None, exact=None, name=None, country=None, error=None):
        given = {"geo": geo, "state": state, "exact": exact, "name": name, "country": country}
        self.answers = {kind: ([] if value is None else value) for kind, value in given.items()}
        self.error = error
        self.calls: list[dict] = []

    async def __call__(self, mirror, path, query):
        self.calls.append({"path": path, **query})
        if self.error is not None:
            raise self.error
        if "geo_lat" in query:
            return self.answers["geo"]
        if query.get("stateExact") == "true":
            return self.answers["exact"]
        if "state" in query:
            return self.answers["state"]
        if "name" in query:
            return self.answers["name"]
        return self.answers["country"]


def make(fetcher, *, bucket=None, clock=None):
    clock = clock or Clock()

    async def mirrors():
        return [MIRROR]

    async def sleep(seconds):
        clock.now += seconds  # o tempo só anda quando alguém espera: nenhum teste dorme de verdade

    directory = Directory(
        fetcher=fetcher,
        mirrors=mirrors,
        clock=clock,
        sleep=sleep,
        bucket=bucket or TokenBucket(clock=clock),
        state_index=INDEX,
    )
    return directory, clock


def plenty():
    return TokenBucket(100, 100)


class TestQueries:
    @pytest.mark.asyncio
    async def test_a_city_makes_at_most_four_requests_with_the_contract_parameters(self):
        fetcher = Scripted()
        d, _ = make(fetcher, bucket=plenty())

        await d.region(CITY, key=1)

        kinds = [("geo_lat" in c, c.get("stateExact"), "state" in c, "name" in c) for c in fetcher.calls]
        assert kinds == [
            (True, None, False, False),
            (False, None, True, False),
            (False, "true", True, False),
            (False, None, False, True),
        ]
        geo = fetcher.calls[0]
        assert geo["geo_distance"] == "100000" and geo["geo_lat"] == "-16.67861" and geo["geo_long"] == "-49.25389"
        assert all(
            c["hidebroken"] == "true" and c["order"] == "clickcount" and c["reverse"] == "true" and c["limit"] == "150"
            for c in fetcher.calls
        )
        assert (
            fetcher.calls[1]["countrycode"] == "BR"
            and fetcher.calls[1]["state"] == "Goiás"
            and fetcher.calls[2]["state"] == "GO"
        )
        assert fetcher.calls[3]["name"] == "Goiânia"

    @pytest.mark.asyncio
    async def test_a_small_country_is_one_request_by_country_code(self):
        fetcher = Scripted(country=[station(1, countrycode="ET")])
        d, _ = make(fetcher, bucket=plenty())

        result = await d.region(COUNTRY, key=1)

        assert len(fetcher.calls) == 1 and fetcher.calls[0]["countrycode"] == "ET" and "state" not in fetcher.calls[0]
        assert [h.station.uuid for h in result.hits] == [uuid(1)] and result.partial is False

    @pytest.mark.asyncio
    async def test_a_node_with_nothing_to_ask_or_none_makes_no_request(self):
        fetcher = Scripted()
        d, _ = make(fetcher)

        assert (await d.region(None, key=1)).hits == ()
        assert (await d.region(LocNode("r:x", "region", "x", None, False), key=1)).hits == ()
        assert fetcher.calls == []


class TestMerge:
    @pytest.mark.asyncio
    async def test_near_by_geo_or_name_first_then_state_only_without_coordinates_each_by_clicks(self):
        fetcher = Scripted(
            geo=[station(1, clickcount=5, **NEAR), station(2, clickcount=50, **NEAR)],
            name=[station(3, clickcount=30), station(2, clickcount=50, **NEAR)],
            state=[
                station(4, clickcount=99, state="Goiás"),
                station(5, clickcount=7, state="GO"),
                station(1, clickcount=5, state="Goiás", **NEAR),
            ],
        )
        d, _ = make(fetcher, bucket=plenty())

        result = await d.region(CITY, key=1)

        assert [h.station.uuid for h in result.hits] == [uuid(2), uuid(3), uuid(1), uuid(4), uuid(5)]
        assert [h.matched for h in result.hits] == [
            "geo",
            "name",
            "geo",
            "state",
            "state",
        ]  # repetido (uuid) fica uma vez só
        assert [h.has_geo for h in result.hits] == [True, False, True, False, False]

    @pytest.mark.asyncio
    async def test_a_state_match_with_coordinates_outside_the_radius_is_left_out(self):
        far = {"geo_lat": -12.0, "geo_long": -45.0, "state": "Goiás"}  # no mesmo estado, mas a centenas de km da cidade
        d, _ = make(Scripted(state=[station(1, **far), station(2, state="Goiás")]), bucket=plenty())

        result = await d.region(CITY, key=1)

        assert [h.station.uuid for h in result.hits] == [uuid(2)]

    @pytest.mark.asyncio
    async def test_para_does_not_bring_parana_the_resolver_decides_not_the_directory(self):
        belem = LocNode(
            "x:2",
            "city",
            "Belém",
            "s:BR:16",
            True,
            cc="BR",
            lat=-1.4,
            lon=-48.5,
            admin1=("16",),
            terms=("Pará",),
            city="Belém",
        )
        states = [
            station(1, state="Pará"),
            station(2, state="Paraná"),
            station(3, state="Belém, Pará"),
            station(4, state="Narnia"),
        ]
        d, _ = make(Scripted(state=states), bucket=plenty())

        result = await d.region(belem, key=1)

        assert sorted(h.station.uuid for h in result.hits) == [uuid(1), uuid(3)]

    @pytest.mark.asyncio
    async def test_at_most_150_stations(self):
        d, _ = make(Scripted(geo=[station(n, **NEAR) for n in range(1, 301)]), bucket=plenty())

        assert len((await d.region(CITY, key=1)).hits) == 150

    @pytest.mark.asyncio
    async def test_a_geo_result_across_the_border_is_kept_but_a_name_result_from_another_country_is_not(self):
        d, _ = make(
            Scripted(geo=[station(1, countrycode="PY", **NEAR)], name=[station(2, countrycode="PT")]), bucket=plenty()
        )

        assert [h.station.uuid for h in (await d.region(CITY, key=1)).hits] == [uuid(1)]


class TestHostile:
    @pytest.mark.asyncio
    async def test_invalid_coordinates_clicks_and_stations_are_discarded_or_clamped(self):
        items = [
            station(1, geo_lat=999, geo_long=0),
            station(2, geo_lat="x", geo_long=1),
            station(3, clickcount=10**15, **NEAR),
            station(4, clickcount="lixo", **NEAR),
            station(5, url_resolved="http://127.0.0.1:2333/x", **NEAR),
            station(6, stationuuid="nao-e-uuid", **NEAR),
            "lixo",
            None,
            {"stationuuid": uuid(7)},
        ]
        d, _ = make(Scripted(geo=items), bucket=plenty())

        result = await d.region(CITY, key=1)

        assert {h.station.uuid: h.clickcount for h in result.hits} == {uuid(3): 10**9, uuid(4): 0}

    @pytest.mark.asyncio
    async def test_a_state_text_of_10_kb_is_not_resolved_and_not_a_match(self):
        d, _ = make(Scripted(state=[station(1, state="Goiás" + " " * 10_000)]), bucket=plenty())

        assert (await d.region(CITY, key=1)).hits == ()

    @pytest.mark.asyncio
    async def test_a_response_that_is_not_a_list_gives_nothing(self):
        d, _ = make(Scripted(geo={"x": 1}, state="lixo", name=5), bucket=plenty())

        result = await d.region(CITY, key=1)

        assert result.hits == () and result.partial is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [DirectoryError("size"), DirectoryError("json"), RuntimeError("x")])
    async def test_a_failing_directory_is_partial_never_an_error(self, error):
        d, _ = make(Scripted(error=error), bucket=plenty())

        result = await d.region(CITY, key=1)

        assert result.hits == () and result.partial is True

    @pytest.mark.asyncio
    async def test_the_station_name_still_passes_through_the_single_external_text_function(self):
        hostile = station(1, name="@everyone <@1> https://evil.example", countrycode="ET")
        d, _ = make(Scripted(country=[hostile]), bucket=plenty())

        name = (await d.region(COUNTRY, key=1)).hits[0].station.name

        assert "@everyone" not in name and "<@" not in name and "https://" not in name


class TestLimits:
    @pytest.mark.asyncio
    async def test_with_the_global_bucket_empty_it_waits_up_to_six_seconds_then_returns_partial(self):
        clock = Clock()
        bucket = TokenBucket(0.01, 2.0, clock=clock)  # quase não repõe: a 1ª ficha sai e a reserva do play fica
        fetcher = Scripted(geo=[station(1, **NEAR)])
        d, _ = make(fetcher, bucket=bucket, clock=clock)

        result = await d.region(CITY, key=1)

        assert (
            len(fetcher.calls) == 1 and result.partial is True
        )  # o 1º saiu; os outros 3 esperaram o prazo e desistiram
        assert [h.station.uuid for h in result.hits] == [uuid(1)]
        assert REGION_WAIT_S <= clock.now - 1000.0 < REGION_WAIT_S + 1

    @pytest.mark.asyncio
    async def test_the_play_and_health_check_reserve_is_never_spent_by_the_region_search(self):
        clock = Clock()
        bucket = TokenBucket(0.0, 1.0, clock=clock)  # só 1 ficha: é a reserva
        fetcher = Scripted()
        d, _ = make(fetcher, bucket=bucket, clock=clock)

        result = await d.region(CITY, key=1)

        assert fetcher.calls == [] and result.partial is True
        assert bucket.try_acquire() is True  # a ficha do play continua lá

    @pytest.mark.asyncio
    async def test_repeating_the_same_choice_costs_zero_requests(self):
        fetcher = Scripted(geo=[station(1, **NEAR)])
        d, _ = make(fetcher, bucket=plenty())
        first = await d.region(CITY, key=1)
        sent = len(fetcher.calls)

        again = await d.region(CITY, key=2)

        assert len(fetcher.calls) == sent and again.hits == first.hits and again.partial is False

    @pytest.mark.asyncio
    async def test_a_partial_result_is_not_cached(self):
        d, _ = make(Scripted(error=DirectoryError("status")), bucket=plenty())
        assert (await d.region(CITY, key=1)).partial is True

        d._fetch = Scripted(geo=[station(9, **NEAR)])

        assert [h.station.uuid for h in (await d.region(CITY, key=1)).hits] == [uuid(9)]

    @pytest.mark.asyncio
    async def test_one_server_pays_one_token_per_choice_and_runs_out_after_a_burst_of_three(self):
        fetcher = Scripted()
        d, clock = make(fetcher, bucket=plenty())
        nodes = [
            LocNode(
                f"x:{n}",
                "city",
                f"C{n}",
                "s:BR:29",
                True,
                cc="BR",
                lat=-16.0 - n,
                lon=-49.0,
                admin1=("29",),
                terms=("Goiás",),
                exact="GO",
                city=f"C{n}",
            )
            for n in range(5)
        ]

        results = [await d.region(node, key=7) for node in nodes]

        assert [r.partial for r in results] == [False, False, False, True, True]  # a rajada do servidor é 3
        assert len(fetcher.calls) == 3 * 4  # as recusadas não gastaram pedido nenhum
        assert (await d.region(nodes[3], key=8)).partial is False  # outro servidor não é afetado
        clock.now += 10  # o balde do servidor repõe (uma a cada 2 s)
        assert (await d.region(nodes[4], key=7)).partial is False
