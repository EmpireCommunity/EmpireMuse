# -*- coding: utf-8 -*-
"""Etapas de `LavalinkPlayer.cleanup` (cancelar tarefas e encerrar a mensagem do player)."""

from __future__ import annotations

import logging
import datetime

import disnake

from empiremuse.utils.others import send_idle_embed

logger = logging.getLogger("empiremuse.player")

# Sessões mais novas que isso (segundos) encerram com um embed simples em vez do skin desativado.
SKIN_KEEP_MIN_SESSION_SECONDS = 15


def _cu_cancel_task_quietly(self, attr: str) -> None:
    try:
        getattr(self, attr).cancel()
    except Exception:
        pass


def _cu_cancel_tasks(self) -> None:
    for attr in (
        "reconnect_voice_channel_task",
        "members_timeout_task",
        "auto_skip_track_task",
        "_queue_updater_task",
    ):
        _cu_cancel_task_quietly(self, attr)

    for attr in ("message_updater_task", "_new_node_task", "idle_task"):
        _cu_cancel_task_quietly(self, attr)
        setattr(self, attr, None)


def _cu_voice_channel(self):
    try:
        return self.guild.voice_client.channel
    except Exception:
        return self.last_channel


def _cu_session_older_than_skin_grace(self) -> bool:
    started = datetime.datetime.fromtimestamp(self.uptime, datetime.timezone.utc)
    return (disnake.utils.utcnow() - started).total_seconds() > SKIN_KEEP_MIN_SESSION_SECONDS


async def _cu_static_session(self, inter: disnake.MessageInteraction = None) -> None:
    panel = getattr(self.bot.pool, "jukebox_panel", None)
    if panel is not None and panel.players(self.guild_id):
        # outros players seguem tocando no canal único: o painel se refaz com eles, em vez do embed ocioso por cima de todos
        if inter is not None:
            try:
                if not inter.response.is_done():
                    await inter.response.defer()
            except Exception:
                logger.exception("Falha em _cu_static_session")
        panel.forget(self.guild_id)
        await self.channel_cleanup()
        return

    try:
        await send_idle_embed(inter or self.message, self.command_log, bot=self.bot)
    except Exception:
        logger.exception("Falha em _cu_static_session")

    await self.channel_cleanup()


async def _cu_close_player_message(self, inter: disnake.MessageInteraction = None) -> bool:
    """Desativa a mensagem do player. Devolve True quando a limpeza deve terminar sem esvaziar a fila."""

    try:
        if self.has_thread or self.static:
            await _cu_close_thread_message(self, inter)

        elif inter:
            await _cu_close_message_from_interaction(self, inter)

        elif self.controller_mode is True:
            if not self.current:
                await self.destroy_message()
                return True

            await _cu_close_controller_message(self)

    except Exception:
        logger.exception("Falha em _cu_close_player_message")

    return False


def _cu_thread_message_editor(self, inter: disnake.MessageInteraction = None):
    try:
        if inter.message.id == self.message.id:
            return inter.response.edit_message
        return self.message.edit
    except AttributeError:
        try:
            return self.message.edit
        except AttributeError:
            return None


async def _cu_close_thread_message(self, inter: disnake.MessageInteraction = None) -> None:
    func = _cu_thread_message_editor(self, inter)

    if not func:
        return

    try:
        await func(
            allowed_mentions=self.allowed_mentions,
            **self.get_skin(disable=True)
            if (not self.static and _cu_session_older_than_skin_grace(self))
            else {"embed": disnake.Embed(description=self.command_log, color=self.bot.get_color(self.guild.me))},
        )
        await _cu_archive_jukebox_thread(self)
    except Exception:
        logger.exception("Falha ao arquivar thread do servidor")


async def _cu_archive_jukebox_thread(self) -> None:
    channel: disnake.Thread = self.bot.get_channel(self.message.id)

    if channel.parent.permissions_for(self.guild.me).send_messages_in_threads:
        try:
            await channel.send(
                embed=disnake.Embed(
                    color=self.bot.get_color(self.guild.me),
                    description="**A sessão de pedido de música da conversa atual foi encerrada.**",
                )
            )
        except Exception:
            pass

    if channel.owner.id == self.bot.user.id or channel.parent.permissions_for(self.guild.me).manage_threads:
        kwargs = {"archived": True, "locked": True}
    else:
        kwargs = {}

    await channel.edit(**kwargs)


def _cu_lyric_closing_kwargs(self, disabled_components) -> dict:
    """Embed de letra com o aviso de encerramento; vazio quando não há letra aberta na mensagem."""

    if not (self.lyric_embed and self.message and self.message.components):
        return {}

    self.lyric_embed.description += f"\n🛑 ⠂{self.command_log}"
    return {"embed": self.lyric_embed, "components": disabled_components()}


def _cu_disabled_message_components(self) -> list:
    components = []

    for component in self.message.components:
        for item in component.children:
            if isinstance(item, disnake.Button):
                item = disnake.ui.Button().from_component(item)
            elif isinstance(item, disnake.SelectMenu):
                item = disnake.ui.Select().from_component(item)
            item.disabled = True
            components.append(item)

    return components


def _cu_disabled_view_children(self) -> list:
    components = []

    for c in disnake.ui.View(timeout=0).from_message(self.message).children:
        c.disabled = True
        components.append(c)

    return components


async def _cu_close_message_from_interaction(self, inter: disnake.MessageInteraction) -> None:
    kw = _cu_lyric_closing_kwargs(self, lambda: _cu_disabled_message_components(self))

    if inter.message.id == self.message.id:
        func = inter.response.edit_message
        if not kw and _cu_session_older_than_skin_grace(self):
            kw = self.get_skin(disable=True)
            try:
                del kw["flags"]
            except Exception:
                pass
    else:
        try:
            func = self.message.edit
        except Exception:
            func = None

    if func:
        if not kw:
            kw = {
                "embed": disnake.Embed(description=f"🛑 ⠂{self.command_log}", color=self.bot.get_color(self.guild.me)),
                "view": None,
            }

        await func(**kw)


async def _cu_close_controller_message(self) -> None:
    kw = _cu_lyric_closing_kwargs(self, lambda: _cu_disabled_view_children(self))

    await self.message.edit(**kw or self.get_skin(disable=True))
