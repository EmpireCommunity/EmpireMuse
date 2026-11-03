"""E4c — `check_channel_perm` (utils/music/jukebox_channel.py) deve enxergar os nomes CANÔNICOS das permissões.

Hoje `Permissions` itera por `view_channel`/`send_messages` e a lista da função usa os aliases `read_messages`/
`create_forum_threads`, que nunca casam: falta de ler mensagens (e de postar em fórum) passa batido (RED).
"""

import pytest

from empiremuse.utils.music import jukebox_channel
from empiremuse.utils.music.errors import GenericError
from tests.support.canal_da_jukebox import (
    BOT_MENTION,
    FULL_TEXT,
    FakeGuild,
    FakeStage,
    FakeText,
    Journal,
    perms,
)

TEXT_NEEDS = ("send_messages", "embed_links", "view_channel")
ESC = "\x1b"


def exact(channel, missing):
    """Mensagem EXATA esperada (bloco ```ansi, uma linha ``▪`` por permissão, na ordem de iteração de Permissions; singular com uma só)."""
    from empiremuse.utils.music.converters import perms_translations

    ordered = [name for name, _ in channel.permissions_for(None) if name in missing]
    plural = len(ordered) > 1
    lines = "\n".join(f" {ESC}[0;33m▪ {ESC}[0;31m{perms_translations.get(name, name)} {ESC}[0m" for name in ordered)
    head = (
        f"**{BOT_MENTION} não possui {'as seguintes permissões necessárias' if plural else 'a seguinte permissão necessária'} "
        f"no canal {channel.mention}**"
    )
    return f"{head} ```ansi\n{lines}```"


def guild():
    j = Journal()
    return j, FakeGuild(j, perms(manage_channels=True))


def without(*names):
    return perms(**{k: v for k, v in FULL_TEXT.items() if k not in names})


def message_of(channel):
    with pytest.raises(GenericError) as raised:
        jukebox_channel.check_channel_perm(channel)
    return raised.value.text


class TestCheckChannelPerm:
    @pytest.mark.parametrize("kind", [FakeText, FakeStage])
    @pytest.mark.parametrize("missing", list(TEXT_NEEDS)[:2])
    def test_text_voice_and_stage_report_each_missing_permission(self, kind, missing):
        j, g = guild()
        channel = kind(j, 22, "pedidos", g, without(missing))
        assert message_of(channel) == exact(channel, (missing,))
