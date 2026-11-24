"""KI-27 — caracterização das 15 skins do player (10 normais e 5 estáticas) em `empiremuse/utils/music/skins/`.

Cada skin é carregada com um player falso em vários estados (tocando, pausado, livestream, recomendação automática, fila vazia,
só fila automática, sem dica, com log de comando). Confere o contrato com o Discord, não o desenho: `load` devolve um dicionário
com `embeds` (ou `content`), `components` válidos (ids únicos, até 25 opções por select) e embeds dentro dos limites da API; `setup_features`
liga o modo certo (controlador/estático) no player.
"""

import importlib
import json
import pkgutil
from types import SimpleNamespace
from unittest.mock import MagicMock

import disnake
import pytest

import empiremuse.utils.music.skins.normal_player as normal_pkg
import empiremuse.utils.music.skins.static_player as static_pkg

EMBED_TOTAL_LIMIT = 6000
EMBED_DESCRIPTION_LIMIT = 4096
MAX_EMBEDS = 10
MAX_CONTENT = 2000
MAX_SELECT_OPTIONS = 25


def skin_classes(package):
    classes = []
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and obj.__module__ == module.__name__ and hasattr(obj, "load"):
                classes.append(pytest.param(obj, id=f"{package.__name__.rsplit('.', 1)[-1]}.{info.name}"))
    return classes


NORMAL = skin_classes(normal_pkg)
STATIC = skin_classes(static_pkg)


def make_track(**overrides):
    track = MagicMock()
    values = dict(
        title="Título da música",
        uri="https://exemplo.test/a",
        search_uri="https://exemplo.test/s",
        thumb="https://exemplo.test/t.png",
        duration=185000,
        author="Autor",
        requester=1,
        autoplay=False,
        is_stream=False,
        playlist_name="Lista",
        playlist_url="https://exemplo.test/p",
        info={"sourceName": "youtube", "extra": {}},
        album_name="Álbum",
        album_url="https://exemplo.test/al",
        ytid="dQw4w9WgXcQ",
        single_title="Título da música",
        lyrics=None,
    )
    values.update(overrides)
    for key, value in values.items():
        setattr(track, key, value)
    return track


STATES = {
    "tocando": {},
    "pausado": {"paused": True},
    "livestream": {"current": make_track(is_stream=True, duration=0)},
    "recomendada": {"current": make_track(autoplay=True)},
    "sem_playlist": {"current": make_track(playlist_name=None, album_name=None)},
    "fila_vazia": {"queue": [], "queue_autoplay": []},
    "so_fila_automatica": {"queue": [], "queue_autoplay": [make_track(title=f"Auto {n}") for n in range(5)]},
    "fila_longa": {"queue": [make_track(title=f"Fila {n}", duration=0) for n in range(30)]},
    "sem_dica_nem_log": {"current_hint": None, "command_log": ""},
    "mini_fila_desligada": {"mini_queue_enabled": False},
    "modo_restrito_24_7": {"restrict_mode": True, "keep_connected": True},
    "titulo_enorme": {"current": make_track(title="T" * 400, author="A" * 300)},
}


def make_player(**overrides):
    player = MagicMock()
    values = dict(
        current=make_track(),
        queue=[make_track(title=f"Fila {n}") for n in range(5)],
        queue_autoplay=[make_track(title="Auto")],
        paused=False,
        volume=100,
        loop=False,
        command_log="Alguém pausou a música.",
        command_log_emoji="🔹",
        current_hint="Use /ajuda",
        mini_queue_enabled=True,
        nightcore=False,
        autoplay=False,
        restrict_mode=False,
        keep_connected=False,
        has_thread=False,
        static=False,
        position=1000,
    )
    values.update(overrides)
    for key, value in values.items():
        setattr(player, key, value)
    player.bot.get_color.return_value = 0x5865F2
    player.bot.config = {"HINT_RATE": 3}
    player.__str__ = lambda self: "player"
    return player


def custom_ids(components):
    ids = []
    for component in components:
        rows = component if isinstance(component, (list, tuple)) else [component]
        for item in rows:
            if getattr(item, "custom_id", None):
                ids.append(item.custom_id)
    return ids


def assert_valid_message(data):
    assert isinstance(data, dict) and "embeds" in data
    embeds = data["embeds"]
    # `minimalist` e `embed_link` entregam texto/link em `content` e deixam os embeds vazios
    assert embeds or data.get("content"), "a skin não devolveu embed nem conteúdo"
    assert len(embeds) <= MAX_EMBEDS and all(isinstance(e, disnake.Embed) for e in embeds)
    assert len(data.get("content") or "") <= MAX_CONTENT
    for embed in embeds:
        assert len(embed.description or "") <= EMBED_DESCRIPTION_LIMIT
        assert len(embed) <= EMBED_TOTAL_LIMIT
    assert sum(len(e) for e in embeds) <= EMBED_TOTAL_LIMIT

    components = data.get("components")
    if components:
        ids = custom_ids(components)
        assert len(ids) == len(set(ids)), f"custom_id repetido: {ids}"
        assert all(i.startswith("musicplayer_") or i == "bot_invite" for i in ids if i), ids
        for component in components:
            for item in component if isinstance(component, (list, tuple)) else [component]:
                if isinstance(item, disnake.ui.StringSelect):
                    assert 0 < len(item.options) <= MAX_SELECT_OPTIONS


@pytest.mark.parametrize("state", STATES.values(), ids=STATES.keys())
@pytest.mark.parametrize("skin_cls", NORMAL + STATIC)
def test_every_skin_renders_every_player_state(skin_cls, state):
    data = skin_cls().load(make_player(**state))
    assert_valid_message(data)


@pytest.mark.parametrize("skin_cls", NORMAL)
def test_normal_skins_turn_off_static_mode(skin_cls):
    player = make_player(static=True)
    skin_cls().setup_features(player)
    assert player.static is False


@pytest.mark.parametrize("skin_cls", STATIC)
def test_static_skins_turn_on_static_mode(skin_cls):
    player = make_player(static=False)
    skin_cls().setup_features(player)
    assert player.static is True


NO_LAST_ACTION = {"lite", "micro_nc", "minimalist"}  # skins que nunca mostram a última ação


@pytest.mark.parametrize("skin_cls", NORMAL + STATIC)
def test_a_classic_skin_shows_the_last_action_in_a_public_call(skin_cls):
    """Controle positivo do teste da call privada: na call pública a ação aparece onde a skin a mostra."""
    action = "Bia pulou a música."
    player = make_player(command_log=action)
    player.tr = lambda key, **values: " ".join([key, *map(str, values.values())])
    _call(player, private=False)

    shown = action in _shown_text(skin_cls().load(player))

    assert shown is (skin_cls.__module__.rsplit(".", 1)[-1] not in NO_LAST_ACTION)


def _shown_text(data) -> str:
    embeds = " ".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in data.get("embeds") or [])
    return f"{data.get('content') or ''} {embeds}"


def _call(player, *, private: bool):
    perms = SimpleNamespace(view_channel=not private, connect=True)
    player.guild.me.voice.channel.permissions_for.return_value = perms


@pytest.mark.regression
@pytest.mark.parametrize("skin_cls", NORMAL + STATIC)
def test_no_classic_skin_shows_the_last_action_in_a_private_call(skin_cls):
    action = "Bia pulou a música."
    shown = {}
    for private in (False, True):
        player = make_player(command_log=action)
        player.tr = lambda key, **values: " ".join([key, *map(str, values.values())])
        _call(player, private=private)
        shown[private] = action in _shown_text(skin_cls().load(player))

    assert shown[True] is False


SECRET = "SEGREDO"
SECRET_ID = 987654321
BOT_ID = 111
HIDDEN = "Faixa oculta"


def _secret_track(n=0, **overrides):
    values = dict(
        title=f"{SECRET} faixa {n}",
        single_title=f"{SECRET} faixa {n}",
        uri=f"https://{SECRET.lower()}.test/{n}",
        search_uri=f"https://{SECRET.lower()}.test/s{n}",
        thumb=f"https://{SECRET.lower()}.test/capa{n}.png",
        author=f"{SECRET} artista",
        authors_string=f"{SECRET} artista",
        authors_md=f"[{SECRET} artista](https://{SECRET.lower()}.test/a)",
        requester=SECRET_ID,
        playlist_name=f"{SECRET} lista",
        playlist_url=f"https://{SECRET.lower()}.test/p",
        album_name=f"{SECRET} album",
        album_url=f"https://{SECRET.lower()}.test/al",
        info={"sourceName": "youtube", "extra": {"related": {"uri": f"https://{SECRET.lower()}.test/r"}}},
    )
    return make_track(**(values | overrides))


def _everything_shown(data) -> str:
    parts = [_shown_text(data)]
    for component in data.get("components") or []:
        for item in component if isinstance(component, (list, tuple)) else [component]:
            to_dict = getattr(item, "to_component_dict", None)
            parts.append(json.dumps(to_dict() if callable(to_dict) else str(item), ensure_ascii=False, default=str))
    return " ".join(parts)


def _secret_player(*, private: bool, **overrides):
    from empiremuse.utils.music.private_call import skin_view

    values = dict(
        current=_secret_track(),
        queue=[_secret_track(n) for n in range(1, 6)],
        queue_autoplay=[_secret_track(9, autoplay=True)],
        command_log=f"<@{SECRET_ID}> pulou {SECRET}",
        static=False,
        skin="default",
    )
    player = make_player(**(values | overrides))
    player.tr = lambda key, **values: (
        HIDDEN if key == "jbx.hidden_track" else " ".join([key, *map(str, values.values())])
    )
    player.bot.user.id = BOT_ID
    _call(player, private=private)
    return player, skin_view(player)


@pytest.mark.regression
@pytest.mark.critical
@pytest.mark.parametrize("state", ["tocando", "recomendada", "mini_fila_desligada"])
@pytest.mark.parametrize("skin_cls", NORMAL + STATIC)
def test_no_classic_skin_shows_the_track_cover_or_requester_in_a_private_call(skin_cls, state):
    extra = {
        "recomendada": {"current": _secret_track(autoplay=True)},
        "mini_fila_desligada": {"mini_queue_enabled": False},
    }
    _player, view = _secret_player(private=True, **extra.get(state, {}))
    public, _ = _secret_player(private=False, **extra.get(state, {}))

    shown = _everything_shown(skin_cls().load(view))
    title_on_public = f"{SECRET} faixa 0" in _everything_shown(skin_cls().load(public))

    assert SECRET not in shown and SECRET.lower() not in shown and str(SECRET_ID) not in shown
    assert f"<@{BOT_ID}>" not in shown  # ninguém no lugar de quem pediu
    assert "musicplayer_queue_dropdown" not in shown  # nem o select da fila, como nas skins do EmpireMuse
    assert (HIDDEN in shown) is title_on_public  # onde a skin mostra o título, mostra o título oculto


@pytest.mark.parametrize("skin_cls", NORMAL + STATIC)
def test_a_classic_skin_in_a_public_call_is_unchanged(skin_cls):
    player, view = _secret_player(private=False)

    assert view is player and SECRET in _everything_shown(skin_cls().load(view))


@pytest.mark.regression
@pytest.mark.parametrize("skin", ["default", "muse_text"])
def test_both_load_paths_hand_the_masked_view_only_to_a_classic_skin_in_a_private_call(skin):
    from empiremuse.utils.music.models import np_steps
    from empiremuse.utils.music.models.player import LavalinkPlayer
    from empiremuse.utils.music.private_call import PrivateCallView

    seen = []
    loaded = SimpleNamespace(load=lambda p: seen.append(p) or {"embeds": [disnake.Embed(description="x")]})
    player, _ = _secret_player(private=True, skin=skin)
    player.bot.pool.player_skins = {skin: loaded}

    np_steps._load_skin(player)
    LavalinkPlayer.get_skin(player)

    assert [isinstance(p, PrivateCallView) for p in seen] == [skin == "default"] * 2


@pytest.mark.regression
@pytest.mark.parametrize("private", [False, True])
def test_the_default_extra_menu_has_no_queue_select_nor_lyrics_in_a_private_call(private):
    from empiremuse.utils.music.models import np_steps

    player, _ = _secret_player(private=private, static=True, mini_queue_feature=False, has_thread=False)
    player.node.lyric_support = True
    menu = SimpleNamespace(options=[])
    req = SimpleNamespace(data={"components": [None] * 5 + [menu]})

    np_steps._np_buttons_extra(player, req)

    shown = _everything_shown({"components": req.data["components"][6:]}) + str([o.value for o in menu.options])
    assert (SECRET in shown, "musicplayer_lyrics" in shown) == (not private, not private)
