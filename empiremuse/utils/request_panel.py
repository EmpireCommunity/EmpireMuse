# -*- coding: utf-8 -*-
"""Painel do canal de pedidos único (item 11, P4): quem edita a mensagem, quantos players há e a edição agrupada por servidor.

O bot do painel é o AUTOR da mensagem e por isso o único que pode editá-la; os players dos outros bots a recebem já ligada ao bot do painel
(``fetch_panel_message``), então ``player.message.edit`` sai com o token certo. Com 0 players o painel é o embed ocioso; com 1 é a skin
estática desse player; com 2 ou mais é a lista (``interactions/request_panel.render_panel``). Edição do painel: no máximo uma por servidor a
cada 3 s, agrupada (várias mudanças viram uma só). Toda falha vai para o log; o painel é enfeite sobre uma lista já gravada.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.request_panel import PanelEntry, make_entry, refresh_wait_ms

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore, BotPool
    from empiremuse.utils.music.models import LavalinkPlayer

logger = logging.getLogger(__name__)

NO_MENTIONS = disnake.AllowedMentions.none()


def is_private(voice_channel: Any) -> bool:
    """A call é privada? ``@everyone`` não enxerga ou não entra: a faixa e quem pediu não aparecem no painel (S-V7)."""
    try:
        perms = voice_channel.permissions_for(voice_channel.guild.default_role)
    except AttributeError:
        return True
    return not (perms.view_channel and perms.connect)


def entry_for(bot: BotCore, player: LavalinkPlayer) -> Optional[PanelEntry]:
    """O bloco do painel para um player conectado; ``None`` se ele não está em call."""
    voice = getattr(getattr(player.guild.me, "voice", None), "channel", None)
    if voice is None:
        return None
    track = player.current
    requester = None
    if track is not None and getattr(track, "requester", None):
        member = player.guild.get_member(int(track.requester)) if str(track.requester).isdigit() else None
        requester = member.display_name if member else None
    return make_entry(
        bot_name=bot.user.display_name,
        bot_id=bot.user.id,
        voice_name=voice.name,
        private=is_private(voice),
        title=getattr(track, "title", None),
        url=getattr(track, "uri", None),
        paused=bool(getattr(player, "paused", False)),
        queue=len(player.queue),
        requester=requester,
    )


async def fetch_panel_message(pool: BotPool, guild_id: int) -> Optional[disnake.Message]:
    """A mensagem do painel vista PELO BOT DO PAINEL (o autor): só ela pode ser editada. ``None`` se não há canal ou mensagem."""
    request = pool.servers.request
    config = await request.get(guild_id)
    if not config.channel or not config.message_id:
        return None
    bot = await request.panel_bot(guild_id)
    if bot is None:
        return None
    channel = bot.get_channel(config.channel) or await bot.fetch_channel(config.channel)
    if isinstance(channel, disnake.Thread) and not isinstance(channel.parent, disnake.ForumChannel):
        return await channel.parent.fetch_message(config.message_id)
    return await channel.fetch_message(config.message_id)


class RequestPanel:
    def __init__(self, pool: BotPool) -> None:
        self.pool = pool
        self._last: dict[int, int] = {}
        self._pending: dict[int, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._handoff_running: set[int] = set()
        self._handoff_pending: set[int] = (
            set()
        )  # passagem pedida e ainda não concluída (falhou, ou foi pedida de novo durante a anterior)
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self._resend_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

    # ------------------------------------------------------------------ quem está tocando

    def players(self, guild_id: int) -> list[tuple[BotCore, LavalinkPlayer]]:
        """Os players estáticos (do canal de pedidos) do servidor, de todos os bots, na ordem do ``identifier``."""
        found = []
        for bot in sorted(self.pool.get_all_bots(), key=lambda b: b.identifier):
            player = bot.music.players.get(guild_id) if bot.user else None
            if player is not None and player.static and not player.is_closing:
                found.append((bot, player))
        return found

    def takes_over(self, guild_id: int) -> bool:
        """O painel inteiro (lista) assume a mensagem? Com 2 ou mais players, ou com 1 numa call privada (S-V7: a skin mostraria a faixa).

        Com 1 player numa call aberta vale a skin estática dele, como sempre foi.
        """
        players = self.players(guild_id)
        if len(players) >= 2:
            return True
        if not players:
            return False
        voice = getattr(getattr(players[0][1].guild.me, "voice", None), "channel", None)
        return voice is not None and is_private(voice)

    # ------------------------------------------------------------------ edição agrupada

    async def refresh(self, guild_id: int, *, now: bool = False) -> None:
        """Redesenha o painel do servidor, no máximo 1 vez a cada 3 s: pedidos no meio da janela viram UMA edição no fim dela."""
        wait = 0 if now else refresh_wait_ms(self._last.get(guild_id), int(time.monotonic() * 1000))
        if wait <= 0:
            await self._render(guild_id)
            return
        task = self._pending.get(guild_id)
        if task is None or task.done():
            self._pending[guild_id] = self._spawn(self._later(guild_id, wait / 1000), guild_id)

    def _spawn(self, coro, guild_id: Optional[int] = None) -> asyncio.Task:
        """Tarefa de fundo COM referência (o laço só guarda referência fraca) e removida quando termina."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if guild_id is not None and self._pending.get(guild_id) is finished:
                del self._pending[guild_id]

        task.add_done_callback(done)
        return task

    async def _later(self, guild_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._render(guild_id)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        return lock

    async def _render(self, guild_id: int) -> None:
        async with self._lock(guild_id):
            self._last[guild_id] = int(time.monotonic() * 1000)
            try:
                await self._draw(guild_id)
            except Exception as exc:  # noqa: BLE001 - enfeite sobre dados já gravados
                logger.warning("Painel do canal de pedidos não atualizado [%s] (%s)", guild_id, type(exc).__name__)

    async def _draw(self, guild_id: int) -> None:
        from empiremuse.utils.others import send_idle_embed  # tarde: utils.others importa muita coisa
        from empiremuse.utils.music.interactions.request_panel import render_panel
        from empiremuse.utils.server_playlists import load_select_options

        panel_bot = await self.pool.servers.request.panel_bot(guild_id)
        message = await fetch_panel_message(self.pool, guild_id)
        if panel_bot is None or message is None:
            return
        players = self.players(guild_id)
        if not players:
            await send_idle_embed(message, bot=panel_bot, force=True)
        elif not self.takes_over(guild_id):
            await players[0][1].invoke_np(force=True, panel_render=True)  # a skin estática do player, como sempre foi
        else:
            for _, player in players:
                player.last_data = {}  # o painel vira lista: ao voltar à skin, ela precisa ser redesenhada
            entries = [e for bot, player in players if (e := entry_for(bot, player)) is not None]
            payload = render_panel(
                panel_bot,
                message.guild,
                entries,
                tr=await self.pool.i18n.for_guild(message.guild),
                playlist_options=await load_select_options(panel_bot, guild_id),
            )
            await message.edit(allowed_mentions=NO_MENTIONS, **payload)

    async def resend(self, guild_id: int, *, replace_message_id: Optional[int] = None) -> Optional[disnake.Message]:
        """O BOT DO PAINEL envia um painel novo no canal de pedidos e grava o id (a mensagem anterior sumiu ou era de outro bot).

        ``replace_message_id``: a mensagem antiga (do bot que saiu); o novo painel tenta apagá-la, sem insistir se não puder.
        """
        from empiremuse.utils.others import send_idle_embed  # tarde: utils.others importa muita coisa

        lock = self._resend_locks.get(guild_id)
        if lock is None:
            lock = self._resend_locks[guild_id] = asyncio.Lock()
        async with lock:  # dois reenvios juntos mandariam dois painéis: o segundo encontra o do primeiro
            request = self.pool.servers.request
            config = await request.get(guild_id)
            bot = await request.panel_bot(guild_id)
            if not config.channel or bot is None:
                return None
            if replace_message_id is None:
                try:
                    existing = await fetch_panel_message(self.pool, guild_id)
                except disnake.HTTPException:
                    existing = None
                if existing is not None:
                    if existing.author.id == bot.user.id:
                        return existing  # já é do bot do painel: nada a reenviar
                    replace_message_id = existing.id  # é de outro bot (o painel anterior): o novo o substitui
            channel = bot.get_channel(config.channel) or await bot.fetch_channel(config.channel)
            message = await send_idle_embed(channel, bot=bot, force=True)
            try:
                await request.set(guild_id, config.channel, message.id, expected=config.current)
            except Exception:
                await message.delete()  # a configuração mudou no meio: não deixa painel órfão
                raise
        if replace_message_id and replace_message_id != message.id:
            try:
                await channel.get_partial_message(replace_message_id).delete()
            except disnake.HTTPException:
                pass
        return message

    async def on_roster_change(self, guild_id: int) -> None:
        """Um bot entrou ou saiu do servidor: reelege o bot do painel. Se a eleição mudou, ``elect`` já agenda a passagem do painel."""
        await self.pool.servers.request.elect(guild_id)

    async def handoff(self, guild_id: int) -> None:
        """A passagem do painel: garante que a mensagem é do bot do painel atual (senão ele manda uma nova e a antiga sai), religa os
        players a ela e redesenha. É o ÚNICO caminho depois de uma troca de ``panel_bot_id``, venha ela do ``/setup``, da entrada ou
        saída de um bot ou de quem chamou ``panel_bot``/``is_panel_bot`` primeiro: idempotente (painel já do bot = só religar)."""
        request = self.pool.servers.request
        message = await self.resend(guild_id)
        if message is None or (await request.get(guild_id)).message_id != message.id:
            return  # outro reenvio gravou uma mensagem mais nova durante este: religar à antiga deixaria os players numa apagada
        for _, player in self.players(guild_id):
            player.message = message  # a anterior foi apagada; os players passam a editar o painel novo
            player.last_data = {}
        await self.refresh(guild_id, now=True)

    def schedule_handoff(self, guild_id: int) -> None:
        """Agenda ``handoff`` em segundo plano (quem grava a eleição não espera o Discord). Pedido durante uma passagem em curso a repete."""
        self._handoff_pending.add(guild_id)
        if guild_id in self._handoff_running:
            return
        self._handoff_running.add(guild_id)
        try:
            self._spawn(self._handoff_quietly(guild_id))
        except RuntimeError:
            self._handoff_running.discard(guild_id)

    def retry_pending_handoff(self, guild_id: int) -> None:
        """Uma passagem que falhou fica pendente; quem passa pelo bot do painel (``panel_bot``/``is_panel_bot``) a retoma."""
        if guild_id in self._handoff_pending and guild_id not in self._handoff_running:
            self.schedule_handoff(guild_id)

    async def _handoff_quietly(self, guild_id: int) -> None:
        try:
            while guild_id in self._handoff_pending:
                self._handoff_pending.discard(guild_id)
                try:
                    await self.handoff(guild_id)
                except Exception as exc:  # noqa: BLE001 - o painel é enfeite sobre uma configuração já gravada
                    self._handoff_pending.add(
                        guild_id
                    )  # fica pendente: a próxima chamada de panel_bot/is_panel_bot tenta de novo
                    logger.warning(
                        "Painel do canal de pedidos não passado ao novo bot [%s] (%s)", guild_id, type(exc).__name__
                    )
                    return
        finally:
            self._handoff_running.discard(guild_id)

    def forget(self, guild_id: int) -> None:
        """Um player acabou ou saiu da call: o painel se refaz (volta ao ocioso, ou à skin do que sobrou)."""
        try:
            self._spawn(self.refresh(guild_id))
        except RuntimeError:
            return

    def close(self) -> None:
        """Cancela as tarefas de fundo do painel (encerramento do pool)."""
        for task in list(self._tasks):
            task.cancel()
