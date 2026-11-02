# -*- coding: utf-8 -*-
"""Configuração e redefinição do canal da Jukebox (compartilhado entre Discord e painel web).

Reúne a validação de alvo/permissões e os efeitos no Discord + banco de ``/jukebox setup`` e ``/jukebox reset``, sem depender de
``ApplicationCommandInteraction``: quem chama entrega o canal já escolhido, o ator (``id`` e ``mention``) e, quando precisar
de interação com o usuário (título de post em fórum ou aviso final do reset), callbacks assíncronos.

Ordem das operações:
- ``apply_jukebox_channel``: valida → limpa mensagens no canal existente (lotes de 100 para < 14d, 1/s para >= 14d até 500,
  inclusive fixadas) → cria/edita no Discord → grava na seção ``jukebox`` do documento único (com ``expected``) → move o player.
- ``jukebox_reset``: apaga a mensagem do painel no Discord → limpa a seção ``jukebox`` e restaura as opções A e B para o padrão →
  avisa → desfaz o modo estático do player.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any, NamedTuple, Optional, Union

import disnake

from empiremuse.rules.limits import FORUM_POST_TITLE_MAX_LENGTH
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.music.converters import perms_translations
from empiremuse.utils.others import CustomContext, send_idle_embed
from empiremuse.utils.server_jukebox import JukeboxConfig
from empiremuse.utils.servers import ServerConfigConflict

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

TargetChannel = Union[disnake.TextChannel, disnake.VoiceChannel, disnake.ForumChannel, disnake.StageChannel]
AskForumTitle = Callable[[str], Awaitable[Optional[str]]]
Notify = Callable[[str], Awaitable[None]]

PURGE_MAX_MESSAGES = 500
BULK_DELETE_LIMIT = 100
BULK_DELETE_MAX_AGE = timedelta(days=14)


def config_as_data(config: JukeboxConfig) -> dict:
    """A Jukebox no formato que o ``send_idle_embed`` lê (``player_controller.channel``/``message_id`` como texto)."""
    return {
        "player_controller": {
            "channel": str(config.channel) if config.channel else None,
            "message_id": str(config.message_id) if config.message_id else None,
        }
    }


async def ensure_panel_bot(bot: BotCore, guild_id: int) -> None:
    """Só o bot do painel configura ou redefine a Jukebox: ele é o autor da mensagem do canal (S-V11)."""
    if not await bot.pool.servers.jukebox.is_panel_bot(guild_id, bot):
        raise GenericError("**Só o bot do painel do servidor pode configurar ou redefinir a Jukebox.**")


channel_perms = ("send_messages", "embed_links", "read_messages")

thread_perms = ("send_messages_in_threads", "embed_links", "read_messages")

forum_perms = ("create_forum_threads", "send_messages_in_threads", "read_messages", "embed_links", "manage_threads")


def check_channel_perm(channel: TargetChannel):
    if isinstance(channel, disnake.ForumChannel):
        missing_perms = [p for p, v in channel.permissions_for(channel.guild.me) if p in forum_perms and not v]
    elif isinstance(channel, disnake.Thread):
        missing_perms = [p for p, v in channel.parent.permissions_for(channel.guild.me) if p in thread_perms and not v]
    else:
        missing_perms = [p for p, v in channel.permissions_for(channel.guild.me) if p in channel_perms and not v]

    if missing_perms:
        raise GenericError(
            f"**{channel.guild.me.mention} não possui a{'s'[: len(missing_perms) ^ 1]} seguinte{'s'[: len(missing_perms) ^ 1]} "
            f"permiss{'ão' if len(missing_perms) == 1 else 'ões'} necessária{'s'[: len(missing_perms) ^ 1]} no canal {channel.mention}** ```ansi\n"
            + "\n".join(f" \x1b[0;33m▪ \x1b[0;31m{perms_translations.get(p, p)} \x1b[0m" for p in missing_perms)
            + "```"
        )


class MissingManagePermissions(GenericError):
    """Faltam permissões de gerenciamento do servidor (``manage_channels`` / ``manage_threads``) para ``/jukebox setup``."""

    def __init__(self, bot_mention: str, missing: list[str]) -> None:
        self.missing = list(missing)
        lines = "\n".join(f" \x1b[0;33m▪ \x1b[0;31m{perms_translations.get(p, p)} \x1b[0m" for p in missing)
        super().__init__(
            f"**{bot_mention} precisa da{'s'[: len(missing) ^ 1]} permissão{'ões'[: len(missing) ^ 1]} abaixo "
            f"ativada{'s'[: len(missing) ^ 1]} na função/cargo dele para essa função funcionar corretamente:**```ansi\n"
            f"{lines}```"
        )


class JukeboxChannelResult(NamedTuple):
    channel: Any
    message: Any


def missing_channel_permissions(channel: TargetChannel) -> list[str]:
    """Nomes canônicos das permissões que faltam ao bot em ``channel`` (lista vazia = tudo certo)."""
    if isinstance(channel, disnake.ForumChannel):
        needed = forum_perms
        perms = channel.permissions_for(channel.guild.me)
    elif isinstance(channel, disnake.Thread):
        needed = thread_perms
        perms = channel.parent.permissions_for(channel.guild.me)
    else:
        needed = channel_perms
        perms = channel.permissions_for(channel.guild.me)
    return [p for p, v in perms if p in needed and not v]


def build_channel_kwargs(guild: disnake.Guild) -> dict:
    return {
        "overwrites": {
            guild.me: disnake.PermissionOverwrite(
                embed_links=True,
                send_messages=True,
                send_messages_in_threads=True,
                read_messages=True,
                read_message_history=True,
                manage_messages=True,
                manage_channels=True,
                attach_files=True,
            )
        },
        "slowmode_delay": 5,
    }


async def find_current_jukebox_message(bot: BotCore, guild: disnake.Guild, config: JukeboxConfig):
    """``(player ativo ou None, mensagem atual da Jukebox ou None)``: player estático primeiro, depois o banco."""
    message = None
    try:
        player: LavalinkPlayer = bot.music.players[guild.id]
        if player.static:
            message = player.message
    except KeyError:
        player = None

    if not message and config.channel and config.message_id:
        try:
            channel = bot.get_channel(config.channel) or await guild.fetch_channel(config.channel)
            message = await channel.fetch_message(config.message_id)
        except Exception:
            logger.debug("Mensagem anterior da Jukebox não encontrada", exc_info=True)

    return player, message


def ensure_can_create_channel(bot: BotCore, guild: disnake.Guild) -> None:
    req_perms = [p for p, v in guild.me.guild_permissions if p in ("manage_channels", "manage_threads") and not v]
    if req_perms:
        raise MissingManagePermissions(bot.user.mention, req_perms)


async def create_jukebox_channel(
    bot: BotCore,
    parent: Union[disnake.Guild, disnake.CategoryChannel],
    kind: str,
    channel_kwargs: dict,
    *,
    name: Optional[str] = None,
    actor: Any = None,
):
    """Cria um canal novo de texto, voz ou palco em ``parent`` (servidor ou categoria)."""
    channel_name = (name or f"{bot.user.name} player controller")[:FORUM_POST_TITLE_MAX_LENGTH]
    reason = f"Canal da Jukebox criado por: {actor}" if actor else "Canal da Jukebox criado pelo painel."

    if not parent.permissions_for(
        parent.guild.me if isinstance(parent, disnake.CategoryChannel) else parent.me
    ).manage_channels:
        where = f"na categoria {parent.mention}" if isinstance(parent, disnake.CategoryChannel) else "no servidor"
        raise GenericError(f"**{bot.user.mention} não possui permissão de gerenciar canais {where}.**")

    if kind == "voice":
        return await parent.create_voice_channel(channel_name, reason=reason, **channel_kwargs)
    if kind == "stage":
        return await parent.create_stage_channel(channel_name, reason=reason, **channel_kwargs)
    return await parent.create_text_channel(channel_name, reason=reason, **channel_kwargs)


def validate_jukebox_target(
    bot: BotCore,
    guild: disnake.Guild,
    target: Any,
    *,
    existing_channel: bool,
    command_prefix: Optional[str] = None,
) -> None:
    """Valida se ``target`` pode receber o painel da Jukebox.

    É a primeira etapa de ``apply_jukebox_channel``; o painel a chama antes de criar a tarefa para responder 422 na hora.
    """
    if isinstance(target, CustomContext):
        raise GenericError(
            f"**Você deve mencionar um canal (ou informar o nome pra criar um novo) ao usar o comando "
            f"`{command_prefix}jukebox setup`**"
        )

    if target == guild.public_updates_channel:
        raise GenericError("**Você não pode usar um canal de atualizações do Discord.**")

    if target == guild.rules_channel:
        raise GenericError("**Você não pode usar um canal de regras.**")

    if existing_channel:
        check_channel_perm(target)

    if isinstance(target, disnake.ForumChannel):
        return

    if (
        existing_channel
        and not guild.me.guild_permissions.administrator
        and not target.permissions_for(guild.me).manage_permissions
    ):
        raise GenericError(
            f"**{guild.me.mention} não possui permissão de administrador ou permissão de "
            f"gerenciar permissões do canal {target.mention}** para editar as permissões "
            f"necessárias para a Jukebox funcionar devidamente.\n\n"
            f"Caso não queira fornecer a permissão de administrador ou editar as permissões do"
            f" canal {target.mention} para me permitir gerenciar permissões do canal, você pode usar o comando "
            f"sem selecionar um canal de destino para criar um novo canal."
        )

    if not target.permissions_for(guild.me).read_message_history:
        raise GenericError(
            f"**{bot.user.mention} não possui permissão de ler histórico de mensagens no canal {target.mention}.**"
        )


@asynccontextmanager
async def exclusive_operation(bot: BotCore, guild_id: int, config: JukeboxConfig):
    """Uma operação da Jukebox por vez no servidor (Discord e painel web), com a configuração conferida DENTRO da trava.

    Duas operações simultâneas não chegam a criar canal, limpar mensagens nem editar post no Discord antes de uma delas saber
    que a configuração já mudou.
    """
    jukebox = bot.pool.servers.jukebox
    async with jukebox.operation(guild_id):
        if (await jukebox.get(guild_id)).current != config.current:
            raise ServerConfigConflict
        yield


async def apply_jukebox_channel(
    bot: BotCore, guild: disnake.Guild, actor: Any, target, *, config: JukeboxConfig, **kwargs
) -> Optional[JukeboxChannelResult]:
    """Configura ``target`` como canal da Jukebox sob a trava de operação do servidor.

    Canal recém-criado antes da trava (fluxo do Discord): se a configuração mudou enquanto o usuário escolhia o tipo do canal,
    o canal órfão é apagado antes de levantar ``ServerConfigConflict``.
    """
    try:
        async with exclusive_operation(bot, guild.id, config):
            return await _apply_jukebox_channel(bot, guild, actor, target, config=config, **kwargs)
    except ServerConfigConflict:
        if not kwargs.get("existing_channel", True):
            await _delete_orphan_channel(bot, guild.id, target)
        raise


async def _delete_orphan_channel(bot: BotCore, guild_id: int, target) -> None:
    """Apaga um canal recém-criado que perdeu a corrida para outra operação (nunca se outra operação o gravou como canal)."""
    if target is None or not hasattr(target, "delete"):
        return
    try:
        jukebox = bot.pool.servers.jukebox
        if getattr(target, "id", None) and (await jukebox.get(guild_id)).channel == target.id:
            return  # já é o canal da Jukebox de outra operação: fica
        await target.delete(reason="Configuração da Jukebox mudou durante o /jukebox setup.")
    except Exception:
        logger.debug("Canal órfão não apagado após conflito", exc_info=True)


async def _purge_existing_channel(target, guild_me, original_message) -> None:
    """Limpa todas as mensagens do canal (inclusive fixadas): < 14d em lotes de 100; >= 14d uma a uma a 1/s até 500."""
    old_messages = []
    total = 0
    now = disnake.utils.utcnow()
    if hasattr(target, "history"):
        async for msg in target.history(limit=PURGE_MAX_MESSAGES + 1):
            if original_message and getattr(msg, "id", None) == getattr(original_message, "id", None):
                continue
            if getattr(msg, "author", None) == guild_me and getattr(msg, "thread", None):
                continue
            total += 1
            if total > PURGE_MAX_MESSAGES:
                raise GenericError(
                    "**Este canal possui mais de 500 mensagens. Escolha outro canal ou crie um novo para a Jukebox.**"
                )
            created_at = getattr(msg, "created_at", None)
            if created_at is not None and (now - created_at) >= BULK_DELETE_MAX_AGE:
                old_messages.append(msg)

    check = lambda m: (
        (m.author != guild_me or not m.thread)
        and (not original_message or getattr(m, "id", None) != getattr(original_message, "id", None))
    )
    for _ in range(max(1, (total + BULK_DELETE_LIMIT - 1) // BULK_DELETE_LIMIT)):
        deleted = await target.purge(limit=BULK_DELETE_LIMIT, check=check)
        if not deleted or len(deleted) < BULK_DELETE_LIMIT:
            break

    for idx, msg in enumerate(old_messages):
        try:
            await msg.delete()
        except disnake.NotFound:
            pass
        if idx + 1 < len(old_messages):
            await asyncio.sleep(1.0)


async def _apply_jukebox_channel(
    bot: BotCore,
    guild: disnake.Guild,
    actor: Any,
    target,
    *,
    existing_channel: bool,
    purge: str = "yes",
    channel_kwargs: dict,
    channel_name: str,
    original_message,
    player: Optional[LavalinkPlayer],
    config: JukeboxConfig,
    ask_forum_title: Optional[AskForumTitle] = None,
    command_prefix: Optional[str] = None,
    send_idle_embed=send_idle_embed,
) -> Optional[JukeboxChannelResult]:
    """Configura ``target`` como canal da Jukebox e grava no banco (passos pós-escolha do ``/jukebox setup``).

    Devolve ``None`` apenas quando ``ask_forum_title`` cancela (interação de modal expirada no Discord).
    """
    await ensure_panel_bot(bot, guild.id)
    guild_data = config_as_data(config)

    embed_archived = disnake.Embed(
        description=f"**Este canal da Jukebox foi reconfigurado pelo membro {actor.mention}.**",
        color=bot.get_color(guild.me),
    )

    validate_jukebox_target(bot, guild, target, existing_channel=existing_channel, command_prefix=command_prefix)

    if isinstance(target, disnake.ForumChannel):
        channel_kwargs.clear()

        if ask_forum_title:
            channel_name = await ask_forum_title(channel_name)
            if channel_name is None:
                return None

        thread = None
        message = None

        for t in target.threads:
            if t.owner_id == bot.user.id:
                try:
                    message = await t.fetch_message(t.id)
                except disnake.NotFound:
                    continue
                thread = t
                thread_kw = {}
                if thread.locked and target.permissions_for(target.guild.me).manage_threads:
                    thread_kw.update({"locked": False, "archived": False})
                elif thread.archived:
                    thread_kw["archived"] = False
                if thread_kw:
                    await t.edit(**thread_kw)
                break

        if not thread and guild.me.guild_permissions.read_message_history:
            async for t in target.archived_threads(limit=100):
                if t.owner_id == bot.user.id:
                    try:
                        message = await t.fetch_message(t.id)
                    except disnake.NotFound:
                        continue
                    thread = t
                    thread_kw = {}
                    if thread.locked and target.permissions_for(target.guild.me).manage_threads:
                        thread_kw.update({"locked": False, "archived": False})
                    elif thread.archived:
                        thread_kw["archived"] = False
                    if thread_kw:
                        await t.edit(**thread_kw)
                    break

        if not thread:
            if not target.permissions_for(guild.me).manage_threads:
                raise GenericError(
                    f"**{bot.user.mention} não possui permissão de gerenciar tópicos no canal {target.mention}.**\n"
                    f"`Nota: Você pode me conceder temporariamente essa permissão e após usar o comando novamente "
                    f"você pode remover essa permissão.`"
                )

            thread_wmessage = await target.create_thread(
                name=channel_name,
                content="Post da Jukebox.",
                auto_archive_duration=10080,
                slowmode_delay=5,
            )
            message = thread_wmessage.message

        message = await send_idle_embed(target=message, bot=bot, force=True, guild_data=guild_data)

        target = message.channel

        await _archive_previous_message(original_message, target, actor, embed_archived)

    else:
        if existing_channel and purge == "yes":
            await _purge_existing_channel(target, guild.me, original_message)

        message = await _archive_previous_message(original_message, target, actor, embed_archived)

        if not message:
            async for m in target.history(limit=100):
                if m.author == guild.me and m.thread:
                    message = m
                    break

    if existing_channel and channel_kwargs:
        try:
            await target.edit(**channel_kwargs)
        except Exception:
            logger.exception("Falha em apply_jukebox_channel")

    channel = target

    message = await _switch_player_and_thread(bot, guild, actor, channel, message, player, guild_data, send_idle_embed)

    # canal e mensagem juntos, no documento do servidor; se mudaram desde que o comando leu, ServerConfigConflict (nada é sobrescrito)
    await bot.pool.servers.jukebox.set(guild.id, channel.id, message.id, expected=config.current)

    await _rebind_other_players(bot, channel, message)

    return JukeboxChannelResult(channel, message)


async def _archive_previous_message(original_message, target, actor, embed_archived):
    if not original_message:
        return None
    if original_message.channel != target:
        try:
            if isinstance(original_message.channel.parent, disnake.ForumChannel):
                await original_message.thread.delete(reason=f"Player reconfigurado por {actor}.")
                return None
        except AttributeError:
            pass
        except Exception:
            logger.exception("Falha ao remover tópico anterior da Jukebox")
            return None
        try:
            await original_message.edit(content=None, embed=embed_archived, components=None)
        except Exception:
            pass
        if original_message.thread:
            try:
                await original_message.thread.edit(
                    archived=True,
                    locked=True,
                    reason=f"Player reconfigurado por {actor}.",
                )
            except Exception:
                pass
        return None
    return original_message


async def _switch_player_and_thread(bot, guild, actor, channel, message, player, guild_data, send_idle_embed):
    """Religa o player ativo ao novo canal e garante a thread da Jukebox; devolve a mensagem final."""

    if player and player.text_channel != channel:
        if player.static:
            try:
                await player.message.thread.edit(
                    archived=True, locked=True, reason=f"Player reconfigurado por {actor}."
                )
            except Exception:
                pass
        else:
            try:
                await player.message.delete()
            except Exception:
                pass
        if not message or message.channel.id != channel.id:
            message = await send_idle_embed(channel, bot=bot, force=True, guild_data=guild_data)
        player.message = message
        player.static = True
        player.text_channel = channel
        player.setup_hints()
        player.setup_features()
        await player.invoke_np(force=True)

    elif not message or message.channel.id != channel.id:
        message = await send_idle_embed(channel, bot=bot, force=True, guild_data=guild_data)

    if isinstance(channel, disnake.TextChannel):
        if not message.thread:
            if channel.permissions_for(guild.me).create_public_threads:
                await message.create_thread(name="Jukebox", auto_archive_duration=10080)
        else:
            thread_kw = {}
            if message.thread.locked and message.thread.permissions_for(guild.me).manage_threads:
                thread_kw.update({"locked": False, "archived": False})
            elif message.thread.archived and message.thread.owner_id == bot.user.id:
                thread_kw["archived"] = False
            if thread_kw:
                await message.thread.edit(reason=f"Jukebox reativada por: {actor}.", **thread_kw)
    elif (
        player
        and isinstance(channel, (disnake.VoiceChannel, disnake.StageChannel))
        and player.guild.me.voice.channel != channel
    ):
        await player.connect(channel.id)

    return message


# ============================================================================ reset


def ensure_can_reset(guild: disnake.Guild) -> None:
    if not guild.me.guild_permissions.manage_threads:
        raise GenericError(f"Não tenho permissão de **{perms_translations['manage_threads']}** no servidor.")


async def find_jukebox_channel(bot: BotCore, guild_id: int, config: JukeboxConfig):
    """Canal da Jukebox configurado no banco (cache ou API); ``GenericError`` se não há/foi apagado/é de outro servidor."""
    try:
        channel = bot.get_channel(config.channel) or await bot.fetch_channel(config.channel)  # type: ignore[arg-type]
    except Exception:
        channel = None

    if not channel or channel.guild.id != guild_id:
        raise GenericError(
            f"**Não há canal da Jukebox configurado no bot {bot.user.mention} (ou o canal foi deletado).**"
        )

    return channel


def _other_static_players(bot: BotCore, guild_id: int):
    """Os players estáticos dos OUTROS bots do pool: o canal é único, então eles também seguem o setup e o reset."""
    panel = getattr(bot.pool, "jukebox_panel", None)
    return [(b, p) for b, p in panel.players(guild_id) if b is not bot] if panel else []


async def _rebind_other_players(bot: BotCore, channel, message) -> None:
    """Depois do setup: os players dos outros bots passam a usar o novo canal e a mensagem do painel (que é do bot do painel)."""
    for other, player in _other_static_players(bot, channel.guild.id):
        try:
            player.text_channel = other.get_channel(channel.id) or channel
            player.message = message
            player.setup_hints()
            player.setup_features()
            player.last_data = {}  # o painel novo está ocioso: o redesenho não pode ser pulado por "nada mudou"
        except Exception:
            logger.exception("Falha ao religar o player de outro bot ao novo canal da Jukebox")
    if panel := getattr(bot.pool, "jukebox_panel", None):
        await panel.refresh(channel.guild.id, now=True)


async def _reset_other_players(bot: BotCore, guild_id: int, channel_inter) -> None:
    """Depois do reset: os players dos outros bots deixam de ser estáticos, como o do bot do painel."""
    for other, player in _other_static_players(bot, guild_id):
        try:
            await _reset_player(player, other.get_channel(channel_inter.id) or channel_inter if channel_inter else None)
        except Exception:
            logger.exception("Falha ao redefinir o player de outro bot")


async def _reset_player(player, channel_inter):
    player.static = False
    player.message = None
    player.text_channel = channel_inter
    player.process_hint()
    await player.invoke_np(force=True)


MSG_RESET_DISCORD_FAILED = (
    "**Não foi possível concluir a redefinição da Jukebox no Discord. "
    "A configuração foi mantida; confira as permissões do bot no canal e tente novamente.**"
)


async def _fetch_configured_message(channel, config: JukeboxConfig):
    """Mensagem da Jukebox registrada no banco; ``None`` se não existe mais ou o registro é inválido."""
    try:
        return await channel.fetch_message(config.message_id)
    except disnake.HTTPException, TypeError, ValueError:
        return None


async def _clear_in_discord(actor: Any, channel, delete_channel: Optional[str], config: JukeboxConfig) -> bool:
    """Apaga o post de fórum ou apaga a mensagem do painel da Jukebox. ``True`` se era um post de fórum."""
    if isinstance(getattr(channel, "parent", None), disnake.ForumChannel):
        await channel.delete(reason=f"{actor.id} redefiniu a Jukebox")
        return True

    original_message = await _fetch_configured_message(channel, config)

    if delete_channel == "sim":
        await channel.delete(reason=f"Jukebox redefinida por: {actor}")

    elif original_message:
        if original_message.thread:
            await original_message.thread.edit(archived=True, reason=f"Jukebox redefinida por {actor}.")
        await original_message.delete()

    return False


async def jukebox_reset(
    bot: BotCore,
    guild: disnake.Guild,
    actor: Any,
    channel,
    *,
    delete_channel: Optional[str] = None,
    channel_inter=None,
    config: JukeboxConfig,
    notify: Notify,
) -> None:
    """``/jukebox reset`` sob a trava de operação do servidor (nunca junto de um ``/jukebox setup``)."""
    async with exclusive_operation(bot, guild.id, config):
        await _jukebox_reset(
            bot,
            guild,
            actor,
            channel,
            delete_channel=delete_channel,
            channel_inter=channel_inter,
            config=config,
            notify=notify,
        )


async def _jukebox_reset(
    bot: BotCore,
    guild: disnake.Guild,
    actor: Any,
    channel,
    *,
    delete_channel: Optional[str] = None,
    channel_inter=None,
    config: JukeboxConfig,
    notify: Notify,
) -> None:
    """Passos do ``/jukebox reset`` depois de achar o canal: apaga o painel no Discord, limpa a seção ``jukebox`` e
    restaura as opções A e B para o padrão; as mensagens dos usuários e as playlists do servidor permanecem.
    """
    await ensure_panel_bot(bot, guild.id)

    try:
        was_forum = await _clear_in_discord(actor, channel, delete_channel, config)
    except Exception as exc:
        logger.error(
            "Redefinição da Jukebox: falha no Discord (servidor=%s, canal=%s): %s",
            guild.id,
            getattr(channel, "id", None),
            exc,
            exc_info=True,
        )
        raise GenericError(MSG_RESET_DISCORD_FAILED) from exc

    await bot.pool.servers.jukebox.clear(guild.id, expected=config.current)

    await notify("forum_deleted" if was_forum else "reset_done")

    await _reset_other_players(bot, guild.id, channel_inter)

    try:
        player: LavalinkPlayer = bot.music.players[guild.id]
    except KeyError:
        return

    await _reset_player(player, channel_inter)


reset_jukebox_channel = jukebox_reset
