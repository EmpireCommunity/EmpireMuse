"""BK-10: a marca ``limited_to_supporters`` (fonte do ``supporter_limited`` do site-data) bate com o que o bot FAZ.

Cada subcomando público de ``/radio`` e do prefixo ``radio`` roda de verdade num servidor SEM o acesso de Apoiador (com uma rádio já
configurada): o marcado RECUSA com o aviso de Apoiador (o erro de resposta), e nenhum outro recusa por isso. Uma marca esquecida num comando novo que passa a
exigir o acesso, ou uma marca que sobrou num comando que deixou de exigir, quebra este teste.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake

import pytest

from empiremuse.cogs import radio as radio_cog
from empiremuse.rules.apoiador import is_limited_to_supporters
from tests.cogs.radio_config import configure
from tests.cogs.test_radio_comandos import CHANNEL, GUILD, build, fresh_cooldown  # noqa: F401

pytestmark = pytest.mark.critical

REFUSAL = "Limitadas a Apoiadores"  # o texto de ``radio.refused.no_premium`` e de ``radio.status.no_premium`` em pt-BR


def slash_leaves(command, path=("radio",)):
    children = getattr(command, "children", {})
    if not children:
        yield " ".join(path), command
    for name, child in sorted(children.items()):
        yield from slash_leaves(child, (*path, name))


def prefix_leaves():
    group = radio_cog.Radio.radio_prefix  # sozinho, o prefixo ``radio`` é o ``radio status`` (testado abaixo)
    for sub in sorted(group.commands, key=lambda command: command.name):
        if not sub.hidden:
            yield f"radio {sub.name}", sub


SLASH_ARGS = {  # os argumentos de cada subcomando de barra (valores válidos; o que importa é o caminho sem o acesso)
    "radio setup": lambda w: {"channel": w.channel, "bot": "2", "kind": "custom"},
    "radio on": lambda w: {"channel": str(CHANNEL), "bot": None},
    "radio off": lambda w: {"channel": str(CHANNEL), "bot": None},
    "radio reset": lambda w: {"channel": str(CHANNEL), "bot": None},
    "radio station create": lambda w: {"name": "Rock", "kind": "artists"},
    "radio station edit": lambda w: {"name": "Rock"},
    "radio station delete": lambda w: {"name": "Rock"},
    "radio playlists edit": lambda w: {"name": "Rock"},
    "radio playlists remove": lambda w: {"name": "Rock"},
}


async def without_access():
    """Um servidor com uma rádio configurada e SEM o acesso de Apoiador; as respostas de ``inter`` e ``ctx`` são mocks."""
    w = build()
    await w.hub.control.load()  # sem pausa do dono: a única recusa possível por acesso é a de Apoiador
    await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2, kind="custom", source=None)
    replies = AsyncMock()
    w.inter.edit_original_message = replies
    w.inter.send = replies
    w.inter.response = SimpleNamespace(
        defer=AsyncMock(), send_message=replies, send_modal=replies, is_done=lambda: True
    )
    member = MagicMock(
        spec=disnake.Member
    )  # ``/radio playlists`` só segue com um membro de verdade que gerencia o servidor
    member.id = w.inter.author.id
    member.guild_permissions = SimpleNamespace(manage_guild=True, administrator=False)
    member.voice = SimpleNamespace(channel=w.channel)
    w.inter.author = member
    w.inter.guild.owner_id = 0
    w.ctx = SimpleNamespace(
        guild=w.inter.guild, author=w.inter.author, message=SimpleNamespace(guild=w.inter.guild), send=replies
    )
    return w


async def refusal(call) -> str:
    """O texto da RECUSA (o ``GenericError`` que o bot devolve como resposta de erro), ou vazio se o comando respondeu. ``/radio status``
    responde normalmente e só INFORMA que o servidor não tem o acesso: isso não é recusa e não entra aqui."""
    try:
        await call
    except Exception as error:  # noqa: BLE001 - outro erro qualquer também não é a recusa de Apoiador
        return str(error)
    return ""


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "leaf"), list(slash_leaves(radio_cog.Radio.radio)), ids=lambda item: str(item))
async def test_every_slash_radio_subcommand_refuses_without_the_access_exactly_when_marked(path, leaf):
    w = await without_access()
    args = SLASH_ARGS.get(path, lambda w: {})(w)

    said = await refusal(leaf.callback(w.cog, w.inter, **args))

    assert (REFUSAL in said) is is_limited_to_supporters(leaf.callback), f"{path}: {said[:300]}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "command"), list(prefix_leaves()), ids=lambda item: str(item))
async def test_every_public_radio_prefix_command_refuses_without_the_access_exactly_when_marked(path, command):
    w = await without_access()

    said = await refusal(command.callback(w.cog, w.ctx))

    assert (REFUSAL in said) is is_limited_to_supporters(command.callback), f"{path}: {said[:300]}"
