"""Presença na call: ``player_vc_disconnect`` só conta como "tem ouvinte" quem não é bot e não está surdo.

Antes, ``check`` era um gerador e ``bool(gerador)`` é sempre ``True``: a call só com ouvintes surdos (ou só com bots) nunca
disparava o tempo de espera por ausência de ouvintes.
"""

import asyncio
from types import SimpleNamespace

from empiremuse.music_cog.player_lifecycle import PlayerLifecycleMixin

GUILD = 910


class _Lifecycle(PlayerLifecycleMixin):
    pass


def _membro(*, bot: bool = False, deaf: bool = False, self_deaf: bool = False) -> SimpleNamespace:
    return SimpleNamespace(bot=bot, voice=SimpleNamespace(deaf=deaf, self_deaf=self_deaf))


def _chamar_mudanca_no_mesmo_canal(membros: list[SimpleNamespace]) -> list[bool]:
    """Evento de voz que não troca de canal (mute/deafen de alguém): devolve o ``check`` dado a ``start_members_timeout``."""
    recebidos: list[bool] = []
    canal = SimpleNamespace(members=membros)
    player = SimpleNamespace(
        last_channel=canal,
        members_timeout_task=None,
        start_members_timeout=lambda check, **_k: recebidos.append(check),
    )
    cog = _Lifecycle()
    cog.bot = SimpleNamespace(music=SimpleNamespace(players={GUILD: player}))
    quem = SimpleNamespace(bot=False, guild=SimpleNamespace(id=GUILD), id=99)
    estado = SimpleNamespace(channel=canal)
    asyncio.run(cog.player_vc_disconnect(quem, estado, estado))
    return recebidos


class TestPresencaNoMesmoCanal:
    def test_only_deafened_listeners_do_not_count_as_presence(self) -> None:
        assert _chamar_mudanca_no_mesmo_canal([_membro(self_deaf=True), _membro(deaf=True)]) == [False]

    def test_only_bots_do_not_count_as_presence(self) -> None:
        assert _chamar_mudanca_no_mesmo_canal([_membro(bot=True)]) == [False]

    def test_an_empty_call_does_not_count_as_presence(self) -> None:
        assert _chamar_mudanca_no_mesmo_canal([]) == [False]

    def test_one_listening_member_counts_as_presence(self) -> None:
        assert _chamar_mudanca_no_mesmo_canal([_membro(self_deaf=True), _membro()]) == [True]
