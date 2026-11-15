"""2.1.2 — regras puras do status automático do canal de voz (``empiremuse/rules/voice_status.py``, contrato I2).

VR1 ``resolve_voice_status`` (V-S4: nunca levanta, padrão ligado); VR2 ``fit_voice_status`` (I-S2: 500 unidades UTF-16, sem partir
``<a?:n:id>``); VR3 ``render_voice_status`` + ``voice_status_value`` (V-S1/I-S4: o valor inserido nunca é reprocessado nem vira menção,
emoji, link ou formatação). O módulo é importado dentro de cada teste: sem ele, cada um falha pelo próprio ``import``.
"""

import importlib
import re
from collections.abc import Mapping
from types import ModuleType

import pytest

pytestmark = pytest.mark.critical

DEFAULT_TEMPLATE = "{track.emoji} Tocando: {track.title} - {track.author}"
UNIDADES_MAXIMAS = 500
TOKEN_COMPLETO = re.compile(r"<a?:\w+:\d+>")


def regras() -> ModuleType:
    return importlib.import_module("empiremuse.rules.voice_status")


def unidades(texto: str) -> int:
    """Tamanho em unidades UTF-16, como o Discord conta (emoji astral = 2)."""
    return len(texto.encode("utf-16-le")) // 2


# ============================================================================ constantes


# ============================================================================ VR1


class TestResolveVoiceStatus:
    def test_an_old_document_without_the_keys_is_on_with_the_default_template(self) -> None:
        cfg = regras().resolve_voice_status({})

        assert cfg.permanent is True and cfg.template == DEFAULT_TEMPLATE

    @pytest.mark.parametrize("salvo", [True, False])
    def test_a_saved_bool_wins(self, salvo: bool) -> None:
        assert regras().resolve_voice_status({"voice_status_permanent": salvo}).permanent is salvo

    @pytest.mark.parametrize(
        "lixo", [None, "false", "False", "", 0, 1, 0.0, [], [True], {}, {"a": 1}, b"", b"\x00", "x" * 1_000_000]
    )
    def test_a_saved_value_that_is_not_a_bool_means_on(self, lixo: object) -> None:
        assert regras().resolve_voice_status({"voice_status_permanent": lixo}).permanent is True

    def test_a_valid_saved_template_is_used_as_it_is(self) -> None:
        cfg = regras().resolve_voice_status({"voice_channel_status": "Ouvindo {track.title}"})

        assert cfg.template == "Ouvindo {track.title}"

    @pytest.mark.parametrize(
        "lixo",
        [
            "",
            "   ",
            "Tocando agora",  # sem placeholder
            "{foo}",
            "‮{track.title}",  # Cf
            "{track.title}\x00",  # Cc
            "{track.title}" + "x" * 600,  # além do limite
            "x" * 1_000_000,
            None,
            0,
            1,
            [],
            ["{track.title}"],
            {},
            b"{track.title}",
            True,
        ],
    )
    def test_an_empty_invalid_or_non_text_template_means_the_default(self, lixo: object) -> None:
        assert regras().resolve_voice_status({"voice_channel_status": lixo}).template == DEFAULT_TEMPLATE

    def test_it_never_raises_whatever_the_document_holds(self) -> None:
        valores = [None, True, 1, "x", b"x", [], {}, "x" * 100_000, object()]
        for permanente in valores:
            for modelo in valores:
                cfg = regras().resolve_voice_status(
                    {"voice_status_permanent": permanente, "voice_channel_status": modelo}
                )
                assert isinstance(cfg.permanent, bool) and isinstance(cfg.template, str) and cfg.template


# ============================================================================ VR2


class TestFitVoiceStatus:
    def test_a_text_exactly_at_the_limit_is_not_cut(self) -> None:
        texto = "a" * UNIDADES_MAXIMAS

        assert regras().fit_voice_status(texto) == texto

    def test_one_unit_over_is_cut_and_ends_with_dots_within_the_limit(self) -> None:
        saida = regras().fit_voice_status("a" * (UNIDADES_MAXIMAS + 1))

        assert saida.endswith("...") and unidades(saida) <= UNIDADES_MAXIMAS and saida.startswith("a" * 400)

    def test_a_huge_text_never_passes_the_limit(self) -> None:
        saida = regras().fit_voice_status("é" * 100_000)

        assert unidades(saida) <= UNIDADES_MAXIMAS and saida.endswith("...")

    def test_an_astral_emoji_counts_as_two_units(self) -> None:
        assert regras().fit_voice_status("😀" * 250) == "😀" * 250  # 500 unidades: cabe

        saida = regras().fit_voice_status("😀" * 251)  # 502: corta

        assert unidades(saida) <= UNIDADES_MAXIMAS and saida.endswith("...")
        saida.encode("utf-16-le")  # nenhum meio-par de surrogates

    def test_the_cut_never_splits_an_astral_character(self) -> None:
        saida = regras().fit_voice_status("a" + "😀" * 400)

        assert "\ud83d" not in saida and set(saida.removesuffix("...")) <= {"a", "😀"}
        assert unidades(saida) <= UNIDADES_MAXIMAS

    @pytest.mark.parametrize(
        "token", ["<:abcdefgh:123456789012345678>", "<a:abcdefgh:123456789012345678>", "<:a:1>", "<a:a:1>"]
    )
    @pytest.mark.parametrize("folga", range(0, 24, 2))
    def test_the_cut_never_splits_a_custom_emoji(self, token: str, folga: int) -> None:
        texto = "x" * (UNIDADES_MAXIMAS - folga) + token + "y" * 60

        saida = regras().fit_voice_status(texto)

        assert unidades(saida) <= UNIDADES_MAXIMAS and saida.endswith("...")
        assert "<" not in TOKEN_COMPLETO.sub("", saida), f"emoji partido: {saida[-60:]!r}"

    def test_a_custom_emoji_that_fits_whole_stays_whole(self) -> None:
        texto = "<:spotify:123456789012345678> " + "t" * 600

        saida = regras().fit_voice_status(texto)

        assert saida.startswith("<:spotify:123456789012345678> ") and unidades(saida) <= UNIDADES_MAXIMAS


# ============================================================================ VR3


class TestVoiceStatusValue:
    @pytest.mark.parametrize("nao_texto", [None, 5, 1.5, b"abc", ["a"], {"a": 1}, object()])
    def test_a_non_text_becomes_empty(self, nao_texto: object) -> None:
        assert regras().voice_status_value(nao_texto) == ""

    @pytest.mark.parametrize("controle", ["\x00", "\x1b", "\x7f", "‮", "​", "⁦", "\r", "\n", "\t"])
    def test_control_format_and_line_break_characters_are_removed(self, controle: str) -> None:
        saida = regras().voice_status_value(f"a{controle}b")

        assert controle not in saida and saida.replace(" ", "") == "ab"

    @pytest.mark.parametrize(
        "token", ["<@123456789012345678>", "<@!123456789012345678>", "<@&123456789012345678>", "<#123456789012345678>"]
    )
    def test_mentions_become_inert(self, token: str) -> None:
        saida = regras().voice_status_value(f"oi {token} tchau")

        assert token not in saida and "<" not in saida and "@" not in saida and "‹" in saida
        assert ("＠" in saida) == ("@" in token)

    @pytest.mark.parametrize("token", ["<:x:1>", "<a:x:1>", "</cmd:123>", "<t:1700000000:R>"])
    def test_emoji_commands_and_timestamps_become_inert(self, token: str) -> None:
        saida = regras().voice_status_value(f"{token}")

        assert token not in saida and "<" not in saida and "‹" in saida

    @pytest.mark.parametrize("alvo", ["@everyone", "@here"])
    def test_everyone_and_here_become_inert(self, alvo: str) -> None:
        saida = regras().voice_status_value(f"aviso {alvo} agora")

        assert alvo not in saida and "@" not in saida and f"＠{alvo[1:]}" in saida

    def test_markdown_is_escaped(self) -> None:
        saida = regras().voice_status_value("**forte** _it_ ~~r~~ ||s|| `c` [t](u)")

        assert "\\*\\*forte\\*\\*" in saida and "\\_it\\_" in saida and "\\[t](u)" in saida

    @pytest.mark.parametrize("url", ["https://discord.gift/abc", "http://exemplo.invalid/x", "ftp://exemplo.invalid/y"])
    def test_a_bare_url_loses_its_double_slash(self, url: str) -> None:
        saida = regras().voice_status_value(f"veja {url} ok")

        assert "//" not in saida and "／／" in saida

    def test_the_zero_width_joiner_trick_is_not_used(self) -> None:
        assert "⁠" not in regras().voice_status_value("<@123456789012345678> @everyone")


class TestRenderVoiceStatus:
    @staticmethod
    def valores(**sobrepor: str) -> Mapping[str, str]:
        base = {
            "{track.title}": "Musica",
            "{track.author}": "Artista",
            "{track.playlist}": "Playlist",
            "{requester.name}": "Fulano",
            "{track.source}": "spotify",
            "{track.emoji}": "<:spotify:123456789012345678>",
            "{track.timestamp}": "<t:1700000000:R>",
            "{track.duration}": "3:20",
            "{requester.id}": "42",
        }
        return base | sobrepor

    def test_all_placeholders_are_replaced_in_one_pass(self) -> None:
        r = regras()
        modelo = " | ".join(
            [
                "{track.emoji}",
                "{track.title}",
                "{track.author}",
                "{track.duration}",
                "{track.source}",
                "{track.playlist}",
                "{requester.name}",
                "{requester.id}",
                "{track.timestamp}",
            ]
        )

        saida = r.render_voice_status(modelo, self.valores())

        assert (
            saida
            == "<:spotify:123456789012345678> | Musica | Artista | 3:20 | spotify | Playlist | Fulano | 42 | <t:1700000000:R>"
        )

    def test_a_title_that_looks_like_a_placeholder_comes_out_literal(self) -> None:
        r = regras()
        titulo = r.voice_status_value("{requester.id}")

        saida = r.render_voice_status("{track.title} por {requester.id}", self.valores(**{"{track.title}": titulo}))

        assert saida == "{requester.id} por 42"

    @pytest.mark.parametrize(
        "escondido", ["{track.author}", "{requester.name}", "{track.playlist}", "{track.emoji}", "{track.title}"]
    )
    def test_no_inserted_value_is_expanded_again(self, escondido: str) -> None:
        r = regras()
        valores = self.valores(**{"{track.title}": r.voice_status_value(f"A{escondido}B")})

        saida = r.render_voice_status("[{track.title}]", valores)

        assert saida == f"[A{escondido}B]"

    def test_the_author_cannot_reach_a_value_that_comes_later_in_the_template(self) -> None:
        r = regras()
        valores = self.valores(**{"{track.author}": r.voice_status_value("{requester.name}{requester.id}")})

        assert r.render_voice_status("{track.author}", valores) == "{requester.name}{requester.id}"

    def test_the_bot_emoji_stays_but_a_title_emoji_does_not(self) -> None:
        r = regras()
        valores = self.valores(**{"{track.title}": r.voice_status_value("<:x:1> <a:y:2> <@&1> @everyone [t](u) ‮")})

        saida = r.render_voice_status("{track.emoji} {track.title}", valores)

        assert saida.startswith("<:spotify:123456789012345678> ")
        titulo = saida.removeprefix("<:spotify:123456789012345678> ")
        assert "<" not in titulo and "@" not in titulo and "‮" not in titulo and "\\[t](u)" in titulo


# ============================================================================ V-145-4 — todo "<" e todo "@" do valor externo ficam inertes


class TestNeutralizacaoIncondicional:
    def test_every_angle_bracket_and_at_sign_of_the_value_is_swapped_without_looking_for_a_token(self) -> None:
        assert regras().voice_status_value("a<b@c") == "a‹b＠c"

    @pytest.mark.parametrize("solto", ["<", "@", "x<", "@&123>", "x@", "everyone", "a < b", "mail@host"])
    def test_loose_pieces_never_keep_the_ascii_characters(self, solto: str) -> None:
        saida = regras().voice_status_value(solto)

        assert "<" not in saida and "@" not in saida

    def test_markdown_is_still_escaped_next_to_the_swapped_characters(self) -> None:
        saida = regras().voice_status_value("**a**<_b_@")

        assert saida == "\\*\\*a\\*\\*‹\\_b\\_＠"

    @pytest.mark.parametrize(
        ("modelo", "titulo", "autor"),
        [
            (
                "{track.title}:{track.author}",
                "x<a",
                "b:123456789012345678>",
            ),  # '<a:b:id>' (emoji animado) colado pelo modelo
            ("{track.title}{track.author}", "x<", "@&123456789012345678>"),  # '<@&id>' (menção de cargo) colada
            ("{track.title}{track.author}", "x@", "everyone"),  # '@everyone' colado
            ("{track.title}{track.author}", "x@", "here"),
            ("{track.title}{track.author}", "x<", "#123456789012345678>"),  # '<#id>' (canal) colado
            ("{track.title}{track.author}", "x</", "cmd:123456789012345678>"),  # '</cmd:id>' colado
            ("{track.title}{track.author}", "x<t", ":1700000000:R>"),  # '<t:…:R>' colado
        ],
    )
    def test_a_template_that_glues_two_values_cannot_rebuild_a_token(
        self, modelo: str, titulo: str, autor: str
    ) -> None:
        r = regras()
        valores = {"{track.title}": r.voice_status_value(titulo), "{track.author}": r.voice_status_value(autor)}

        saida = r.render_voice_status(modelo, valores)

        assert "<" not in saida and "@" not in saida, saida


class TestUrlColadaPeloModelo:
    @pytest.mark.parametrize(("titulo", "autor"), [("https:", "//evil.example/x"), ("ftp:", "//h")])
    def test_a_template_that_glues_two_values_cannot_form_a_bare_url(self, titulo: str, autor: str) -> None:
        r = regras()
        valores = {"{track.title}": r.voice_status_value(titulo), "{track.author}": r.voice_status_value(autor)}

        saida = r.render_voice_status("{track.title}{track.author}", valores)

        assert "//" not in saida, saida


@pytest.mark.regression
@pytest.mark.parametrize(
    ("legacy", "clean"),
    [("Tocando: {track.title}", "Tocando: {track.title}"), ("{track.title}\ud800", "{track.title}")],
)
def test_a_template_stored_before_the_stricter_text_rule_is_read_cleaned_and_the_form_saves_it_cleaned(legacy, clean):
    """Revisão do PR 255: o modelo gravado com U+2028 ou surrogate isolado virava o padrão na leitura, e o formulário, reenviado para mudar
    só o "permanente", o regravava vazio. Agora a leitura o limpa e o formulário grava o limpo."""
    from empiremuse.panel.services.general import VoiceStatusForm

    stored = {"voice_status_permanent": True, "voice_channel_status": legacy}
    assert regras().resolve_voice_status(stored).template == clean

    form = VoiceStatusForm()
    shown = form.extract(stored)
    parsed = form.parse(None, shown, {"voice_channel_status": [shown["voice_channel_status"]]})
    assert shown["voice_channel_status"] == clean and parsed.new["voice_channel_status"] == clean
