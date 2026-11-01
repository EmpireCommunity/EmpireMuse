"""Dublê de ``pool.radio`` para a página Rádios, sobre as regras REAIS (``rules.radio``) e as exceções reais da store.

Só o banco é de mentira: a store em memória valida com ``check_chat`` como a de verdade e faz compare-and-set em ``rev``;
``premium.get`` e ``service.state`` seguem a interface do ``RadioHub`` (R1 §3). ``RadioUnavailable`` pode ser armada para
o teste do 503.
"""

from dataclasses import replace
from types import SimpleNamespace

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
        """Como a store real (R3): só rádio ao vivo, uuid válido e diferente da principal, compare-and-set em ``rev``."""
        self._check()
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


class FakeDirectory:
    """Radio Browser de mentira: ``results`` para qualquer termo válido, ``stations`` por uuid; registra os termos buscados."""

    def __init__(self):
        self.results = []
        self.stations = {}
        self.calls = []

    async def search(self, term, *, key=None):
        self.calls.append((term, key))
        return list(self.results)

    async def by_uuid(self, uuid):
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

    def update_chat_rules(self, radio):
        self.chat_updates.append(radio)


class FakeRadioApi:
    def __init__(self):
        self.store, self.premium, self.service = FakeStore(), FakePremium(), FakeService()
        self.directory, self.health = FakeDirectory(), FakeHealth()
        self.manager = FakeManager()


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
