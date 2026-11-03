"""Textos do player fora das skins (item 21, F2): dicas no idioma de agora, plurais, painel de pedidos e o idle em dois idiomas."""

import ast
import importlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.rules import i18n as rules
from empiremuse.rules.jukebox_panel import PAUSED, PanelEntry
from empiremuse.utils.music.interactions.jukebox_panel import render_panel
from empiremuse.utils.music.models.player import LavalinkPlayer
from tests.i18n.skins_harness import make_player

pytestmark = pytest.mark.critical

PT = get_i18n().for_locale("pt-BR")
EN = get_i18n().for_locale("en-US")


def player_with(tr, *, lastfm=True):
    player = object.__new__(LavalinkPlayer)
    player.tr = tr
    player.prefix_info = "!!"
    player._hint_platforms = ["youtube, soundcloud", "deezer"]
    player._hint_lastfm = lastfm
    player._extra_hints = ["dica extra"]
    return player


class TestHints:
    def test_the_fixed_hints_follow_the_translator_of_now(self):
        player = player_with(PT)

        assert "/change_skin ou !!skin" in player.initial_hints[0]
        assert any("youtube, soundcloud e deezer" in hint for hint in player.initial_hints)

        player.tr = EN

        assert "/change_skin command or !!skin" in player.initial_hints[0]
        assert any("youtube, soundcloud and deezer" in hint for hint in player.initial_hints)
        assert player.initial_hints[-1] == "dica extra"

    def test_lastfm_hint_only_when_configured(self):
        assert len(player_with(PT, lastfm=False).initial_hints) == len(player_with(PT).initial_hints) - 1


class TestPluralsKeepTodaysPortuguese:
    @pytest.mark.parametrize(
        ("count", "snippet"),
        [(1, "há mais 1 bot no servidor que funciona com"), (3, "há mais 3 bots no servidor que funcionam com")],
    )
    def test_the_bots_hint(self, count, snippet):
        assert snippet in PT("player.hint.bots_in_guild", count=count, prefix_info="!!")

    @pytest.mark.parametrize(
        ("count", "snippet"), [(1, "mais 1 bot extra no servidor"), (4, "mais 4 bots extras no servidor")]
    )
    def test_the_outside_bots_hint(self, count, snippet):
        assert snippet in PT("player.hint.bots_outside_guild", count=count, prefix_info="!!")

    def test_the_ansi_tail_of_the_queue_is_byte_for_byte_the_old_line(self):
        text = PT(
            "player.queue.and_more_ansi",
            count=20,
            ansi_0_37="\x1b[0;37m",
            ansi_0="\x1b[0m",
            ansi_0_35="\x1b[0;35m",
        )

        assert text == "\n\n\x1b[0;37mE mais\x1b[0m \x1b[0;35m20\x1b[0m \x1b[0;37mmúsicas.\x1b[0m"
        assert "more songs." in EN("player.queue.and_more_ansi", count=20, ansi_0_37="", ansi_0="", ansi_0_35="")


def panel(tr, *entries, last_action=""):
    bot = MagicMock()
    bot.get_color.return_value = 0
    bot.app_emojis = {}
    bot.pool.emoji_data = {}
    guild = MagicMock()
    guild.me.display_avatar.replace.return_value.url = "http://a"
    return render_panel(bot, guild, entries, tr=tr, last_action=last_action)


ENTRIES = (
    PanelEntry("Bot A", 1, "Call A", False, "Faixa", "http://x", "playing", 1),
    PanelEntry("Bot B", 2, "Call B", True, None, None, PAUSED, 0),
)


class TestJukeboxPanel:
    def test_pt_br_is_todays_text(self):
        embed = panel(PT, *ENTRIES, last_action="alguém fez algo")["embeds"][0]

        assert embed.title == "🎶 Jukebox"
        assert embed.description == (
            "Entre em um canal de voz e peça uma música aqui no canal (ou pelo botão abaixo). "
            "**2 players ativos** neste servidor:"
        )
        assert embed.fields[0].name == "▶️ Bot A · 🔊 Call A" and embed.fields[0].value.startswith("-# Tocando: ")
        assert "📑 1 na fila" in embed.fields[0].value
        assert embed.fields[1].name == "⏸️ Bot B · 🔊 canal privado"
        assert embed.fields[1].value == "-# 🔒 A call é privada: a faixa não aparece aqui."
        assert (
            embed.footer.text
            == "Última ação: alguém fez algo\nOs botões e o select agem no player da call em que você está."
        )

    def test_en_us_is_translated_and_the_buttons_fit(self):
        data = panel(EN, *ENTRIES)
        embed = data["embeds"][0]

        assert embed.title == "🎶 Jukebox" and "active players" in embed.description
        assert embed.fields[0].value.startswith("-# Playing: ") and "in the queue" in embed.fields[0].value
        assert embed.fields[1].name.endswith("private channel")
        assert data["components"][0].children[0].label == "Request a song"


class TestReviewRound175:
    def test_the_hints_have_no_leftover_marker_and_agree_in_number(self):
        for text in get_i18n().entries("pt-BR").values():
            assert "funciona(m)" not in text
        one = PT("player.hint.bots_outside_guild", count=1, prefix_info="!!")
        many = PT("player.hint.bots_outside_guild", count=3, prefix_info="!!")

        assert "que funciona com" in one and "que funcionam com" in many

    def test_the_reconnect_failure_message_has_no_placeholder_and_is_the_fixed_text(self):
        key = "player.a_failure_occurred_while_reconnecting_2"
        for locale in ("pt-BR", "en-US"):
            assert not re.search(r"\{\w+\}", get_i18n().entries(locale)[key]), locale
        assert PT(key) == "Ocorreu uma falha ao reconectar o player no canal de voz."
        assert EN(key).endswith("to the voice channel.")

    def test_no_exception_caught_in_the_code_is_handed_to_a_translation_call(self):
        offenders = []
        root = Path(__file__).resolve().parents[2] / "empiremuse"
        for path in root.rglob("*.py"):
            for handler in (
                n
                for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(n, ast.ExceptHandler) and n.name
            ):
                for call in (n for stmt in handler.body for n in ast.walk(stmt) if isinstance(n, ast.Call)):
                    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "tr"):
                        continue
                    used = {n.id for kw in call.keywords for n in ast.walk(kw.value) if isinstance(n, ast.Name)}
                    if handler.name in used:
                        offenders.append(f"{path.name}:{call.lineno}")
        assert not offenders, offenders

    def test_the_spelling_fixes_are_in(self):
        flat = " ".join(get_i18n().entries("pt-BR").values())
        for wrong in (
            "plater",
            "perca de",
            "músca",
            "excluido",
            "oscioso",
            "ne conexão",
            "IFORMAÇÕES",
            "devido a falta",
            "playlists pública",
        ):
            assert not re.search(rf"(?<!\w){re.escape(wrong)}(?!\w)", flat), wrong

    def test_the_classic_skin_counts_only_the_tracks_it_does_not_show(self):
        player = make_player(PT, queue=20)
        classic = importlib.import_module("empiremuse.utils.music.skins.static_player.classic")

        content = classic.load().load(player)["content"]

        assert "\x1b[0;35m5\x1b[0m" in content and "\x1b[0;35m20\x1b[0m" not in content
