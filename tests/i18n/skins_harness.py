"""Renderiza todas as skins com um player simulado e devolve um dicionário comparável (item 21: o pt-BR é o texto de hoje, palavra por palavra)."""

import datetime
import importlib
import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import disnake

SKINS = (
    "static_player.default",
    "static_player.default_progressbar",
    "static_player.classic",
    "static_player.mini",
    "static_player.embed_link",
    "normal_player.default",
    "normal_player.default_progressbar",
    "normal_player.classic",
    "normal_player.mini",
    "normal_player.embed_link",
    "normal_player.lite",
    "normal_player.minimalist",
    "normal_player.micro_nc",
    "normal_player.micro_controller",
    "normal_player.miniplayer",
)
NOW = datetime.datetime(2026, 11, 1, 12, 0, tzinfo=datetime.timezone.utc)
VARIANTS = {
    "playing": {},
    "paused": {"paused": True},
    "live": {"stream": True},
    "loop_current": {"loop": "current", "track_loops": 2},
    "loop_queue": {"loop": "queue"},
    "autoplay": {"autoplay_track": True, "hint": ""},
    "idle_queue": {"queue": 0, "queue_autoplay": 3},
    "keep": {"keep_connected": True, "log": ""},
}


def make_player(tr, **opts):
    track = lambda n, **kw: SimpleNamespace(  # noqa: E731
        duration=0 if kw.get("stream") else 180000 + n * 1000,
        is_stream=bool(kw.get("stream")),
        track_loops=kw.get("loops", 0),
        requester=42,
        autoplay=False,
        title=f"Faixa {n}",
        uri=f"http://x/{n}",
        search_uri=f"http://x/{n}",
        authors_string="Artista",
        authors_md="[Artista](http://a)",
        author="Artista",
        single_title=f"Faixa {n}",
        thumb=None,
        album_name="Álbum",
        album_url="http://al",
        playlist_name="Lista",
        playlist_url="http://pl",
        info={"sourceName": "youtube", "extra": {"related": {"uri": "http://r"}}},
        emoji="🎵",
        unique_id=f"id{n}",
        stream_title=None,
        ytid="abc123",
    )
    current = track(0, stream=opts.get("stream"), loops=opts.get("track_loops", 0))
    current.autoplay = bool(opts.get("autoplay_track"))
    bot = MagicMock()
    bot.get_color.return_value = 0x112233
    bot.pool.emoji_data = {}
    bot.app_emojis = {}
    bot.config = {"IDLE_TIMEOUT": 180}
    bot.user.display_avatar.replace.return_value.url = "http://avatar"
    player = MagicMock()
    player.bot = bot
    player.tr = tr
    player.current = current
    player.position = 30000
    player.paused = bool(opts.get("paused"))
    player.loop = opts.get("loop", False)
    player.keep_connected = bool(opts.get("keep_connected"))
    player.volume = 55
    player.nightcore = False
    player.autoplay = False
    player.restrict_mode = False
    player.queue = [track(n) for n in range(1, 1 + opts.get("queue", 5))]
    player.queue_autoplay = [track(n) for n in range(10, 10 + opts.get("queue_autoplay", 0))]
    player.played = [track(90), track(91)]
    player.current_hint = opts.get("hint", "uma dica")
    player.command_log = opts.get("log", "alguém fez algo")
    player.command_log_emoji = "▶️"
    player.guild.me.voice.channel.mention = "<#9>"
    player.guild.me.voice.channel.jump_url = "http://v"
    player.guild.me.display_avatar.replace.return_value.url = "http://a"
    player.guild.me.display_avatar.url = "http://a"
    player.last_data = {}
    player.static = False
    player.listen_along_invite = None
    player.controller_mode = True
    player.lyric_embed = None
    player.stage_title_event = False
    player.is_closing = False
    player.node.lyric_support = True
    player.lyric_embed = None
    return player


def component_dict(component):
    return component.to_component_dict() if hasattr(component, "to_component_dict") else str(component)


def render(tr):
    """``{skin: {variante: dados}}``; ``dados`` tem só texto e componentes (nada de objetos)."""
    out = {}
    with patch("disnake.utils.utcnow", lambda: NOW):
        for skin in SKINS:
            module = importlib.import_module(f"empiremuse.utils.music.skins.{skin}")
            out[skin] = {}
            for name, opts in VARIANTS.items():
                player = make_player(tr, **opts)
                try:
                    data = module.load().load(player)
                except Exception as error:  # noqa: BLE001 - o erro também é parte do que se compara
                    out[skin][name] = f"ERRO {type(error).__name__}"
                    continue
                entry = {}
                for key, value in data.items():
                    if key == "embeds":
                        entry[key] = [e.to_dict() for e in value]
                    elif key == "embed":
                        entry[key] = value.to_dict()
                    elif key == "components":
                        entry[key] = [component_dict(c) for c in value]
                    elif isinstance(value, (str, int, float, bool, type(None))):
                        entry[key] = value
                out[skin][name] = entry
    text = json.dumps(out, ensure_ascii=False, default=str, sort_keys=True)
    return json.loads(re.sub(r"<MagicMock id='\d+'>", "<MagicMock>", text))
