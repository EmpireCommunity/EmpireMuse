"""Textos do player fora das skins (item 21, F2): dicas no idioma de agora, plurais, painel de pedidos e o idle em dois idiomas."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.rules.request_panel import PAUSED, PanelEntry
from empiremuse.utils.music.interactions.request_panel import render_panel
from empiremuse.utils.music.models.player import LavalinkPlayer

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

    @pytest.mark.parametrize(("count", "snippet"), [(1, "mais 1 bot extras"), (4, "mais 4 bots extras")])
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


class TestRequestPanel:
    def test_pt_br_is_todays_text(self):
        embed = panel(PT, *ENTRIES, last_action="alguém fez algo")["embeds"][0]

        assert embed.title == "🎶 Pedidos de música"
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

        assert embed.title == "🎶 Music requests" and "active players" in embed.description
        assert embed.fields[0].value.startswith("-# Playing: ") and "in the queue" in embed.fields[0].value
        assert embed.fields[1].name.endswith("private channel")
        assert data["components"][0].children[0].label == "Request a song"
