"""V-145-6: a retomada não sobrepõe o permanente salvo durante a leitura do banco, mesmo quando o agendamento foi cancelado em seguida.

O ``_cancel_voice_status_tasks`` devolve o ``voice_status_apply`` a ``None``, igual a antes do save: comparar a tarefa não vê o save.
A geração do agendamento só anda, então a retomada percebe que o valor lido ficou velho.
"""

import pytest

from empiremuse.rules.voice_status import VoiceStatusConfig
from empiremuse.utils.music.models.player_voice import PlayerVoiceMixin, schedule_permanent_voice_status
from tests.persistence.test_sessao_retomada_sem_voice_state import _ambiente, montar, sessao_gravada  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.regression]

DESLIGADO = VoiceStatusConfig(False, "modelo salvo")


def aplicacoes(jogador) -> list:
    return [c[1] for c in jogador.chamadas if isinstance(c, tuple) and c[0] == "apply_permanent_voice_status"]


async def test_resume_keeps_a_save_scheduled_and_cancelled_during_the_database_read():
    dados = await sessao_gravada()
    cog, _no, criados = montar(4)

    async def leitura_lenta(*args, **kwargs):
        if criados:  # durante a leitura: o admin salva o permanente desligado e a sessão é desligada logo depois
            schedule_permanent_voice_status(criados[0], DESLIGADO)
            PlayerVoiceMixin._cancel_voice_status_tasks(criados[0])
        return {"voice_status_permanent": True}  # o valor velho, lido antes do save

    cog.bot.get_global_data = leitura_lenta

    await cog.resume_player(dados, hints=[])

    assert aplicacoes(criados[0]) == [DESLIGADO]


async def test_resume_applies_the_permanent_when_nothing_was_scheduled_meanwhile():
    dados = await sessao_gravada()
    cog, _no, criados = montar(4)

    await cog.resume_player(dados, hints=[])

    assert len(aplicacoes(criados[0])) == 1
