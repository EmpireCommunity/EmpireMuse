"""Dublê de ``pool.radio`` para a página Rádios, com a interface combinada com o Turbine (contrato R1 §1 e §3).

``Radio`` congelada (sem ``updated_by``), ``check_radio`` que devolve ``Radio | RuleError(code, key)`` sem levantar, store com
compare-and-set em ``rev`` que valida de novo, ``premium.get`` e ``service.state``. Enquanto o esqueleto da R1 não está na
base, as portas ``check_radio``/``store_errors`` do serviço do painel são trocadas por estas.
"""

from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace

import disnake

from empiremuse.panel.services import radio as radio_service
from empiremuse.rules.errors import RuleError

RANGES = {"voice_delete_after_s": (5, 600), "stage_max_messages": (3, 50), "stage_max_age_s": (60, 3600)}
DEFAULT_CHAT = {"voice_delete_after_s": 30, "stage_max_messages": 7, "stage_max_age_s": 600}


@dataclass(frozen=True)
class Radio:
    id: str
    bot_id: int
    guild_id: int
    channel_id: int
    kind: str
    source: dict
    chat: dict
    enabled: bool
    rev: int


def check_radio(doc):
    chat = doc.get("chat") or {}
    for name, (low, high) in RANGES.items():
        value = chat.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            return RuleError("chat_range", name, value, high)
    return Radio(**{name: doc[name] for name in Radio.__dataclass_fields__})


class RadioConflict(Exception):
    pass


class RadioInvalid(Exception):
    pass


class FakeStore:
    def __init__(self):
        self.docs = {}
        self.updates = []  # (radio_id, updated_by)

    def seed(self, bot_id, guild_id, channel_id, *, kind="custom", enabled=True, rev=3, chat=None):
        radio = Radio(
            f"{bot_id}:{guild_id}",
            bot_id,
            guild_id,
            channel_id,
            kind,
            {"playlist": "Rock"},
            dict(chat or DEFAULT_CHAT),
            enabled,
            rev,
        )
        self.docs[radio.id] = radio
        return radio

    async def list_for_guild(self, guild_id):
        return [r for r in self.docs.values() if r.guild_id == int(guild_id)]

    async def get(self, radio_id):
        return self.docs.get(radio_id)

    async def update_chat(self, radio_id, chat, *, expected_rev, updated_by):
        radio = self.docs[radio_id]
        if radio.rev != expected_rev:
            raise RadioConflict
        if isinstance(check_radio({**asdict(radio), "chat": chat}), RuleError):
            raise RadioInvalid
        saved = replace(radio, chat=dict(chat), rev=radio.rev + 1)
        self.docs[radio_id] = saved
        self.updates.append((radio_id, updated_by))
        return saved


class FakePremium:
    def __init__(self):
        self.docs = {}

    def grant(self, guild_id, active=1):
        self.docs[int(guild_id)] = SimpleNamespace(active=active, granted_by="owner", granted_at=0)

    async def get(self, guild_id):
        return self.docs.get(int(guild_id))


class FakeService:
    def __init__(self):
        self.states = {}

    def state(self, radio_id):
        return self.states.get(radio_id)


class FakeRadioApi:
    def __init__(self):
        self.store, self.premium, self.service = FakeStore(), FakePremium(), FakeService()


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


def install(monkeypatch, pool):
    api = FakeRadioApi()
    pool.radio = api
    monkeypatch.setattr(radio_service, "check_radio", check_radio)
    monkeypatch.setattr(radio_service, "store_errors", lambda: (RadioConflict, RadioInvalid))
    return api
