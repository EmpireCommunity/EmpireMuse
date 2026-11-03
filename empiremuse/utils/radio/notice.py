"""O bot só-rádio e os comandos de prefixo (item 8, R1): ele não atende ``/play``, pedido nem prefixo; quem tenta recebe UM aviso por canal por minuto.

Chamado no começo do ``on_message`` do ``BotCore``, logo depois de montar o contexto do comando. Devolve ``True`` quando a mensagem é
dele e já foi tratada (o ``on_message`` para ali): o bot só-rádio nunca segue para a mensagem com menção, o pedido de música nem o comando.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import disnake

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)


def _another_bot_will_answer(bot: BotCore, hub: Any, message: disnake.Message) -> bool:
    """Há, neste servidor, outro bot pronto e que NÃO é só-rádio? Num prefixo compartilhado ele executa o comando: dizer "este bot não atende"
    seria avisar à toa. O aviso fica para quando só o bot só-rádio ouviu."""
    guild_bots = getattr(bot.pool, "get_guild_bots", None)
    if guild_bots is None or message.guild is None:
        return False
    return any(
        other is not bot
        and getattr(other, "bot_ready", False)
        and not hub.roles.is_radio_only(other.identifier)
        and _can_answer_here(other, message)
        for other in guild_bots(message.guild.id)
    )


def _can_answer_here(other: Any, message: disnake.Message) -> bool:
    """O OUTRO bot está neste servidor e pode escrever neste canal (numa thread, no canal pai com ``send_messages_in_threads``)? Pronto não basta:
    sem estar no servidor ou sem permissão ele não responde, e o aviso do bot só-rádio seria o único retorno."""
    try:
        guild = other.get_guild(message.guild.id)
        if guild is None or guild.me is None:
            return False
        channel = guild.get_channel_or_thread(message.channel.id)
        if channel is None:
            return False
        if isinstance(channel, disnake.Thread):
            return channel.parent is not None and bool(
                channel.parent.permissions_for(guild.me).send_messages_in_threads
            )
        return bool(channel.permissions_for(guild.me).send_messages)
    except AttributeError:
        return False  # sem como confirmar, o aviso sai (melhor avisar a mais do que deixar o usuário sem resposta)


async def refuse_radio_only(bot: BotCore, ctx: Any, message: disnake.Message) -> bool:
    """``True`` se ``bot`` é só-rádio (a mensagem não segue). Um comando de prefixo ganha o aviso, no máximo um por canal por minuto."""
    pool = bot.pool
    hub = getattr(pool, "radio", None)
    if hub is None or not hub.roles.is_radio_only(bot.identifier):
        return False
    if (
        getattr(ctx, "valid", False)
        and not _another_bot_will_answer(bot, hub, message)
        and hub.claim_notice(message.channel.id)
    ):
        try:
            tr = await pool.i18n.for_guild(message.guild)
            await message.channel.send(
                tr("radio.only_bot_notice"),
                allowed_mentions=disnake.AllowedMentions.none(),
                delete_after=15,
            )
        except Exception as error:  # noqa: BLE001 - o aviso é cortesia: sem permissão de enviar, só o tipo vai ao log
            logger.warning("Aviso do bot só-rádio não enviado (%s)", type(error).__name__)
    return True
