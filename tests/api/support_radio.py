"""Mundo das rádios na API (B4): o ``DataWorld`` (servidores 100 e 800, bots 1 e 2) com a rádio de mentira do painel
(``FakeRadioApi``: store com CAS, estações, diretório, ``setup`` e premium), por cima das regras reais.

Servidor 100 (Alfa): bot 1, o usuário ``1001`` é administrador e ``2002`` também. Servidor 800 (Ômega): bots 1 e 2. O canal
123 (de Alfa) deixa o usuário gerenciar mensagens; o 456 (de Ômega) não.
"""

from empiremuse.panel.state import ensure_panel_state  # noqa: F401 - o estado do pool é criado pela base
from tests.api.support import body_of
from tests.api.support_data import DataApiCase
from tests.panel.helpers_radio import PNG_1PX, install, make_channel, radio_state  # noqa: F401
from tests.panel.helpers_settings import UID

ALFA, OMEGA = 100, 800
CHANNEL = 123
OTHER_USER = "2002"
LIVE_UUID = "11111111-1111-1111-1111-111111111111"
OTHER_UUID = "22222222-2222-2222-2222-222222222222"
BASE = f"/guilds/{ALFA}"


def station(uuid, name="Rock FM", country="Brasil"):
    from empiremuse.rules.radio_stations import Station

    return Station(
        uuid, name, "https://stream.example.com/secret-path", "MP3", 128, country, "https://icon.example.com/i.png"
    )


class RadioApiCase(DataApiCase):
    def make_pool(self):
        pool = super().make_pool()
        favorites = self.world.favorites
        self.radio = install(pool)
        self.radio.user_favorites = favorites
        world = self.world
        world.channels[CHANNEL] = make_channel(CHANNEL, "Sala DJ", ALFA)
        world.channels[456] = make_channel(456, "Sala 2", OMEGA, manage_messages=False)
        world.channels[789] = make_channel(789, "Sala Ômega", OMEGA)
        world.g100.add_member(int(OTHER_USER), administrator=True)
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID)
        self.radio.directory.stations[OTHER_UUID] = station(OTHER_UUID, "Jazz FM")
        return pool

    def setUp(self):
        super().setUp()
        self.other = self.new_session(OTHER_USER)

    # ------------------------------------------------------------------ o que cada teste prepara

    def premium(self, guild=ALFA):
        self.radio.premium.grant(guild, active=0)

    def seed_live(self, bot=1, guild=ALFA, channel=CHANNEL, **kwargs):
        radio = self.radio.store.seed_live(bot, guild, channel, station=kwargs.pop("station", LIVE_UUID), **kwargs)
        self.radio.service.states[radio.id] = radio_state(title="Rock FM")
        return radio

    def seed_custom(self, bot=1, guild=ALFA, channel=CHANNEL, **kwargs):
        radio = self.radio.store.seed(bot, guild, channel, kind="custom", **kwargs)
        self.radio.service.states[radio.id] = radio_state()
        return radio

    def radio_url(self, radio, suffix=""):
        return f"{BASE}/radios/{radio.id}{suffix}"

    def revoke(self, user=UID, guild=None):
        member = (guild or self.world.g100).members[int(user)]
        member.guild_permissions.administrator = member.guild_permissions.manage_guild = False

    def get(self, path, **extra):
        return self.call("GET", path, **extra)


def keys_of(node):
    """Todo nome de chave de um JSON, em qualquer profundidade."""
    if isinstance(node, dict):
        for name, value in node.items():
            yield name
            yield from keys_of(value)
    elif isinstance(node, list):
        for item in node:
            yield from keys_of(item)


def bodies(*responses):
    return [body_of(response) for response in responses]
