"""Dublê de ``pool.radio`` para a página Rádios, sobre as regras REAIS (``rules.radio``) e as exceções reais da store.

A árvore de local (R4) é um ``FakeTree`` pequeno com a mesma interface do ``LocationTree`` (``node``, ``path``,
``children`` com o ⭐ do país do servidor) e ``directory.region`` devolve ``RegionResult(hits, partial)``.

Só o banco é de mentira: a store em memória valida com ``check_chat`` como a de verdade e faz compare-and-set em ``rev``;
``premium.get`` e ``service.state`` seguem a interface do ``RadioHub`` (R1 §3). ``RadioUnavailable`` pode ser armada para
o teste do 503.
"""

from dataclasses import replace
from types import SimpleNamespace
from typing import NamedTuple, Optional

import disnake

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import KIND_LIVE, Radio, check_chat, check_radio, default_chat, radio_id
from empiremuse.rules.errors import RuleError as _RuleError  # noqa: F401 - reexport para os testes
from empiremuse.rules.radio_stations import valid_uuid
from empiremuse.utils.radio.premium import Premium, RadioUnavailable
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound


class FakeStore:
    def __init__(self):
        self.docs = {}
        self.updates = []  # (radio_id, updated_by)
        self.fail_with = None  # exceção a levantar em toda leitura (ex.: RadioUnavailable)
        self.set_reserve_error = None
        self.set_station_error = None

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

    async def set_station(self, radio_id_, station, *, expected_rev, updated_by=""):
        """Como a store real (R4): só rádio ao vivo, uuid válido, compare-and-set em ``rev``; a reserva igual à principal
        nova é zerada. ``set_station_error`` simula a store recusando DEPOIS do pré-check do painel."""
        self._check()
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


class FakeRadioApi:
    def __init__(self):
        self.store, self.premium, self.service = FakeStore(), FakePremium(), FakeService()
        self.directory, self.health = FakeDirectory(), FakeHealth()
        self.manager = FakeManager()
        self.locations = build_tree()  # ``None`` = o arquivo não carregou: a página fica sem o filtro


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
