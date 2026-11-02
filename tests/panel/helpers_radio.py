"""Dublê de ``pool.radio`` para a página Rádios, sobre as regras REAIS (``rules.radio``) e as exceções reais da store.

A árvore de local (R4) é um ``FakeTree`` pequeno com a mesma interface do ``LocationTree`` (``node``, ``path``,
``children`` com o ⭐ do país do servidor) e ``directory.region`` devolve ``RegionResult(hits, partial)``.

Só o banco é de mentira: a store em memória valida com ``check_chat`` como a de verdade e faz compare-and-set em ``rev``;
``premium.get`` e ``service.state`` seguem a interface do ``RadioHub`` (R1 §3). ``RadioUnavailable`` pode ser armada para
o teste do 503.
"""

import secrets
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import NamedTuple, Optional

import disnake

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import KIND_LIVE, Radio, check_chat, check_radio, default_chat, radio_id
from empiremuse.rules.radio import radio_id as radio_id_of
from empiremuse.rules.errors import RuleError as _RuleError  # noqa: F401 - reexport para os testes
from empiremuse.rules.radio_stations import valid_uuid
from empiremuse.utils.radio.premium import Premium, RadioUnavailable
from empiremuse.utils.radio.setup import RadioForbidden, SetupOptions
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound


KEEP = object()  # como ``utils.radio.store.KEEP``: "não mexa" na lista


class FakeStore:
    def __init__(self):
        self.docs = {}
        self.updates = []  # (radio_id, updated_by)
        self.fail_with = None  # exceção a levantar em toda leitura (ex.: RadioUnavailable)
        self.set_reserve_error = None
        self.set_station_error = None
        self.stations = None  # ligado por FakeRadioApi (a store de estações, para set_custom_station)
        self.station_lists = {}  # radio_id -> a lista gravada por set_station (None = tirada)

    def seed(self, bot_id, guild_id, channel_id, *, kind="custom", enabled=True, rev=3, chat=None):
        source = {"playlist": "Rock"} if kind == "custom" else {"station": "0" * 8 + "-0000-0000-0000-" + "0" * 12}
        radio = check_radio(
            {
                "id": radio_id(bot_id, guild_id),
                "channel_id": channel_id,
                "kind": kind,
                "source": source,
                "chat": dict(chat or default_chat()),
                "enabled": enabled,
                "rev": rev,
            }
        )
        assert isinstance(radio, Radio), radio
        self.docs[radio.id] = radio
        return radio

    def seed_live(self, bot_id, guild_id, channel_id, *, station, reserve=None, enabled=True, rev=3):
        radio = check_radio(
            {
                "id": radio_id(bot_id, guild_id),
                "channel_id": channel_id,
                "kind": KIND_LIVE,
                "source": {"station": station, "reserve": reserve},
                "chat": default_chat(),
                "enabled": enabled,
                "rev": rev,
            }
        )
        assert isinstance(radio, Radio), radio
        self.docs[radio.id] = radio
        return radio

    def _check(self):
        if self.fail_with is not None:
            raise self.fail_with

    async def list_for_guild(self, guild_id):
        self._check()
        return [r for r in self.docs.values() if r.guild_id == int(guild_id)]

    async def get(self, radio_id_):
        self._check()
        return self.docs.get(radio_id_)

    async def update_chat(self, radio_id_, chat, *, expected_rev, updated_by=""):
        self._check()
        radio = self.docs.get(radio_id_)
        if radio is None:
            raise RadioNotFound
        if radio.rev != expected_rev:
            raise RadioConflict
        merged = check_chat(chat, base=radio.chat)
        if isinstance(merged, RuleError):
            raise RadioInvalid(merged)
        saved = replace(radio, chat=merged, rev=radio.rev + 1)
        self.docs[radio_id_] = saved
        self.updates.append((radio_id_, updated_by))
        return saved

    async def set_reserve(self, radio_id_, reserve, *, expected_rev, updated_by=""):
        """Como a store real (R3): só rádio ao vivo, uuid válido e diferente da principal, compare-and-set em ``rev``.
        ``set_reserve_error`` (uma exceção) simula a store recusando DEPOIS do pré-check do painel."""
        self._check()
        if self.set_reserve_error is not None:
            raise self.set_reserve_error
        radio = self.docs.get(radio_id_)
        if radio is None:
            raise RadioNotFound
        if radio.rev != expected_rev:
            raise RadioConflict
        if radio.kind != KIND_LIVE or reserve == radio.source.get("station"):
            raise RadioInvalid(RuleError("radio_source_invalid", "reserve"))
        if reserve is not None and not valid_uuid(reserve):
            raise RadioInvalid(RuleError("radio_source_invalid", "reserve"))
        saved = replace(radio, source={"station": radio.source["station"], "reserve": reserve}, rev=radio.rev + 1)
        self.docs[radio_id_] = saved
        self.updates.append((radio_id_, updated_by))
        return saved

    async def set_station(self, radio_id_, station, *, expected_rev, updated_by="", station_list=KEEP):
        """Como a store real (R4): só rádio ao vivo, uuid válido, compare-and-set em ``rev``; a reserva igual à principal
        nova é zerada; ``station_list`` (a lista de onde veio, ``None`` tira, ``KEEP`` mantém) fica em ``station_lists``.
        ``set_station_error`` simula a store recusando DEPOIS do pré-check do painel."""
        self._check()
        if station_list is not KEEP:
            self.station_lists[radio_id_] = station_list
        if self.set_station_error is not None:
            raise self.set_station_error
        radio = self.docs.get(radio_id_)
        if radio is None:
            raise RadioNotFound
        if radio.rev != expected_rev:
            raise RadioConflict
        if radio.kind != KIND_LIVE or not valid_uuid(station):
            raise RadioInvalid(RuleError("radio_source_invalid", "station"))
        reserve = radio.source.get("reserve")
        source = {"station": station, "reserve": None if reserve == station else reserve}
        saved = replace(radio, source=source, rev=radio.rev + 1)
        self.docs[radio_id_] = saved
        self.updates.append((radio_id_, updated_by))
        return saved

    async def set_custom_station(self, radio_id_, station_id, *, expected_rev, updated_by=""):
        """Como a store real (R5): só rádio personalizada e estação do MESMO servidor, compare-and-set em ``rev``."""
        self._check()
        radio = self.docs.get(radio_id_)
        if radio is None:
            raise RadioNotFound
        if radio.rev != expected_rev:
            raise RadioConflict
        station = self.stations.docs.get(station_id) if self.stations is not None else None
        if radio.kind != "custom" or station is None or station.guild_id != radio.guild_id:
            raise RadioInvalid(RuleError("radio_source_invalid", "station"))
        saved = replace(radio, source={"station": station_id}, rev=radio.rev + 1)
        self.docs[radio_id_] = saved
        self.updates.append((radio_id_, updated_by))
        return saved


class Hit(NamedTuple):
    station: object
    clickcount: int
    has_geo: bool
    matched: str


class RegionResult(NamedTuple):
    hits: tuple
    partial: bool


class LogoResult(NamedTuple):
    png: Optional[bytes]
    transient: bool


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cfc0f01f00050001"
    "ff7b5ab4a30000000049454e44ae426082"
)


class FakeDirectory:
    """Radio Browser de mentira: ``results`` para qualquer termo válido, ``stations`` por uuid, ``region_hits`` para
    qualquer nó final; registra os termos buscados e os nós pedidos."""

    def __init__(self):
        self.results = []
        self.stations = {}
        self.calls = []
        self.by_uuid_calls = []
        self.region_hits = []
        self.region_partial = False
        self.region_calls = []  # (node.id, key)
        self.logos = {}  # uuid -> LogoResult; ausente = LogoResult(None, False)
        self.logo_calls = []

    async def logo_png(self, uuid):
        self.logo_calls.append(uuid)
        return self.logos.get(uuid, LogoResult(None, False))

    async def region(self, node, *, key=None):
        self.region_calls.append((node.id, key))
        return RegionResult(tuple(Hit(s, 10, True, "geo") for s in self.region_hits), self.region_partial)

    async def search(self, term, *, key=None):
        self.calls.append((term, key))
        return list(self.results)

    async def by_uuid(self, uuid):
        self.by_uuid_calls.append(uuid)
        return self.stations.get(uuid)


class FakeHealth:
    def __init__(self):
        self.down = set()

    def unavailable(self, uuid):
        return uuid in self.down


class FakePremium:
    def __init__(self):
        self.docs = {}
        self.fail_with = None

    def grant(self, guild_id, active=1):
        self.docs[int(guild_id)] = Premium(guild_id=int(guild_id), active=active, granted_by="owner")

    async def get(self, guild_id):
        if self.fail_with is not None:
            raise self.fail_with
        return self.docs.get(int(guild_id))


class FakeService:
    def __init__(self):
        self.states = {}

    def state(self, radio_id_):
        return self.states.get(radio_id_)


class FakeManager:
    """Só o que a página chama: ``update_chat_rules`` (o varredor da rádio ligada recebe o chat novo)."""

    def __init__(self):
        self.chat_updates = []  # Radio recebidas
        self.station_changes = []  # Radio recebidas em change_station
        self.fail_with = None  # exceção a levantar em update_chat_rules (o save não pode depender do varredor)

    def update_chat_rules(self, radio):
        if self.fail_with is not None:
            raise self.fail_with
        self.chat_updates.append(radio)

    async def change_station(self, radio):
        """A rádio ligada troca de principal na hora (R4); ``fail_with`` também vale aqui."""
        if self.fail_with is not None:
            raise self.fail_with
        self.station_changes.append(radio)


class LocNode(NamedTuple):
    id: str
    kind: str
    name: str
    parent: Optional[str]
    final: bool


class LocOption(NamedTuple):
    id: str
    label: str
    description: str
    count: int


class FakeTree:
    """``LocationTree`` pequeno: 6 regiões, Brasil com 2 estados (um é grupo) e cidades, Nicarágua como país final."""

    LIMIT = 24

    def __init__(self):
        self.nodes = {}
        self.below = {None: []}
        self.labels = {}
        self.counts = {}
        self.descriptions = {}
        self.calls = []  # (parent, locale, guild_locale)

    def add(self, id_, kind, name, parent=None, *, final=False, count=0, description=""):
        self.nodes[id_] = LocNode(id_, kind, name, parent, final)
        self.below.setdefault(parent, []).append(id_)
        self.below.setdefault(id_, [])
        self.counts[id_], self.descriptions[id_] = count, description
        return self.nodes[id_]

    def node(self, id_):
        if not isinstance(id_, str) or len(id_) >= 100:
            return None
        return self.nodes.get(id_)

    def path(self, id_):
        chain = []
        node = self.nodes.get(id_)
        while node is not None:
            chain.append(node)
            node = self.nodes.get(node.parent) if node.parent else None
        return list(reversed(chain))

    def children(self, parent, locale, *, guild_locale=None):
        self.calls.append((parent, locale, guild_locale))
        items = [
            LocOption(i, self.nodes[i].name, self.descriptions[i] or self._count_text(i), self.counts[i])
            for i in self.below.get(parent, [])
        ]
        if parent is None and guild_locale == "pt-BR" and "c:BR" in self.nodes:
            items.insert(0, LocOption("c:BR", "⭐ Brasil", "", self.counts["c:BR"]))
        return items[: self.LIMIT]

    def _count_text(self, id_):
        """Como a árvore real: região = "n países", país/estado = "n estações", cidade = nada."""
        node = self.nodes[id_]
        if node.kind == "region":
            return f"{len(self.below[id_])} países"
        return f"{self.counts[id_]} estações" if node.kind in ("country", "state") else ""


REGIONS = (
    ("r:america_norte", "América do Norte"),
    ("r:america_latina", "América Latina"),
    ("r:europa_ocidental_norte", "Europa Ocidental e do Norte"),
    ("r:europa_oriental_sul", "Europa Oriental e do Sul"),
    ("r:oriente_medio_africa", "Oriente Médio e África"),
    ("r:asia_pacifico", "Ásia-Pacífico"),
)
SP, CAMPINAS, PORTO_VELHO = "x:3448439", "x:3467865", "x:3662762"


def build_tree():
    tree = FakeTree()
    for id_, name in REGIONS:
        tree.add(id_, "region", name)
    tree.add("c:BR", "country", "Brasil", "r:america_latina", count=1603)
    tree.add("c:NI", "country", "Nicarágua", "r:america_latina", final=True, count=26)
    tree.add("c:DE", "country", "Alemanha", "r:europa_ocidental_norte", count=6288)
    tree.add("s:BR:27", "state", "São Paulo", "c:BR", count=317)
    tree.add(
        "s:BR:@norte", "state", "Demais estados: Norte", "c:BR", count=8, description="Acre, Amapá, Rondônia, Roraima"
    )
    tree.add(SP, "city", "São Paulo", "s:BR:27", final=True)
    tree.add(CAMPINAS, "city", "Campinas", "s:BR:27", final=True)
    tree.add(PORTO_VELHO, "city", "Porto Velho", "s:BR:@norte", final=True)
    return tree


class CustomStation(NamedTuple):
    id: str
    guild_id: int
    name: str
    kind: str
    source: dict
    rev: int
    updated_by: str
    updated_at: datetime
    unknown: tuple = ()


class StationInUse(RadioInvalid):
    """Como a da store real (R5): ``RadioInvalid`` com as rádios que usam a estação."""

    def __init__(self, radios):
        super().__init__(RuleError("station_in_use", "limit"))
        self.radios = list(radios)


STATION_KINDS = ("genre", "artists", "playlists")
GENRE_GROUPS = (  # o catálogo do bot (utils/radio/data/genres.json, 33 ids estáveis)
    (
        "international",
        (
            "rock",
            "metal",
            "pop",
            "kpop",
            "jpop",
            "hiphop",
            "trap",
            "rnb",
            "electronic",
            "house",
            "lofi",
            "chill",
            "jazz",
            "blues",
            "classical",
            "reggae",
            "latin",
            "phonk",
            "afrobeats",
            "eighties",
            "nineties",
        ),
    ),
    (
        "brazilian",
        (
            "sertanejo_uni",
            "sertanejo_modao",
            "agronejo",
            "funk_br",
            "trap_rap_br",
            "pagode_samba",
            "piseiro_arrocha",
            "forro",
            "mpb",
            "gospel",
            "rock_br",
            "phonk_br",
        ),
    ),
)
GENRES = tuple(g for _group, ids in GENRE_GROUPS for g in ids)


class FakeStations:
    """``hub.stations`` de mentira com a validação do ``check_station_doc`` (R5 §2) DENTRO, como a store real."""

    MAX = 10

    def __init__(self, radio_store):
        self.docs = {}
        self.playlists = {}  # guild_id -> nomes de servers.playlists (o teste alinha com world.set_playlists)
        self.updates = []  # (station_id, what, updated_by)
        self.fail_with = None
        self._radios = radio_store

    def genre_groups(self):
        return GENRE_GROUPS

    def seed(self, guild_id, name, kind="genre", source=None, *, rev=1, unknown=()):
        station = CustomStation(
            secrets.token_hex(12),
            int(guild_id),
            name,
            kind,
            dict(source or {"genre": "lofi"}),
            rev,
            "seed",
            datetime.now(timezone.utc),
            tuple(unknown),
        )
        self.docs[station.id] = station
        return station

    def _check(self):
        if self.fail_with is not None:
            raise self.fail_with

    def _validate(self, guild_id, raw, *, skip=None, playlists=()):
        name = str(raw.get("name") or "").strip()
        if not 1 <= len(name) <= 32:
            raise RadioInvalid(RuleError("station_name", "name"))
        taken = {s.name.casefold() for s in self.docs.values() if s.guild_id == guild_id and s.id != skip}
        if name.casefold() in taken:
            raise RadioInvalid(RuleError("station_name_taken", "name"))
        kind = raw.get("kind")
        if kind not in STATION_KINDS:
            raise RadioInvalid(RuleError("station_kind", "kind"))
        source = dict(raw.get("source") or {})
        if set(source) != {kind}:
            raise RadioInvalid(RuleError("station_source", kind))
        if kind == "genre" and source["genre"] not in GENRES:
            raise RadioInvalid(RuleError("station_genre", "genre"))
        if kind == "artists":
            artists = [str(a).strip() for a in source["artists"]]
            if not 1 <= len(artists) <= 10 or any(not 1 <= len(a) <= 64 for a in artists):
                raise RadioInvalid(RuleError("station_artists", "artists"))
            source = {"artists": artists}
        if kind == "playlists":
            wanted = [str(p) for p in source["playlists"]]
            known = set(playlists) | set(self.playlists.get(guild_id, ()))
            if not 1 <= len(wanted) <= 5 or any(p not in known for p in wanted):
                raise RadioInvalid(RuleError("station_playlists", "playlists"))
        if skip is None and sum(1 for s in self.docs.values() if s.guild_id == guild_id) >= self.MAX:
            raise RadioInvalid(RuleError("station_limit", "limit"))
        return name, kind, source

    async def list_for_guild(self, guild_id):
        self._check()
        return [s for s in self.docs.values() if s.guild_id == int(guild_id)]

    async def get(self, guild_id, station_id):
        self._check()
        station = self.docs.get(station_id)
        return station if station is not None and station.guild_id == int(guild_id) else None

    async def create(self, guild_id, raw, *, playlists=(), updated_by=""):
        self._check()
        name, kind, source = self._validate(int(guild_id), raw, playlists=playlists)
        station = CustomStation(
            secrets.token_hex(12), int(guild_id), name, kind, source, 1, updated_by, datetime.now(timezone.utc)
        )
        self.docs[station.id] = station
        self.updates.append((station.id, "create", updated_by))
        return station

    async def update(self, guild_id, station_id, raw, *, expected_rev, playlists=(), updated_by=""):
        self._check()
        station = await self.get(guild_id, station_id)
        if station is None:
            raise RadioNotFound
        if station.rev != expected_rev:
            raise RadioConflict
        name, kind, source = self._validate(station.guild_id, raw, skip=station_id, playlists=playlists)
        saved = station._replace(name=name, kind=kind, source=source, rev=station.rev + 1, updated_by=updated_by)
        self.docs[station_id] = saved
        self.updates.append((station_id, "update", updated_by))
        return saved

    async def radios_using(self, guild_id, station_id):
        self._check()
        return [
            r
            for r in self._radios.docs.values()
            if r.guild_id == int(guild_id) and r.source.get("station") == station_id
        ]

    async def delete(self, guild_id, station_id, *, expected_rev):
        self._check()
        station = await self.get(guild_id, station_id)
        if station is None:
            raise RadioNotFound
        if station.rev != expected_rev:
            raise RadioConflict
        using = await self.radios_using(guild_id, station_id)
        if using:
            raise StationInUse([r.id for r in using])  # ids, como a store real
        del self.docs[station_id]
        self.updates.append((station_id, "delete", ""))


class FakeSetup:
    """``utils/radio/setup.py`` de mentira (§4.3): as listas vêm de ``bots``/``channels`` por servidor; ``configure`` e
    ``reset`` aplicam as regras do contrato sobre a ``FakeStore`` (uma por bot, máximo 3, CAS em ``rev``, rádio ligada
    não troca) e registram as chamadas; ``fail_with`` simula uma recusa do bot (ex.: ``RadioForbidden``)."""

    def __init__(self, api):
        self.api = api
        self.bots = {}  # guild_id -> ((bot_id, nome), ...)
        self.channels = {}  # guild_id -> ((channel_id, nome, "voice"|"stage"), ...)
        self.calls = []  # ("configure"|"reset", guild_id, actor_id, kwargs)
        self.fail_with = None

    async def options(self, guild_id):
        return SetupOptions(tuple(self.bots.get(int(guild_id), ())), tuple(self.channels.get(int(guild_id), ())))

    def _refuse(self, code, key):
        raise RadioInvalid(RuleError(code, key))

    async def configure(self, guild_id, actor_id, *, radio_id, channel_id, bot_id, kind, rev):
        self.calls.append(
            (
                "configure",
                guild_id,
                actor_id,
                dict(radio_id=radio_id, channel_id=channel_id, bot_id=bot_id, kind=kind, rev=rev),
            )
        )
        if self.fail_with is not None:
            raise self.fail_with
        assert isinstance(actor_id, int) and isinstance(channel_id, int) and isinstance(bot_id, int)
        current = None
        if radio_id is not None:
            current = self.api.store.docs.get(radio_id)
            if current is None:
                raise RadioNotFound
            if current.rev != rev:
                raise RadioConflict
        if kind not in ("live", "custom"):
            self._refuse("kind", "kind")
        if channel_id not in {c[0] for c in self.channels.get(int(guild_id), ())}:
            self._refuse("channel_kind", "channel_id")
        free = {b[0] for b in self.bots.get(int(guild_id), ())}
        if bot_id not in free and (current is None or bot_id != current.bot_id):
            self._refuse("reserve_only", "bot_id")
        mine = [r for r in self.api.store.docs.values() if r.guild_id == int(guild_id)]
        if current is None and len(mine) >= 3:
            self._refuse("max_radios", "bot_id")
        if current is None and radio_id_of(bot_id, guild_id) in self.api.store.docs:
            self._refuse("one_per_bot", "bot_id")
        if (
            current is not None
            and current.enabled
            and (channel_id, bot_id, kind) != (current.channel_id, current.bot_id, current.kind)
        ):
            self._refuse("radio_on", "channel_id")
        if current is not None and bot_id != current.bot_id:
            del self.api.store.docs[current.id]
        source = {"playlist": "Rock"} if kind == "custom" else {"station": "0" * 8 + "-0000-0000-0000-" + "0" * 12}
        radio = check_radio(
            {
                "id": radio_id_of(bot_id, guild_id),
                "channel_id": channel_id,
                "kind": kind,
                "source": source if current is None or current.kind != kind else dict(current.source),
                "chat": default_chat() if current is None else dict(current.chat),
                "enabled": False if current is None else current.enabled,
                "rev": 1 if current is None else current.rev + 1,
            }
        )
        assert isinstance(radio, Radio), radio
        self.api.store.docs[radio.id] = radio
        return radio

    async def reset(self, guild_id, actor_id, radio_id, rev):
        self.calls.append(("reset", guild_id, actor_id, dict(radio_id=radio_id, rev=rev)))
        if self.fail_with is not None:
            raise self.fail_with
        current = self.api.store.docs.get(radio_id)
        if current is None or current.guild_id != int(guild_id):
            raise RadioNotFound
        if current.rev != rev:
            raise RadioConflict
        del self.api.store.docs[radio_id]


class FakeRadioApi:
    def __init__(self):
        self.store, self.premium, self.service = FakeStore(), FakePremium(), FakeService()
        self.directory, self.health = FakeDirectory(), FakeHealth()
        self.manager = FakeManager()
        self.locations = build_tree()  # ``None`` = o arquivo não carregou: a página fica sem o filtro
        self.stations = FakeStations(self.store)
        self.store.stations = self.stations
        self.setup = FakeSetup(self)


def radio_state(*, playing=True, paused_by_owner=False, on_reserve=False, offline=False, title=None):
    return SimpleNamespace(
        playing=playing, paused_by_owner=paused_by_owner, on_reserve=on_reserve, offline=offline, title=title
    )


def make_channel(channel_id, name, guild_id, *, manage_messages=True):
    """Canal do cache do bot com ``permissions_for`` (só o que o painel lê)."""
    permissions = disnake.Permissions(manage_messages=manage_messages)
    return SimpleNamespace(
        id=channel_id, name=name, guild=SimpleNamespace(id=guild_id), permissions_for=lambda member: permissions
    )


def install(pool):
    pool.radio = FakeRadioApi()
    return pool.radio


UNAVAILABLE = RadioUnavailable
FORBIDDEN = RadioForbidden
