"""PR #31 (7ad9366c) — `Music.get_lavalink_tracks` ignora nós sem o source manager do Spotify quando a consulta é um link do Spotify.

Sem rede e sem Discord reais: nós e cog são dublês simples (SimpleNamespace / AsyncMock).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.regression

SPOTIFY_TRACK = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
SPOTIFY_SHORT = "https://spotify.link/AbC123xYz"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def run(coro):
    return asyncio.run(coro)


def make_node(identifier, source_managers, players=0, tracks=("faixa",)):
    return SimpleNamespace(
        identifier=identifier,
        is_available=True,
        available=True,
        players=[None] * players,
        info={"sourceManagers": list(source_managers)},
        search_providers=["ytsearch"],
        get_tracks=AsyncMock(return_value=list(tracks)),
    )


def search(nodes, query, node=None):
    from empiremuse.cogs.music import Music

    cog = SimpleNamespace(
        bot=SimpleNamespace(music=SimpleNamespace(nodes={n.identifier: n for n in nodes}), pool=SimpleNamespace())
    )
    return run(Music.get_lavalink_tracks(cog, query, ctx=None, user=SimpleNamespace(id=1), node=node, source=False))


@pytest.mark.parametrize("query", [SPOTIFY_TRACK, SPOTIFY_SHORT])
class TestSpotifyUrlSkipsNodesWithoutSpotify:
    def test_node_without_spotify_is_not_queried(self, query):
        sem = make_node("sem", ["youtube"], players=0)
        com = make_node("com", ["youtube", "spotify"], players=5)

        tracks, node, exceptions = search([sem, com], query)

        sem.get_tracks.assert_not_awaited()
        com.get_tracks.assert_awaited_once()
        assert tracks == ["faixa"]
        assert node is com
        assert exceptions == set()

    def test_requested_node_without_spotify_is_skipped_and_the_resolving_node_is_returned(self, query):
        pedido = make_node("pedido", ["youtube"])
        outro = make_node("outro", ["youtube", "spotify"], players=3)

        tracks, node, _ = search([pedido, outro], query, node=pedido)

        pedido.get_tracks.assert_not_awaited()
        outro.get_tracks.assert_awaited_once()
        assert tracks == ["faixa"]
        assert node is outro


class TestNonSpotifyQueryUsesAnyNode:
    def test_youtube_url_is_resolved_by_node_without_spotify(self):
        sem = make_node("sem", ["youtube"])

        tracks, node, _ = search([sem], YOUTUBE_URL)

        sem.get_tracks.assert_awaited_once()
        assert tracks == ["faixa"]
        assert node is sem


class TestSpotifyDesligadoNaBuscaPorProvedor:
    """Sem ``SPOTIFY_CLIENT_ID`` o ``pool.spotify`` é ``None``: o provedor ``spsearch`` é pulado, não vira erro inesperado."""

    def test_spsearch_e_pulado_e_a_busca_segue_pelo_no(self):
        from empiremuse.cogs.music import Music

        no = make_node("no", ["youtube"])

        async def so_aceita_youtube(busca, **kwargs):
            assert not busca.startswith("spsearch"), busca
            return ["faixa"]

        no.get_tracks = AsyncMock(side_effect=so_aceita_youtube)
        bot = SimpleNamespace(
            music=SimpleNamespace(nodes={"no": no}),
            pool=SimpleNamespace(spotify=None, deezer=SimpleNamespace()),
            dispatch=MagicMock(),
        )
        cog = SimpleNamespace(bot=bot)

        tracks, node, exceptions = run(
            Music.get_lavalink_tracks(cog, "musica qualquer", ctx=None, user=SimpleNamespace(id=1), source="spsearch")
        )

        bot.dispatch.assert_not_called()
        assert exceptions == set()
        no.get_tracks.assert_awaited_once()
        assert no.get_tracks.await_args.args[0] == "ytsearch:musica qualquer"
        assert tracks == ["faixa"] and node is no
