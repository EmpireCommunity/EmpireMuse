"""Interações de vinculação e preferências do Last.fm."""

from __future__ import annotations

import asyncio
import datetime
from typing import Optional

import aiohttp
import disnake
from disnake.ext import commands

from empiremuse.utils.music.converters import md_link
from empiremuse.utils.music.guild_text import guild_text
from empiremuse.utils.music.lastfm_tools import LastFmException
from empiremuse.utils.user_docs import change_user_doc, toggle_lastfm_scrobble

AUTH_TOKEN_TIMEOUT_S = 15


class LastFMView(disnake.ui.View):
    def __init__(self, ctx, session_key: str, scrobble: bool = True):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.interaction: Optional[disnake.MessageInteraction] = None
        self.session_key = ""
        self.scrobble_enabled = scrobble
        self.username = ""
        self.token = ""
        self.last_timestamp = None
        self.auth_url = None
        self.skg = None
        self.network = None
        self.clear_session = False
        self.check_loop = None
        self.auth_lock = asyncio.Lock()
        self.error = None
        self.account_linked = False
        self.session_key = session_key
        self.cooldown_scrobble_btn = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.user)
        self.build_components()

    def build_components(self):
        if self.session_key:
            btn1 = disnake.ui.Button(label=f"{'Desativar' if self.scrobble_enabled else 'Ativar'} scrobble/registro")
            btn1.callback = self.scrobble_enable
            self.add_item(btn1)

            btn2 = disnake.ui.Button(label="Desvincular conta do last.fm", style=disnake.ButtonStyle.red)
            btn2.callback = self.disconnect_account
            self.add_item(btn2)

        else:
            btn3 = disnake.ui.Button(label="Vincular conta do last.fm")
            btn3.callback = self.send_authurl_callback
            self.add_item(btn3)

    async def check_session_loop(self):
        count = 15

        while count > 0:
            await asyncio.sleep(20)
            try:
                data = await self.ctx.bot.last_fm.get_session_key(token=self.token)
            except Exception as e:
                self.error = e
                count -= 1
                continue
            self.session_key = data["session"]["key"]
            self.username = data["session"]["name"]
            self.account_linked = True
            self.scrobble_enabled = True
            self.stop()
            return

        self.auth_url = ""
        self.token = ""
        self.stop()

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.send(
                await guild_text(self.ctx.bot, interaction.guild, "legacy.lastfm.not_your_button"), ephemeral=True
            )
            return False
        return True

    async def scrobble_enable(self, interaction: disnake.MessageInteraction):
        if retry_after := self.cooldown_scrobble_btn.get_bucket(interaction).update_rate_limit():
            await interaction.send(
                await guild_text(self.ctx.bot, interaction.guild, "legacy.lastfm.cooldown", count=int(retry_after)),
                ephemeral=True,
            )
            return

        self.interaction = interaction
        bot = self.ctx.bot
        read_at = bot.pool.erasure_tombstones.now()  # antes de qualquer espera
        await interaction.response.defer()

        # relido e alterado sob o lock do usuário, o mesmo do apagamento de dados
        change = await change_user_doc(bot, interaction.author.id, toggle_lastfm_scrobble, read_at=read_at)
        if change.saved:
            lastfm = change.result
            # logo depois do lock, sem nenhuma espera no meio: um apagamento não entra entre a gravação e o cache
            bot.pool.lastfm_sessions[interaction.author.id] = dict(lastfm)
            self.session_key, self.username, self.scrobble_enabled = (
                lastfm["sessionkey"],
                lastfm["username"],
                lastfm["scrobble"],
            )
        else:  # dados apagados desde o clique, ou sem vínculo
            self.session_key = ""
            self.scrobble_enabled = False
        self.clear_items()
        self.build_components()
        await interaction.edit_original_message(view=self)

    async def disconnect_account(self, interaction: disnake.MessageInteraction):
        await interaction.response.defer()
        self.clear_session = True
        self.session_key = ""
        self.interaction = interaction
        self.stop()

    def stop(self):
        if self.check_loop is not None and self.check_loop is not asyncio.current_task():
            self.check_loop.cancel()
        super().stop()

    async def on_timeout(self):
        self.stop()

    async def send_authurl_callback(self, interaction: disnake.MessageInteraction):
        await interaction.response.defer(ephemeral=True)
        async with self.auth_lock:
            if self.is_finished():
                await self._auth_reply(interaction, "legacy.lastfm.auth_expired")
                return
            if not self.auth_url:
                try:
                    async with asyncio.timeout(AUTH_TOKEN_TIMEOUT_S):
                        self.token = await self.ctx.bot.last_fm.get_token()
                except TimeoutError, LastFmException, aiohttp.ClientError, aiohttp.ContentTypeError, KeyError:
                    await self._auth_reply(interaction, "legacy.lastfm.auth_retry")
                    return
                if self.is_finished():
                    await self._auth_reply(interaction, "legacy.lastfm.auth_expired")
                    return
                self.auth_url = (
                    f"https://www.last.fm/api/auth/?api_key={self.ctx.bot.last_fm.api_key}&token={self.token}"
                )
                self.last_timestamp = int((disnake.utils.utcnow() + datetime.timedelta(minutes=5)).timestamp())
            if self.check_loop is None or self.check_loop.done():
                self.check_loop = self.ctx.bot.loop.create_task(self.check_session_loop())

        await interaction.send(
            await guild_text(
                self.ctx.bot,
                interaction.guild,
                "legacy.lastfm.auth_prompt",
                link=md_link(
                    await guild_text(self.ctx.bot, interaction.guild, "legacy.lastfm.click_here"),
                    self.auth_url,
                    code=False,
                ),
                ts=self.last_timestamp,
            ),
            ephemeral=True,
            delete_after=300,
        )

    async def _auth_reply(self, interaction: disnake.MessageInteraction, key: str):
        await interaction.send(await guild_text(self.ctx.bot, interaction.guild, key), ephemeral=True)
