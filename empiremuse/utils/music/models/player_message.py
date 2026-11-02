# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import itertools
import random
from itertools import cycle

import disnake

from empiremuse.utils.db import DBModel
from empiremuse.utils.music.converters import fix_characters, time_format, get_button_style
from empiremuse.utils.music.errors import (
    code_suffix,
    log_unexpected_error,
)
from empiremuse.utils.music.skin_utils import skin_converter
from empiremuse.utils.others import PlayerControls

from empiremuse.utils.music.models.helpers import (
    logger,
)
from empiremuse.utils.music.models.np_steps import (
    _np_guards,
    _np_data,
    _np_plain,
    _np_buttons_default,
    _np_buttons_extra,
    _np_interaction_edit,
    _np_message_edit,
    _np_send_new,
)
from empiremuse.utils.music.models.step_state import NpState


class PlayerMessageMixin:
    async def invoke_np(self, force=False, interaction=None, rpc_update=False, panel_render=False):
        if self.static and not panel_render and await self._panel_takes_over(interaction):
            return

        req = NpState(force=force, interaction=interaction, rpc_update=rpc_update)
        if await _np_guards(self, req):
            return

        if req.rpc_update:
            try:
                await self.process_rpc()
            except Exception:
                logger.exception("Falha em invoke_np")

        if self.retry_setup_hints:
            self.setup_hints()
            self.retry_setup_hints = False

        if await _np_data(self, req):
            return

        if not self.controller_mode:
            await _np_plain(self, req)

        else:
            # nenhum controle de botão foi definido na skin (será usado os botões padrões).
            if req.data.get("components") is None:
                # Aviso: Não modifique os components abaixo, prefira copiar uma das skins da pasta utils -> music -> skins
                # e deixá-la com outro nome (sem acentos, espaços, caracteres especiais) e modifique-as a seu gosto.
                # Caso queira deixar uma skin customizada por padrão adicione/modifique a config DEFAULT_SKIN="tuaskin"

                _np_buttons_default(self, req)

                _np_buttons_extra(self, req)

            self.updating = True

            if req.interaction:
                if await _np_interaction_edit(self, req):
                    return

            else:
                if self.message and (
                    self.ignore_np_once or self.has_thread or self.static or not req.force or self.is_last_message()
                ):
                    if await _np_message_edit(self, req):
                        return

            await self.destroy_message()

            await _np_send_new(self, req)

            self.start_message_updater_task()

        self.updating = False

    async def _panel_takes_over(self, interaction=None) -> bool:
        """Com 2+ players no canal da Jukebox único a mensagem é o painel em lista (``JukeboxPanel``), não a skin deste player."""
        panel = getattr(self.bot.pool, "jukebox_panel", None)
        if panel is None or not panel.takes_over(self.guild_id):
            return False
        if interaction is not None:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception:
                logger.exception("Falha em invoke_np")
        await panel.refresh(self.guild_id)
        return True

    async def message_updater(self):
        while True:
            if not self.text_channel or not self.controller_mode:
                pass

            elif self.auto_update and not self.current.is_stream:
                await asyncio.sleep(self.auto_update)

                try:
                    await self.invoke_np()
                except Exception:
                    logger.exception("Falha em message_updater")

                continue

            elif self.update:
                try:
                    await self.invoke_np()
                except Exception:
                    logger.exception("Falha em message_updater")

                self.update = False

                await asyncio.sleep(5)

            await asyncio.sleep(10)

    async def update_message(self, interaction: disnake.Interaction = None, force=False, rpc_update=False):
        if rpc_update:
            await self.process_rpc()

        if force or (interaction and not interaction.response.is_done()):
            if self.controller_mode or interaction:
                await self.invoke_np(interaction=interaction)

        else:
            self.update = True

    def start_message_updater_task(self):
        try:
            self.message_updater_task.cancel()
        except AttributeError:
            pass
        self.message_updater_task = self.bot.loop.create_task(self.message_updater())

    async def destroy_message(self):
        try:
            self.message_updater_task.cancel()
        except Exception:
            pass

        if self.static:
            return

        try:
            if self.guild.me:
                await self.message.delete()
        except Exception:
            pass

        self.message = None

    def is_last_message(self):
        try:
            return self.last_message_id == self.message.id
        except AttributeError:
            return

    def setup_hints(self):
        hints = list(self.initial_hints)

        if self.static:
            hints.append(self.tr("player.you_can_pin_songs_playlists", prefix_info=str(self.prefix_info)))

        elif self.bot.intents.message_content and self.controller_mode:
            hints.append(self.tr("player.creating_a_thread_on_the"))

        if len(self.bot.pool.get_guild_bots(self.guild.id)) > 1:
            bots_in_guild = 0
            bots_outside_guild = 0

            for b in self.bot.pool.get_guild_bots(self.guild.id):
                if b == self.bot:
                    continue

                try:
                    self.bot.user.id
                except AttributeError:
                    self.retry_setup_hints = True
                    continue

                if b.get_guild(self.guild.id):
                    bots_in_guild += 1
                else:
                    bots_outside_guild += 1

            if bots_in_guild:
                hints.append(
                    self.tr("player.hint.bots_in_guild", count=bots_in_guild, prefix_info=str(self.prefix_info))
                )

            elif bots_outside_guild:
                hints.append(
                    self.tr(
                        "player.hint.bots_outside_guild", count=bots_outside_guild, prefix_info=str(self.prefix_info)
                    )
                )

        if self.controller_mode:
            hints.append(self.tr("player.clicking_this_emoji_in_the"))

        random.shuffle(hints)
        self.hints = cycle(hints)

    def process_hint(self):
        if random.choice([x for x in range(self.hint_rate)]) == 0:
            self.current_hint = next(self.hints)
        else:
            self.current_hint = ""
