"""Carga mínima do webhook de erro: só o tipo da exceção e o código ``ERR-XXXXXX``.

O canal do Discord guarda o que recebe sem prazo. Por isso NENHUM remetente ao webhook envia traceback, texto de exceção, ID ou nome de
usuário, ID de servidor ou de canal, nem o conteúdo do pedido: o detalhe fica no arquivo de log (com retenção) e se acha pelo código.
"""

from __future__ import annotations

import disnake

from empiremuse.logs.redact import scrub

NAME_LIMIT = 100

__all__ = ["minimal_embed", "minimal_report_text", "user_report_embed"]


def minimal_report_text(error: BaseException | None, code: str) -> str:
    """Texto do anexo: tipo da exceção (o original, se vier embrulhado) e o código (``-`` se não houve)."""
    kind = scrub(type(getattr(error, "original", error)).__name__, NAME_LIMIT)
    return f"Tipo da exceção: {kind}\nCódigo do erro: {code or '-'}\n"


def minimal_embed(title: str, code: str) -> disnake.Embed:
    """Embed sem dado de usuário, servidor ou canal: só o título fixo e o código do erro."""
    embed = disnake.Embed(title=title, timestamp=disnake.utils.utcnow())
    if code:
        embed.add_field(name="Código do erro:", value=f"`{code}`", inline=False)
    return embed


def user_report_embed(
    details: str, log_text: str | None, image_url: str | None, color: int | disnake.Colour
) -> disnake.Embed:
    """Embed do botão "Reportar erro": o texto livre do próprio membro e o texto do erro mostrado a ele.

    Sem autor (nome, ID, avatar), sem rodapé e sem ``icon_url``: nada que ligue o relatório a uma pessoa ou a um servidor.
    """
    embed = disnake.Embed(color=color, description=details, title="Report de erro")
    embed.add_field(name="Log:", value=log_text or "-")
    if image_url:
        embed.set_image(url=image_url)
    return embed
