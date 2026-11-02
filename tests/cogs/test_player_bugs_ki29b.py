"""Regressão dos bugs preexistentes do player achados no refactor da KI-29b (um teste por bug).

Cada teste descreve o comportamento esperado; hoje todos falham. Sem rede, sem Discord e sem relógio: o ``sleep`` do
player é trocado por um ``AsyncMock`` e a fila de eventos entrega um único evento e depois encerra o laço.
"""

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import empiremuse.wavelink as wavelink
from empiremuse.i18n.service import get_i18n
from empiremuse.utils.music import models
from empiremuse.utils.music.models import PartialTrack

pytestmark = pytest.mark.regression


def jogador(monkeypatch, **extra):
    """``LavalinkPlayer`` sem o ``__init__`` pesado; as propriedades sem setter viram atributos da subclasse de teste."""

    class JogadorDeTeste(models.LavalinkPlayer):
        is_connected = True

    p = JogadorDeTeste.__new__(JogadorDeTeste)
    canal = SimpleNamespace(send=AsyncMock(), permissions_for=lambda me: SimpleNamespace(send_messages=True), id=5)
    p.bot = SimpleNamespace(
        wait_until_ready=AsyncMock(),
        get_color=lambda *a: 0,
        dispatch=lambda *a, **k: None,
        get_cog=lambda nome: None,
        pool=SimpleNamespace(partial_track_cache={}),
    )
    p.guild = SimpleNamespace(id=9, name="Servidor", me=SimpleNamespace(voice=object()), icon=None)
    p.text_channel = canal
    p.tr = get_i18n().for_locale("pt-BR")
    p.node = SimpleNamespace(identifier="no-1", info={"sourceManagers": []}, search_providers=[], partial_providers=[])
    p.locked = False
    p.is_closing = False
    p.update = False
    p.keep_connected = False
    p.native_yt = False
    p.queue = deque()
    p.failed_tracks = []
    p.current = None
    p.last_track = None
    p._new_node_task = None
    p.set_command_log = MagicMock()
    p.report_error = AsyncMock()
    p.process_next = AsyncMock()
    p.process_save_queue = AsyncMock()
    p.update_stage_topic = AsyncMock()
    for nome, valor in extra.items():
        setattr(p, nome, valor)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    return p


class FilaDeUmEvento:
    """Entrega o evento e, no ``get`` seguinte, marca o player como encerrando (o laço do ``hook_events`` termina)."""

    def __init__(self, p, evento):
        self.p = p
        self.evento = evento
        self.entregue = False

    async def get(self):
        if self.entregue:
            self.p.is_closing = True
            return None
        self.entregue = True
        return self.evento


def faixa_com_falha(**extra):
    base = dict(
        title="Faixa X",
        uri="https://exemplo.invalid/x",
        search_uri="",
        id="abc",
        track_loops=0,
        source_name="soundcloud",
        autoplay=False,
        info={"sourceName": "soundcloud"},
    )
    base.update(extra)
    return SimpleNamespace(**base)


def evento_de_falha():
    return wavelink.TrackException(
        {
            "track": None,
            "player": None,
            "node": SimpleNamespace(identifier="no-1"),
            "exception": {"message": "Falha", "cause": "java.lang.RuntimeException: x", "severity": "COMMON"},
        }
    )


def parcial(fonte):
    return PartialTrack(uri="https://exemplo.invalid/p", title="Parcial", author="Artista", source_name=fonte)


class TestBugsDoPlayerKi29b:
    @pytest.mark.asyncio
    async def test_track_start_relanca_o_cancelamento_ao_salvar_a_fila(self, monkeypatch):
        p = jogador(monkeypatch)
        p.process_save_queue.side_effect = asyncio.CancelledError
        p.event_queue = FilaDeUmEvento(p, wavelink.TrackStart({"node": p.node}))

        with pytest.raises(asyncio.CancelledError):
            await p.hook_events()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mensagem", "remove_provedores"),
        [("Connect timed out", False), ("This video is not available", True)],
        ids=["falha-qualquer", "video-indisponivel"],
    )
    async def test_resolve_track_so_remove_provedores_do_youtube_pela_frase_conhecida(
        self, monkeypatch, mensagem, remove_provedores
    ):
        cog = MagicMock()
        p = jogador(monkeypatch, native_yt=True)
        p.bot.get_cog = lambda nome: cog
        p.node.partial_providers = ['ytsearch:"{title} - {author}"']
        p.node.get_tracks = AsyncMock(side_effect=Exception(mensagem))

        await p.resolve_track(parcial("youtube"), force=True)

        assert cog.remove_provider.called is remove_provedores

    @pytest.mark.asyncio
    async def test_track_exception_sem_canal_de_texto_segue_para_o_proximo(self, monkeypatch):
        track = faixa_com_falha()
        p = jogador(monkeypatch, current=track, last_track=track, text_channel=None)
        p.event_queue = FilaDeUmEvento(p, evento_de_falha())

        await p.hook_events()

        p.process_next.assert_awaited_once()
        assert p.locked is False

    @pytest.mark.asyncio
    async def test_cache_da_faixa_parcial_guarda_so_o_resultado_da_consulta(self, monkeypatch):
        stream = SimpleNamespace(
            title="Ao vivo",
            is_stream=True,
            id="s1",
            duration=0,
            author="Artista",
            thumb="",
            info={"sourceName": "soundcloud"},
        )
        p = jogador(monkeypatch)
        p.node.partial_providers = ['scsearch:"{title} - {author}"', 'scsearch:"{author} {title}"']
        p.node.get_tracks = AsyncMock(side_effect=lambda *a, **k: [stream])
        track = parcial("spotify")

        await p.resolve_track(track)

        [em_cache] = p.bot.pool.partial_track_cache.values()
        assert em_cache == [stream]

    @pytest.mark.asyncio
    async def test_resolve_track_consulta_o_proximo_provedor_quando_a_primeira_consulta_so_traz_rejeitadas(
        self, monkeypatch
    ):
        def resultado(id_, *, stream):
            return SimpleNamespace(
                title="Parcial",
                is_stream=stream,
                id=id_,
                duration=0,
                author="Artista",
                thumb="",
                info={"sourceName": "soundcloud"},
            )

        stream, valida = resultado("s1", stream=True), resultado("v1", stream=False)
        por_consulta = {'scsearch:"Parcial - Artista"': [stream], 'scsearch:"Artista Parcial"': [valida]}
        p = jogador(monkeypatch)
        p.node.partial_providers = ['scsearch:"{title} - {author}"', 'scsearch:"{author} {title}"']
        p.node.get_tracks = AsyncMock(side_effect=lambda consulta, **k: por_consulta[consulta])
        track = parcial("spotify")

        await p.resolve_track(track)

        assert p.node.get_tracks.await_count == 2
        assert track.id == "v1"
