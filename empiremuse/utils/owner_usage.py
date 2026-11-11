# -*- coding: utf-8 -*-
"""Telas do dono no painel ``servers``: uso do servidor, banimento (com confirmação e motivo) e lista de banidos.

Todo componente novo passa pelo ``interaction_check`` de ``OwnerOnlyView`` (quem abriu o painel **e** dono do bot) e o modal do
motivo confere o dono de novo. Nome de servidor, canal e faixa é texto de terceiros: sempre por ``render_text`` na exibição, e toda
resposta leva ``allowed_mentions`` vazio.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import disnake

from empiremuse.rules.usage import (
    KIND_GUILD,
    KIND_PLAYBACK,
    MS_PER_SECOND,
    NAME_LIMIT,
    REASON_LIMIT,
    SUMMARY_DAYS,
    BanRecord,
    UsageSummary,
    clean_text,
    fit_lines,
    render_text,
    summarize,
)
from empiremuse.utils.guild_ban import AlreadyBanned, GuildBansUnavailable, bans_of
from empiremuse.utils.music.converters import time_format
from empiremuse.utils.usage import QUERY_LIMIT, usage_of

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

NO_MENTIONS = disnake.AllowedMentions.none()
FIELD_LIMIT = 1000  # teto do Discord para o valor de um campo é 1024
SELECT_LIMIT = 25
LIST_NOT_LOADED_MESSAGE = (
    "A lista de banidos não foi carregada (banco fora do ar); nada foi alterado. Tente de novo em instantes."
)
NOT_OWNER_TEXT = "Você não pode interagir aqui..."


def _stamp(ts_ms: int) -> str:
    return f"<t:{ts_ms // MS_PER_SECOND}:R>"


class OwnerOnlyView(disnake.ui.View):
    """View cujos componentes só respondem a quem abriu o painel e continua sendo dono do bot."""

    def __init__(self, gate_bot: BotCore, invoker_id: int, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.gate_bot = gate_bot
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: disnake.Interaction) -> bool:
        if interaction.author.id == self.invoker_id and await self.gate_bot.is_owner(interaction.author):
            return True
        await interaction.response.send_message(NOT_OWNER_TEXT, ephemeral=True)
        return False


# ---------------------------------------------------------------------- uso


def _event_line(event: dict) -> str:
    where = event.get("text_name") or event.get("voice_name")
    place = f" · {render_text(where, 40)}" if where else ""
    kind = event["kind"]
    if kind == KIND_PLAYBACK:
        title = render_text(event.get("title") or "?", 60)
        author = f" — {render_text(event['author'], 30)}" if event.get("author") else ""
        end = f" ({event['end']})" if event.get("end") else ""
        via = f" via `{render_text(event['via'], 24)}`" if event.get("via") else ""
        return f"{_stamp(event['ts'])}{place} · ▶ {title}{author}{end}{via}"
    name = render_text(event.get("name") or "?", 32)
    if kind == KIND_GUILD:
        return f"{_stamp(event['ts'])} · servidor: `{name}`"
    result = "" if event.get("ok", True) else f" ✖ {event.get('error', 'erro')}"
    return f"{_stamp(event['ts'])}{place} · `{kind}` {name}{result}"


def usage_embed(guild_id: int, guild_name: str, summary: UsageSummary, *, color: int) -> disnake.Embed:
    embed = disnake.Embed(
        color=color,
        title=f"Uso nos últimos {SUMMARY_DAYS} dias",
        description=f"**{render_text(guild_name, NAME_LIMIT)}** · `{guild_id}`",
    )
    if not summary.recent:
        embed.description += "\n\nNenhum registro de uso neste período."
        return embed

    last = _stamp(summary.last_ts) if summary.last_ts else "—"
    embed.add_field(
        name="Resumo",
        value=(
            f"Comandos: **{summary.commands}**\n"
            f"Faixas: **{summary.tracks}**\n"
            f"Tempo ouvido: **{time_format(summary.listened_seconds * MS_PER_SECOND, use_names=True) or '0 segundos'}**\n"
            f"Última atividade: {last}"
        ),
        inline=False,
    )
    for title, ranking in (("Canais de texto", summary.top_text), ("Canais de voz", summary.top_voice)):
        if ranking:
            lines = (f"{render_text(item.name, 40)} — {item.count}" for item in ranking)
            embed.add_field(name=title, value=fit_lines(lines, FIELD_LIMIT), inline=True)
    embed.add_field(
        name="Últimos eventos", value=fit_lines((_event_line(e) for e in summary.recent), FIELD_LIMIT), inline=False
    )
    return embed


async def send_usage(interaction: disnake.MessageInteraction, guild_id: int, guild_name: str, color: int) -> None:
    """Responde (efêmero) com o resumo de uso do servidor; o banco fora do ar vira uma mensagem simples."""
    await interaction.response.defer(ephemeral=True)
    usage = usage_of(interaction.bot)
    if usage is None:
        await interaction.followup.send("O registro de uso não está disponível.", ephemeral=True)
        return
    try:
        events = await usage.fetch_guild_events(guild_id)
    except Exception as exc:  # noqa: BLE001 - o painel do dono segue funcionando sem o registro
        logger.warning("Registro de uso não lido (%s)", type(exc).__name__)
        await interaction.followup.send("Não consegui ler o registro de uso agora.", ephemeral=True)
        return
    summary = summarize(events, now_ms=int(disnake.utils.utcnow().timestamp() * MS_PER_SECOND))
    embed = usage_embed(guild_id, guild_name, summary, color=color)
    if len(events) >= QUERY_LIMIT:
        embed.set_footer(text="Servidor com muitos eventos: os totais cobrem só os mais recentes.")
    await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)


# ---------------------------------------------------------------------- banimento


class BanReasonModal(disnake.ui.Modal):
    """Pede o motivo (opcional) e abre a confirmação; o dono é conferido de novo aqui (modal não tem ``interaction_check``)."""

    def __init__(self, gate_bot: BotCore, invoker_id: int, guild_id: int, guild_name: str, list_view: Optional[object]):
        self.gate_bot = gate_bot
        self.invoker_id = invoker_id
        self.guild_id = guild_id
        self.guild_name = clean_text(guild_name, NAME_LIMIT)
        self.list_view = list_view
        super().__init__(
            title="Banir servidor",
            custom_id=f"ban_reason_{guild_id}",
            components=[
                disnake.ui.TextInput(
                    label="Motivo (opcional)",
                    custom_id="reason",
                    style=disnake.TextInputStyle.paragraph,
                    required=False,
                    max_length=REASON_LIMIT,
                )
            ],
        )

    async def callback(self, interaction: disnake.ModalInteraction) -> None:
        if interaction.author.id != self.invoker_id or not await self.gate_bot.is_owner(interaction.author):
            await interaction.response.send_message(NOT_OWNER_TEXT, ephemeral=True)
            return
        reason = clean_text(interaction.text_values.get("reason"), REASON_LIMIT)
        view = BanConfirmView(self.gate_bot, self.invoker_id, self.guild_id, self.guild_name, reason, self.list_view)
        shown = f"\nMotivo: {render_text(reason, REASON_LIMIT)}" if reason else ""
        await interaction.response.send_message(
            f"Banir **{render_text(self.guild_name, NAME_LIMIT)}** (`{self.guild_id}`)?\n"
            f"Todos os bots saem agora e o servidor não consegue readicionar nenhum.{shown}",
            view=view,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )


class BanConfirmView(OwnerOnlyView):
    def __init__(
        self,
        gate_bot: BotCore,
        invoker_id: int,
        guild_id: int,
        guild_name: str,
        reason: str,
        list_view: Optional[object],
    ):
        super().__init__(gate_bot, invoker_id, timeout=120)
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.reason = reason
        self.list_view = list_view

    @disnake.ui.button(label="Confirmar banimento", style=disnake.ButtonStyle.danger)
    async def confirm(self, _button: disnake.ui.Button, interaction: disnake.MessageInteraction) -> None:
        bans = bans_of(self.gate_bot)
        await interaction.response.edit_message(content="Banindo...", view=None)
        if bans is None:
            await interaction.edit_original_response(content="A lista de banidos não está disponível.")
            return
        try:
            await bans.ban(self.guild_id, self.guild_name, self.reason)
        except AlreadyBanned:
            await interaction.edit_original_response(content="Esse servidor já está banido; nada foi alterado.")
            return
        except GuildBansUnavailable:
            await interaction.edit_original_response(content=LIST_NOT_LOADED_MESSAGE)
            return
        except Exception as exc:  # noqa: BLE001 - sem gravar, o bot não sai: o ban só vale persistido
            logger.warning("Banimento não gravado (%s)", type(exc).__name__)
            await interaction.edit_original_response(content="Não consegui gravar o banimento; nada foi feito.")
            return
        left = await bans.leave_everywhere(self.guild_id)
        await interaction.edit_original_response(
            content=f"Servidor banido. {left} bot(s) saíram.", allowed_mentions=NO_MENTIONS
        )
        if self.list_view is not None:
            await self.list_view.refresh_after_ban()

    @disnake.ui.button(label="Cancelar", style=disnake.ButtonStyle.secondary)
    async def cancel(self, _button: disnake.ui.Button, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.edit_message(content="Banimento cancelado.", view=None)
        self.stop()


# ---------------------------------------------------------------------- banidos


def banned_embed(records: list[BanRecord], *, color: int) -> disnake.Embed:
    embed = disnake.Embed(color=color, title="Servidores banidos")
    if not records:
        embed.description = "Nenhum servidor banido."
        return embed
    lines = []
    for record in records:
        reason = f" — {render_text(record.reason, 80)}" if record.reason else ""
        lines.append(
            f"**{render_text(record.name or record.guild_id, 50)}** (`{record.guild_id}`) · {_stamp(record.banned_at)}{reason}"
        )
    embed.description = fit_lines(lines, 3800)
    shown = embed.description.count("\n") + 1
    if len(records) > shown:
        embed.set_footer(text=f"+{len(records) - shown} não mostrados")
    return embed


class BannedListView(OwnerOnlyView):
    """Lista os banidos e desbane pelo select (o servidor precisa readicionar o bot por conta própria)."""

    def __init__(self, gate_bot: BotCore, invoker_id: int, color: int):
        super().__init__(gate_bot, invoker_id, timeout=300)
        self.color = color
        self.records = self._current()
        self._rebuild()

    def _current(self) -> list[BanRecord]:
        bans = bans_of(self.gate_bot)
        return bans.records() if bans is not None else []

    def _rebuild(self) -> None:
        self.clear_items()
        if not self.records:
            return
        options = [
            disnake.SelectOption(
                label=clean_text(record.name, NAME_LIMIT) or str(record.guild_id),
                value=str(record.guild_id),
                description=str(record.guild_id),
            )
            for record in self.records[:SELECT_LIMIT]
        ]
        select = disnake.ui.Select(placeholder="Desbanir um servidor...", options=options)
        select.callback = self.unban
        self.add_item(select)

    async def unban(self, interaction: disnake.MessageInteraction) -> None:
        bans = bans_of(self.gate_bot)
        guild_id = int(interaction.values[0])
        try:
            if bans is not None:
                await bans.unban(guild_id)
        except GuildBansUnavailable:
            await interaction.response.send_message(LIST_NOT_LOADED_MESSAGE, ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Desbanimento não gravado (%s)", type(exc).__name__)
            await interaction.response.send_message("Não consegui gravar o desbanimento.", ephemeral=True)
            return
        self.records = self._current()
        self._rebuild()
        await interaction.response.edit_message(
            embed=banned_embed(self.records, color=self.color), view=self if self.records else None
        )


async def send_banned(interaction: disnake.MessageInteraction, invoker_id: int, color: int) -> None:
    await interaction.response.defer(ephemeral=True)  # a carga do banco pode passar dos 3 s
    bans = bans_of(interaction.bot)
    if bans is not None:
        await bans.ensure_loaded()
    if bans is None or not bans.loaded:
        await interaction.followup.send("Não consegui carregar a lista de banidos agora.", ephemeral=True)
        return
    view = BannedListView(interaction.bot, invoker_id, color)
    await interaction.followup.send(
        embed=banned_embed(view.records, color=color),
        view=view if view.records else disnake.utils.MISSING,
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )
