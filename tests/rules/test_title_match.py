"""Regra composta de casamento de títulos (``empiremuse.rules.title_match``): texto completo E título sozinho.

A amostra de pares vem de ``.local/fuzz-check-title.md`` (pares realistas medidos com ``rapidfuzz``). O medidor real
(``token_set_ratio``) é usado nos casos de comportamento; um medidor falso isola a composição.
"""

import pytest
from rapidfuzz import fuzz

from empiremuse.rules.title_match import TITLE_ONLY_MIN_SCORE, title_matches

pytestmark = pytest.mark.critical


def casa(busca: str, candidata_artista: str, candidata_titulo: str, limiar: float) -> bool:
    """Como Spotify/Deezer: ``busca`` (texto do usuário) contra ``artista - título`` da candidata."""
    return title_matches(
        fuzz.token_set_ratio,
        f"{candidata_artista} - {candidata_titulo}".lower(),
        busca.lower(),
        candidata_titulo.lower(),
        busca.lower(),
        limiar,
    )


class TestComposicao:
    @pytest.mark.smoke
    def test_exige_as_duas_notas(self):
        notas = {("completo", "busca"): 90, ("titulo", "busca"): 90}
        medidor = lambda a, b: notas[(a, b)]  # noqa: E731

        assert title_matches(medidor, "completo", "busca", "titulo", "busca", 75)

    def test_texto_completo_abaixo_do_limiar_recusa(self):
        medidor = lambda a, b: 74 if a == "completo" else 100  # noqa: E731

        assert not title_matches(medidor, "completo", "busca", "titulo", "busca", 75)

    def test_titulo_abaixo_de_60_recusa_mesmo_com_texto_completo_alto(self):
        medidor = lambda a, b: 100 if a == "completo" else TITLE_ONLY_MIN_SCORE - 0.1  # noqa: E731

        assert not title_matches(medidor, "completo", "busca", "titulo", "busca", 75)


POSITIVOS = [
    ("queen - bohemian rhapsody", "queen", "bohemian rhapsody"),
    ("bohemian rhapsody - queen", "queen", "bohemian rhapsody"),
    ("linkin park - numb (official music video)", "linkin park", "numb"),
    (
        "rick astley - never gonna give you up (official video) (4k remaster) [hd]",
        "rick astley",
        "never gonna give you up",
    ),
    ("nirvana - smells like teen spirit", "nirvana", "smells like teen spirit - remastered 2021"),
    ("daft punk - get lucky", "daft punk", "get lucky (feat. pharrell williams & nile rodgers)"),
    ("bohemian rhapsody", "queen", "bohemian rhapsody"),
]

NEGATIVOS = [
    ("linkin park - in the end", "linkin park", "crawling"),
    ("system of a down - toxicity", "system of a down", "aerials"),
    ("queen - love of my life", "queen", "somebody to love"),
    ("metallica - one", "metallica", "the unforgiven ii"),
    ("the weeknd - save your tears", "the weeknd", "blinding lights"),
    ("iron maiden - fear of the dark", "taylor swift", "shake it off"),
]


@pytest.mark.parametrize(("busca", "artista", "titulo"), list(POSITIVOS)[:2])
def test_faixas_certas_passam_com_o_limiar_75(busca, artista, titulo):
    assert casa(busca, artista, titulo, 75)


@pytest.mark.parametrize(("busca", "artista", "titulo"), list(NEGATIVOS)[:2])
def test_outra_musica_do_mesmo_artista_ou_sem_relacao_nao_passa(busca, artista, titulo):
    assert not casa(busca, artista, titulo, 75)


class TestSearchText:
    """ERR-65FEA7 (a): a consulta ``ytsearch:"título - autor"`` leva aspas; comparadas com o título do resultado, nada passava."""

    @pytest.mark.parametrize(
        ("query", "candidate", "title"),
        [
            ('ytsearch:"Сказка - IC3PEAK"', "сказка - ic3peak", "сказка"),
            ('ytsearch:"права - ooes"', "права - ooes", "права"),
        ],
    )
    def test_the_right_youtube_result_is_accepted_for_a_quoted_query(self, query, candidate, title):
        from rapidfuzz import fuzz

        from empiremuse.rules.title_match import search_text, title_matches

        wanted = search_text(query)

        assert wanted == candidate
        assert title_matches(fuzz.token_set_ratio, candidate, wanted, title, wanted, 60)

    def test_without_a_provider_prefix_or_quotes_it_is_just_normalized(self):
        from empiremuse.rules.title_match import search_text

        assert search_text('scsearch:  "A  -  B" ') == "a - b" and search_text("Plain Text") == "plain text"
