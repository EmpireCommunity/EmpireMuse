"""Apoio dos testes do item 7 P4 (erros e ``COMMAND_LOG``): modelos do disnake falsos com NOME, apelido e URL assinada reconhecíveis.

Os valores "sensíveis" são montados em tempo de execução. Cada teste procura esses textos na saída: nenhum pode aparecer.
"""

import enum
import io
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import disnake

from empiremuse.logs.settings import LogSettings

NOME_DO_USUARIO = "".join(["Fulano", "Da", "Silva"])
NOME_GLOBAL = "".join(["global", "-", "fulano"])
APELIDO = "".join(["apelido", "-", "fulano"])
NOME_DO_SERVIDOR = "".join(["Servidor", " ", "Reservado"])
CONTEUDO_DA_MENSAGEM = "".join(["texto privado ", "da mensagem"])
URL_DO_ANEXO = "".join(["https://cdn.", "discordapp.com/attachments/1/2/foto.png", "?ex=abc123&hm=def456"])
USUARIO, SERVIDOR, CANAL = 4242, 5151, 6060
NOMES_PROIBIDOS = (
    NOME_DO_USUARIO,
    NOME_GLOBAL,
    APELIDO,
    NOME_DO_SERVIDOR,
    CONTEUDO_DA_MENSAGEM,
    "cdn.discordapp.com",
    "hm=def456",
)


def _modelo(classe: type, **atributos: object) -> MagicMock:
    falso = MagicMock(spec=classe)
    for chave, valor in atributos.items():
        setattr(falso, chave, valor)
    falso.__str__ = MagicMock(return_value=f"{atributos.get('name', classe.__name__)}")
    return falso


def membro(id_: int = USUARIO) -> MagicMock:
    return _modelo(
        disnake.Member, id=id_, name=NOME_DO_USUARIO, global_name=NOME_GLOBAL, nick=APELIDO, display_name=APELIDO
    )


def servidor(id_: int = SERVIDOR) -> MagicMock:
    return _modelo(disnake.Guild, id=id_, name=NOME_DO_SERVIDOR)


def mensagem(id_: int = 77) -> MagicMock:
    return _modelo(disnake.Message, id=id_, content=CONTEUDO_DA_MENSAGEM)


def cargo(id_: int = 88) -> MagicMock:
    return _modelo(disnake.Role, id=id_, name="Cargo Reservado")


def canal_de_texto(id_: int = CANAL) -> MagicMock:
    return _modelo(disnake.TextChannel, id=id_, name="canal-reservado")


def anexo(tipo: str = "image/png", tamanho: int = 1234) -> MagicMock:
    return _modelo(
        disnake.Attachment,
        id=99,
        filename="foto.png",
        url=URL_DO_ANEXO,
        proxy_url=URL_DO_ANEXO,
        content_type=tipo,
        size=tamanho,
    )


class Cor(enum.Enum):
    VERMELHO = "vermelho"


def interacao(opcoes: dict[str, object], comando: str = "play") -> SimpleNamespace:
    """Interação de slash: ``filled_options`` é o que o disnake preencheu."""
    autor = membro()
    return SimpleNamespace(
        data=SimpleNamespace(name=comando),
        application_command=SimpleNamespace(qualified_name=comando, extras={}),
        author=autor,
        user=autor,
        guild=servidor(),
        guild_id=SERVIDOR,
        channel=canal_de_texto(),
        channel_id=CANAL,
        filled_options=opcoes,
        bot=SimpleNamespace(is_owner=_nao_dono),
    )


async def _nao_dono(_autor: object) -> bool:
    return False


def contexto_de_prefixo(comando: str = "play", resto: str = "musica secreta") -> SimpleNamespace:
    autor = membro()
    return SimpleNamespace(
        command=SimpleNamespace(name=comando, qualified_name=comando),
        author=autor,
        guild=servidor(),
        channel=canal_de_texto(),
        message=SimpleNamespace(id=77, content=f"!{comando} {resto}"),
        prefix="!",
        args=[],
        kwargs={},
    )


def configuracao(*, debug_guilds: frozenset[int] = frozenset(), nivel: int = logging.INFO) -> LogSettings:
    return LogSettings(level=nivel, color=False, file=False, debug_guilds=debug_guilds, metrics_interval=0)


def novo_fluxo() -> io.StringIO:
    return io.StringIO()


def sem_nomes(texto: str) -> list[str]:
    return [nome for nome in NOMES_PROIBIDOS if nome in texto]
