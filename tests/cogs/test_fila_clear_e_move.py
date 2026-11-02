"""``/queue clear`` e ``/queue move``: os filtros decidem quais faixas saem da fila ou mudam de posição.

Roda os comandos de verdade (``Music.clear.callback`` e ``Music.do_move``, com as etapas de ``queue_clear_steps`` e
``queue_move_steps``) sobre uma fila de faixas falsas; Discord e player são dublês simples.
"""

import asyncio
from collections import deque
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs.music import Music
from empiremuse.i18n.service import get_i18n
from empiremuse.utils.music.errors import GenericError

GUILD = 700
run = asyncio.run

FILTROS = dict(
    song_name=None,
    song_author=None,
    user=None,
    duplicates=False,
    playlist=None,
    min_duration=None,
    max_duration=None,
    amount=None,
    range_start=0,
    range_end=0,
    absent_members=False,
)


class Faixa:
    def __init__(self, title: str, author: str = "Artista", requester: int = 1, duration: int = 180_000) -> None:
        self.title, self.author, self.requester, self.duration = title, author, requester, duration
        self.uri = f"https://exemplo.invalid/{title.replace(' ', '-')}"
        self.search_uri = None
        self.thumb = None
        self.playlist_name = ""
        self.playlist_url = ""


def titulos(player: Any) -> list[str]:
    return [t.title for t in player.queue]


def cenario(*faixas: Faixa) -> tuple[Any, Any, Any]:
    voz = SimpleNamespace(voice_states={1: None, 2: None})  # só os pedidos 1 e 2 ainda estão no canal
    guild = SimpleNamespace(me=SimpleNamespace(voice=SimpleNamespace(channel=voz)))
    player = SimpleNamespace(queue=deque(faixas), queue_autoplay=deque(), guild=guild)
    bot = SimpleNamespace(music=SimpleNamespace(players={GUILD: player}), pool=SimpleNamespace(i18n=get_i18n()))
    inter = SimpleNamespace(
        music_bot=bot,
        bot=SimpleNamespace(pool=SimpleNamespace(i18n=get_i18n())),
        guild=None,
        guild_id=GUILD,
        author=SimpleNamespace(id=9, mention="<@9>"),
        send=AsyncMock(),
    )
    return player, inter, SimpleNamespace(interaction_message=AsyncMock())


def limpar(self_: Any, inter: Any, **filtros: Any) -> list[str] | None:
    run(Music.clear.callback(self_, inter, **{**FILTROS, **filtros}))
    return self_.interaction_message.await_args.args[1] if self_.interaction_message.await_args else None


def mover(self_: Any, inter: Any, **filtros: Any) -> list[str]:
    run(Music.do_move(self_, inter, **filtros))
    return self_.interaction_message.await_args.args[1]


def quatro_faixas() -> tuple[Faixa, ...]:
    return (Faixa("Rock do Bom"), Faixa("Samba Lento"), Faixa("Rock Pesado"), Faixa("Jazz Suave"))


class TestClear:
    def test_sem_filtros_esvazia_a_fila(self) -> None:
        player, inter, self_ = cenario(*quatro_faixas())
        txt = limpar(self_, inter)
        assert not player.queue and txt[0] == "limpou a fila de música."

    def test_nome_remove_so_as_faixas_que_casam_e_relata_o_filtro(self) -> None:
        player, inter, self_ = cenario(*quatro_faixas())
        txt = limpar(self_, inter, song_name="rock")
        assert titulos(player) == ["Samba Lento", "Jazz Suave"]
        assert txt[0] == "removeu 2 músicas da fila via clear."
        assert "Rock do Bom" in txt[1] and "Rock Pesado" in txt[1] and "Inclui nome" in txt[1]

    def test_quantidade_limita_quantas_saem(self) -> None:
        player, inter, self_ = cenario(*quatro_faixas())
        limpar(self_, inter, song_name="rock", amount=1)
        assert titulos(player) == ["Samba Lento", "Rock Pesado", "Jazz Suave"]

    def test_duplicadas_mantem_a_primeira(self) -> None:
        primeira, outra, repetida = Faixa("Rock do Bom"), Faixa("Samba Lento"), Faixa("Rock do Bom")
        player, inter, self_ = cenario(primeira, outra, repetida)
        txt = limpar(self_, inter, duplicates=True)
        assert list(player.queue) == [primeira, outra] and "Músicas duplicadas" in txt[1]

    def test_intervalo_de_posicoes_remove_so_o_trecho(self) -> None:
        faixas = [Faixa(f"Faixa {n}") for n in range(1, 6)]
        player, inter, self_ = cenario(*faixas)
        txt = limpar(self_, inter, range_start=2, range_end=3)
        assert titulos(player) == ["Faixa 1", "Faixa 4", "Faixa 5"] and "Posição inicial da fila" in txt[1]

    def test_membros_que_sairam_do_canal(self) -> None:
        presente, ausente = Faixa("Rock do Bom", requester=1), Faixa("Samba Lento", requester=3)
        player, inter, self_ = cenario(presente, ausente)
        limpar(self_, inter, absent_members=True)
        assert list(player.queue) == [presente]

    def test_duracao_inicial_remove_as_faixas_a_partir_dela(self) -> None:
        curta, longa = Faixa("Curta", duration=60_000), Faixa("Longa", duration=180_000)
        player, inter, self_ = cenario(curta, longa)
        txt = limpar(self_, inter, min_duration="2:00")
        assert list(player.queue) == [curta] and "duração inicial" in txt[1]

    def test_nada_encontrado_avisa_e_nao_mexe_na_fila(self) -> None:
        player, inter, self_ = cenario(*quatro_faixas())
        limpar(self_, inter, song_name="inexistente")
        inter.send.assert_awaited_once_with("Nenhuma música encontrada!", ephemeral=True)
        assert len(player.queue) == 4 and not self_.interaction_message.await_count

    @pytest.mark.parametrize(
        ("fila", "filtros", "trecho"),
        [
            (quatro_faixas, dict(min_duration="1:00", max_duration="2:00"), "apenas uma das opções"),
            (lambda: (), dict(), "Não há músicas na fila"),
        ],
        ids=["duracoes-juntas", "fila-vazia"],
    )
    def test_pedidos_invalidos_sao_recusados(self, fila: Any, filtros: dict[str, Any], trecho: str) -> None:
        _, inter, self_ = cenario(*fila())
        with pytest.raises(GenericError, match=trecho):
            limpar(self_, inter, **filtros)


class TestMove:
    def test_por_nome_leva_as_faixas_para_a_posicao_pedida(self) -> None:
        player, inter, self_ = cenario(*quatro_faixas())
        txt = mover(self_, inter, position=3, song_name="rock")
        assert titulos(player) == ["Samba Lento", "Jazz Suave", "Rock do Bom", "Rock Pesado"]
        assert txt[0] == "moveu 2 músicas para a posição **[3]** da fila." and "Filtro usado" in txt[1]

    def test_sem_nenhuma_opcao_e_recusado(self) -> None:
        _, inter, self_ = cenario(*quatro_faixas())
        with pytest.raises(GenericError, match="pelo menos uma opção"):
            run(Music.do_move(self_, inter))

    def test_sem_resultado_diz_que_nada_foi_encontrado_e_a_fila_fica(self) -> None:
        player, inter, self_ = cenario(*quatro_faixas())
        with pytest.raises(GenericError, match="Nenhuma música encontrada"):
            run(Music.do_move(self_, inter, song_name="inexistente"))
        assert len(player.queue) == 4

    def test_intervalo_com_fim_antes_do_inicio_e_recusado(self) -> None:
        _, inter, self_ = cenario(*quatro_faixas())
        with pytest.raises(GenericError, match="posição final deve ser maior"):
            run(Music.do_move(self_, inter, range_start=3, range_end=2))
