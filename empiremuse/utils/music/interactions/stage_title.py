# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Union, Optional, TYPE_CHECKING

import disnake

from empiremuse.rules.errors import (
    STATUS_CHARS,
    STATUS_TOO_LONG,
    RuleError,
)
from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.limits import (
    VOICE_PLACEHOLDERS,
    VOICE_STATUS_MAX_LENGTH,
)
from empiremuse.rules.settings import validate_voice_status
from empiremuse.rules.voice_status import DEFAULT_VOICE_TEMPLATE, resolve_voice_status
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    VOICE_STATUS_PERMISSION,
    mention_channels,
    voice_status_blocked_channel,
)
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.music.models.player_voice import schedule_permanent_voice_status
from empiremuse.utils.others import (
    CustomContext,
)

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

from empiremuse.utils.music.interactions.i18n_views import guild_translator, sync_translator
from empiremuse.utils.music.interactions.view_modal import ViewModal

# Cada placeholder do status e a chave do texto que o explica (a ordem é a da lista mostrada ao usuário).
_PLACEHOLDER_KEYS = (
    ("{track.title}", "voice_status.placeholder.track_title"),
    ("{track.author}", "voice_status.placeholder.track_author"),
    ("{track.duration}", "voice_status.placeholder.track_duration"),
    ("{track.timestamp}", "voice_status.placeholder.track_timestamp"),
    ("{track.source}", "voice_status.placeholder.track_source"),
    ("{track.emoji}", "voice_status.placeholder.track_emoji"),
    ("{track.playlist}", "voice_status.placeholder.track_playlist"),
    ("{requester.name}", "voice_status.placeholder.requester_name"),
    ("{requester.id}", "voice_status.placeholder.requester_id"),
)
_ANSI_BLUE = "\x1b[34;1m"
_ANSI_RESET = "\x1b[0m"


def voice_status_error_text(tr: Translator, error: RuleError) -> str:
    """O texto de recusa de um modelo de status inválido (o mesmo no modal e no ``/setvoicestatus``)."""
    if error.code == STATUS_TOO_LONG:
        return tr("voice_status.error.too_long", max=VOICE_STATUS_MAX_LENGTH)
    if error.code == STATUS_CHARS:
        return tr("voice_status.error.chars")
    return tr("voice_status.error.placeholder")


def placeholder_text(tr: Translator) -> str:
    """A lista explicada dos placeholders (bloco ANSI), o exemplo e a nota; os códigos de escape ficam aqui, não no catálogo."""
    lines = "\n".join(f"{_ANSI_BLUE}{placeholder}{_ANSI_RESET} -> {tr(key)}" for placeholder, key in _PLACEHOLDER_KEYS)
    example = tr("voice_status.placeholder.example", title="{track.title}", author="{track.author}")
    return f"```ansi\n{lines}```\n{example}\n\n{tr('voice_status.placeholder.note')}"


class SetStageTitle(disnake.ui.View):
    placeholders = VOICE_PLACEHOLDERS

    placeholder_text = staticmethod(placeholder_text)

    def __init__(
        self,
        ctx: Union[CustomContext, disnake.Interaction],
        bot: BotCore,
        guild: disnake.Guild,
        data: dict,
        tr: Optional[Translator] = None,
    ):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.bot = bot
        self.data = data
        self.guild = guild
        self.message = None
        self.tr = tr or sync_translator(bot, guild)

        setstatus_btn = disnake.ui.Button(
            label=fit(self.tr("voice_status.button.current.label"), 80),
            emoji="🎶",
            style=disnake.ButtonStyle.grey,
            custom_id="status_voice_channel_temp_current",
        )
        setstatus_btn.callback = self.setstatus_callback
        self.add_item(setstatus_btn)

        setstatus_modal_btn = disnake.ui.Button(
            label=fit(self.tr("voice_status.button.toggle.label"), 80), emoji="🔊", style=disnake.ButtonStyle.grey
        )
        setstatus_modal_btn.callback = self.set_status_modal
        self.add_item(setstatus_modal_btn)

        setstatus_perm_btn = disnake.ui.Button(
            label=fit(self.tr("voice_status.button.permanent.label"), 80), emoji="💾", style=disnake.ButtonStyle.grey
        )
        setstatus_perm_btn.callback = self.set_status_perm
        self.add_item(setstatus_perm_btn)

    async def setstatus_callback(self, interaction: disnake.MessageInteraction):
        await self.modal_handler(
            inter=interaction, values={"status_voice_value": resolve_voice_status(self.data).template}
        )

    async def set_status_modal(self, interaction: disnake.MessageInteraction):
        await interaction.response.send_modal(
            ViewModal(
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=fit(self.tr("voice_status.modal.status.label"), 45),
                        custom_id="status_voice_value",
                        placeholder=fit(self.tr("voice_status.modal.status.placeholder"), 100),
                        max_length=VOICE_STATUS_MAX_LENGTH,
                        required=False,
                    ),
                ],
                view=self,
                title=fit(self.tr("voice_status.modal_title"), 45),
                custom_id="status_voice_channel_temp",
            )
        )

    async def set_status_perm(self, interaction: disnake.MessageInteraction):
        await interaction.response.send_modal(
            ViewModal(
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=fit(self.tr("voice_status.modal.permanent.label"), 45),
                        custom_id="status_voice_value",
                        placeholder=fit(self.tr("voice_status.modal.permanent.placeholder"), 100),
                        value=resolve_voice_status(self.data).template,
                        max_length=VOICE_STATUS_MAX_LENGTH,
                        required=False,
                    ),
                ],
                view=self,
                title=fit(self.tr("voice_status.modal_title"), 45),
                custom_id="status_voice_channel_perm",
            )
        )

    def build_embeds(self):
        embeds = []
        tr = self.tr

        color = self.bot.get_color(self.guild.me)
        cfg = resolve_voice_status(self.data)

        embeds.append(
            disnake.Embed(
                description=f"{tr('voice_status.embed.intro')}\n{placeholder_text(tr)}",
                color=color,
            )
        )

        model_note = f"\n{tr('voice_status.embed.default_note')}" if cfg.template == DEFAULT_VOICE_TEMPLATE else ""
        embeds.append(
            disnake.Embed(
                title=tr("voice_status.embed.current_model.title"),
                description=f"{cfg.template}{model_note}",
                color=color,
            )
        )

        state = tr("voice_status.embed.state_on" if cfg.permanent else "voice_status.embed.state_off")
        embeds.append(
            disnake.Embed(
                title=tr("voice_status.embed.permanent.title", state=state),
                description=tr(
                    "voice_status.embed.permanent.text",
                    requester_name="{requester.name}",
                    requester_id="{requester.id}",
                ),
                color=color,
            )
        )

        return embeds

    async def modal_handler(self, inter: disnake.ModalInteraction, values: dict = None):
        try:
            values = inter.text_values
        except AttributeError:
            pass

        tr = await guild_translator(self.bot, self.guild)
        status = validate_voice_status(values["status_voice_value"])

        if isinstance(status, RuleError):
            await inter.send(voice_status_error_text(tr, status), ephemeral=True)
            return

        values["status_voice_value"] = status

        if inter.data.custom_id == "status_voice_channel_perm":
            if not await self._save_permanent(inter, status, tr):
                return

        elif inter.data.custom_id.startswith("status_voice_channel_temp"):
            if not await self._apply_session(inter, status, tr):
                return

        else:
            await inter.send(tr("voice_status.not_implemented", custom_id=str(inter.data.custom_id)), ephemeral=True)
            return

        await self.close()
        self.stop()

    async def _save_permanent(self, inter: disnake.ModalInteraction, text: str, tr: Translator) -> bool:
        """Texto liga o permanente com esse modelo; vazio desliga e mantém o modelo. Um valor por servidor para todo o pool."""
        guild: Optional[disnake.Guild] = None

        for b in self.bot.pool.get_all_bots():
            if guild := b.get_guild(inter.guild_id):
                break

        if not guild:
            await inter.send(
                tr("voice_status.no_bots"),
                components=[
                    disnake.ui.Button(custom_id="bot_invite", label=fit(tr("voice_status.add_bots.label"), 80))
                ],
                ephemeral=True,
            )
            return False

        member = guild.get_member(inter.author.id)

        if member is None or not member.guild_permissions.manage_guild:  # falha fechado: sem o membro, nada é gravado
            await inter.send(tr("voice_status.no_permission_bold"), ephemeral=True)
            return False

        inter.author = member

        await inter.response.defer(ephemeral=True)

        # O documento é relido AGORA e só os dois campos mudam: o ``self.data`` é o de quando o menu abriu e o painel pode ter
        # mudado outra coisa (prefixo, skins) nesse meio tempo; gravar o antigo desfaria a mudança.
        data = await self.bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)
        data["voice_status_permanent"] = bool(text)
        if text:
            data["voice_channel_status"] = text
        await self.bot.update_global_data(inter.guild_id, data, db_name=DBModel.guilds)
        self.data.update(
            voice_status_permanent=data["voice_status_permanent"], voice_channel_status=data["voice_channel_status"]
        )

        cfg = resolve_voice_status(data)
        blocked_channels = []
        log_key = "voice_status.log.permanent_on" if cfg.permanent else "voice_status.log.permanent_off"

        for b in self.bot.pool.get_all_bots():
            try:
                p = b.music.players[inter.guild_id]
            except KeyError:
                continue

            # O modelo é configuração do servidor e fica salvo; o player sem a permissão só não liga o status.
            blocked = voice_status_blocked_channel(p.guild) if cfg.permanent else None
            if blocked:
                blocked_channels.append(blocked)
            else:
                p.set_command_log(text=tr(log_key, mention=str(inter.author.mention)), emoji="📢")
            p.update = True
            p.voice_status_saved_default = False  # agora há uma escolha salva: o aviso de padrão não vale mais

            schedule_permanent_voice_status(p, cfg)  # tarefa por player: o envio nunca espera a aplicação

        if blocked_channels:
            await inter.edit_original_message(
                tr(
                    "voice_status.permanent_blocked",
                    permission=str(VOICE_STATUS_PERMISSION),
                    channels=str(mention_channels(blocked_channels)),
                )
            )
        else:
            await inter.edit_original_message(
                tr("voice_status.permanent_saved" if cfg.permanent else "voice_status.permanent_disabled")
            )

        return True

    async def _apply_session(self, inter: disnake.ModalInteraction, text: str, tr: Translator) -> bool:
        """Liga (com ``text``) ou desliga o status só nesta sessão: volta ao permanente quando o bot sai do canal."""
        player: Optional[LavalinkPlayer] = None

        for bot in self.bot.pool.get_all_bots():
            try:
                candidate = bot.music.players[inter.guild_id]
            except KeyError:
                continue

            # Só vale o player em cujo canal o autor está: sem isso o último do laço ficava como achado.
            if inter.author.id in candidate.guild.me.voice.channel.voice_states:
                player = candidate
                break

        if not player:
            await inter.send(tr("voice_status.not_playing"), ephemeral=True)
            return False

        member = player.guild.get_member(inter.author.id)

        if member is None or not member.guild_permissions.manage_guild:
            await inter.send(tr("voice_status.no_permission"), ephemeral=True)
            return False

        inter.author = member

        if text and (blocked := voice_status_blocked_channel(player.guild)):
            await inter.send(
                tr(
                    "voice_status.missing_permission",
                    permission=str(VOICE_STATUS_PERMISSION),
                    mention=str(blocked.mention),
                ),
                ephemeral=True,
            )
            return False

        await inter.response.defer(ephemeral=True)

        if text:
            player.stage_title_event = True
            player.stage_title_template = text
            player.start_time = disnake.utils.utcnow()
            await player.update_stage_topic(force=True)
        else:
            await player.disable_voice_status()

        await player.process_save_queue()

        player.set_command_log(
            text=tr(
                "voice_status.log.session_on" if text else "voice_status.log.session_off",
                mention=str(inter.author.mention),
            ),
            emoji="📢",
        )

        player.update = True

        await inter.edit_original_message(tr("voice_status.session_set" if text else "voice_status.session_disabled"))

        return True

    async def on_timeout(self) -> None:
        await self.close()

    async def close(self):
        for c in self.children:
            c.disabled = True

        if isinstance(self.ctx, CustomContext):
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
        else:
            try:
                await self.ctx.edit_original_message(view=self)
            except Exception:
                pass

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.ctx.author.id:
            tr = await guild_translator(self.bot, self.guild)
            await inter.send(tr("voice_status.only_member", mention=str(self.ctx.author.mention)), ephemeral=True)
            return False
        return True
