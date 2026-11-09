# -*- coding: utf-8 -*-
"""Confirmação, modal e painel do dono para banir e desbanir usuário (E5, MJ-E4, contrato §7).

Fora de ``cogs/`` de propósito (o cog e o painel as usam). Regras:

* Autoriza SÓ por ``bot.env_owner_ids`` (``OWNER_IDS`` do .env), NUNCA ``is_owner``: ``is_owner`` cai no dono ou na equipe da aplicação de
  cada bot do pool. A conferência vale na view (``interaction_check``) e no ``callback`` do modal; nada aqui herda ``OwnerOnlyView`` nem ``PanelView``.
* O ID passa por ``owner_target_id``; alvo dono, bot ou ID inválido é recusado; o dono vê o usuário resolvido (``render_text``,
  ``AllowedMentions.none``) e confirma (mesma lógica do pedido de apagamento).
* O motivo nunca é devolvido ao banido nem vai à auditoria (D4); só o dono o vê, escapado.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.erasure import owner_target_id
from empiremuse.rules.i18n import fit
from empiremuse.rules.usage import render_text
from empiremuse.rules.user_ban import (
    BAN_ACTION,
    REASON_LIMIT,
    TARGET_BOT,
    TARGET_OWNER,
    UNBAN_ACTION,
    ban_target_error,
)
from empiremuse.utils.erasure_views import _guarded
from empiremuse.utils.user_ban import FAILED_PANEL, FAILED_RPC, AlreadyBanned, UserBansUnavailable, user_bans_of

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 60.0
PANEL_TIMEOUT_S = 300.0
BUTTON_LABEL_LIMIT = 80
DISPLAY_NAME_LIMIT = 32
NONCE_BYTES = 6
CONFIRM_ID = "userban_confirm"
CANCEL_ID = "userban_cancel"
PANEL_BAN_ID = "userban_panel_ban"
PANEL_UNBAN_ID = "userban_panel_unban"
MODAL_ID = "userban_modal"
ID_FIELD = "userban_id"
REASON_FIELD = "userban_reason"
ID_MAX_LENGTH = 24
FAILED_KEYS = {FAILED_PANEL: "erasure.ban.owner.failed_sessions", FAILED_RPC: "erasure.ban.owner.failed_rpc"}


def is_env_owner(bot: BotCore, user_id: object) -> bool:
    """Dono por ``OWNER_IDS`` (MJ-E4). Nunca ``bot.is_owner``."""
    return user_id in bot.env_owner_ids


@dataclass(frozen=True)
class Prepared:
    """Pronto para o dono confirmar (``embed`` + ``view``) ou, sem ``view``, só o texto do erro."""

    content: Optional[str] = None
    embed: Optional[disnake.Embed] = None
    view: Optional[disnake.ui.View] = None


def _bot_ids(bot: BotCore) -> set[int]:
    return {b.user.id for b in bot.pool.get_all_bots() if getattr(b, "user", None) is not None}


async def prepare_confirmation(bot: BotCore, tr, owner_id: int, action: str, argument: str, reason: str) -> Prepared:
    """Valida o alvo, resolve o usuário e monta a confirmação; qualquer recusa volta como texto fixo (nunca exceção do Discord)."""
    target_id = owner_target_id(argument)
    error = ban_target_error(target_id, bot.env_owner_ids, _bot_ids(bot))
    if error is not None or target_id is None:
        key = {TARGET_OWNER: "erasure.ban.owner.target_owner", TARGET_BOT: "erasure.ban.owner.target_bot"}
        return Prepared(content=tr(key.get(error or "", "erasure.owner.invalid_id")))
    try:
        target = await bot.fetch_user(target_id)
    except disnake.NotFound:
        return Prepared(content=tr("erasure.owner.not_found"))
    except disnake.HTTPException as exc:
        logger.warning("banimento: fetch_user falhou (%s)", type(exc).__name__)
        return Prepared(content=tr("erasure.owner.lookup_failed"))
    if target.bot:
        return Prepared(content=tr("erasure.ban.owner.target_bot"))
    name = render_text(target.display_name, DISPLAY_NAME_LIMIT) or str(target.id)
    if action == BAN_ACTION:
        text = tr("erasure.ban.owner.ask", name=name, id=target.id)
        if reason:
            text += "\n" + tr("erasure.ban.owner.reason_line", reason=render_text(reason, REASON_LIMIT))
    else:
        text = tr("erasure.ban.owner.unban_ask", name=name, id=target.id)
    embed = disnake.Embed(description=text)
    embed.set_thumbnail(url=target.display_avatar.url)
    return Prepared(embed=embed, view=UserBanConfirmView(bot, tr, owner_id, action, target.id, name, reason))


class UserBanConfirmView(disnake.ui.View):
    """Confirma ou cancela. Só o dono que pediu, e só se ainda estiver em ``env_owner_ids``."""

    def __init__(self, bot: BotCore, tr, owner_id: int, action: str, target_id: int, name: str, reason: str) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.bot, self.tr, self.owner_id = bot, tr, owner_id
        self.action, self.target_id, self.name, self.reason = action, target_id, name, reason
        self.sent_message: Optional[disnake.Message] = None
        nonce = secrets.token_hex(NONCE_BYTES)
        label = "erasure.ban.owner.confirm" if action == BAN_ACTION else "erasure.ban.owner.unban_confirm"
        confirm = disnake.ui.Button(
            label=fit(tr(label), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.danger,
            custom_id=f"{CONFIRM_ID}:{nonce}",
        )
        cancel = disnake.ui.Button(
            label=fit(tr("erasure.cancel"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.secondary,
            custom_id=f"{CANCEL_ID}:{nonce}",
        )
        confirm.callback, cancel.callback = self.confirmed, self.cancelled
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.owner_id or not is_env_owner(self.bot, interaction.author.id):
            await interaction.response.send_message(self.tr("erasure.not_yours"), ephemeral=True)
            return False
        return True

    async def cancelled(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        await interaction.response.edit_message(content=self.tr("erasure.ban.owner.cancelled"), embed=None, view=None)

    async def on_timeout(self) -> None:
        self.stop()
        if self.sent_message is not None:
            try:
                await self.sent_message.edit(view=None)
            except disnake.HTTPException as exc:
                logger.debug("banimento: pedido expirado não limpo (%s)", type(exc).__name__)

    async def confirmed(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()  # uma confirmação só: o segundo clique não repete
        # Reconhece antes de gravar (3 s) e entrega o desfecho pela edição protegida, como o apagamento: PARTIAL e falha chegam ao dono.
        await _guarded(
            "aviso de andamento",
            interaction.response.edit_message(content=self.tr("erasure.ban.owner.working"), embed=None, view=None),
        )
        text = await self._apply()
        await _guarded("resposta final", interaction.edit_original_message(content=text))

    async def _apply(self) -> str:
        bans = user_bans_of(self.bot)
        if bans is None:
            return self.tr("erasure.ban.owner.unavailable")
        tr = self.tr
        try:
            if self.action == BAN_ACTION:
                result = await bans.ban(self.owner_id, self.target_id, self.reason)
                if result.failed:
                    failed = ", ".join(
                        tr(FAILED_KEYS.get(name, "erasure.ban.owner.failed_rpc")) for name in result.failed
                    )
                    return tr("erasure.ban.owner.partial", failed=failed)
                return tr("erasure.ban.owner.banned", name=self.name, id=self.target_id)
            if await bans.unban(self.owner_id, self.target_id):
                return tr("erasure.ban.owner.unbanned", name=self.name, id=self.target_id)
            return tr("erasure.ban.owner.not_banned")
        except AlreadyBanned:
            return tr("erasure.ban.owner.already")
        except UserBansUnavailable:
            return tr("erasure.ban.owner.unavailable")
        except Exception as exc:  # noqa: BLE001 - erro do banco: nada foi gravado (nunca só em memória); só o tipo vai ao log
            logger.warning("banimento: falha ao aplicar (%s)", type(exc).__name__)
            return tr("erasure.ban.owner.write_failed")


class UserBanModal(disnake.ui.Modal):
    """Pede o ID (e o motivo, no ban). A conferência de ``env_owner_ids`` é refeita no ``callback`` (o envio pode vir de outro contexto)."""

    def __init__(self, bot: BotCore, tr, action: str) -> None:
        self.bot, self.tr, self.action = bot, tr, action
        components = [
            disnake.ui.TextInput(
                label=fit(tr("erasure.ban.owner.modal_id"), 45),
                custom_id=ID_FIELD,
                style=disnake.TextInputStyle.short,
                min_length=1,
                max_length=ID_MAX_LENGTH,
            )
        ]
        if action == BAN_ACTION:
            components.append(
                disnake.ui.TextInput(
                    label=fit(tr("erasure.ban.owner.modal_reason"), 45),
                    custom_id=REASON_FIELD,
                    style=disnake.TextInputStyle.paragraph,
                    required=False,
                    placeholder=fit(tr("erasure.ban.owner.modal_reason_hint"), 100),
                    max_length=REASON_LIMIT,
                )
            )
        title = "erasure.ban.owner.modal_title" if action == BAN_ACTION else "erasure.ban.owner.modal_unban_title"
        super().__init__(
            title=fit(tr(title), 45), custom_id=f"{MODAL_ID}:{secrets.token_hex(NONCE_BYTES)}", components=components
        )

    async def callback(self, interaction: disnake.ModalInteraction) -> None:
        if not is_env_owner(self.bot, interaction.author.id):
            await interaction.response.send_message(self.tr("erasure.not_yours"), ephemeral=True)
            return
        values: dict[str, Any] = interaction.text_values
        prepared = await prepare_confirmation(
            self.bot,
            self.tr,
            interaction.author.id,
            self.action,
            str(values.get(ID_FIELD, "")).strip(),
            str(values.get(REASON_FIELD, "")),
        )
        # ``send_message`` não aceita ``None`` onde espera ``MISSING`` (``embed=None`` viraria ``embeds=[None]``): só os campos preenchidos.
        kwargs: dict[str, Any] = {"ephemeral": True, "allowed_mentions": disnake.AllowedMentions.none()}
        for name, value in (("content", prepared.content), ("embed", prepared.embed), ("view", prepared.view)):
            if value is not None:
                kwargs[name] = value
        await interaction.response.send_message(**kwargs)


class UserBanPanelView(disnake.ui.View):
    """Entrada do painel do dono: dois botões que abrem o modal. NÃO herda ``PanelView`` nem ``OwnerOnlyView`` (usam ``is_owner``)."""

    def __init__(self, bot: BotCore, tr, owner_id: int) -> None:
        super().__init__(timeout=PANEL_TIMEOUT_S)
        self.bot, self.tr, self.owner_id = bot, tr, owner_id
        nonce = secrets.token_hex(NONCE_BYTES)
        ban = disnake.ui.Button(
            label=fit(tr("erasure.ban.panel.ban"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.danger,
            custom_id=f"{PANEL_BAN_ID}:{nonce}",
        )
        unban = disnake.ui.Button(
            label=fit(tr("erasure.ban.panel.unban"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.secondary,
            custom_id=f"{PANEL_UNBAN_ID}:{nonce}",
        )
        ban.callback, unban.callback = self._open_ban, self._open_unban
        self.add_item(ban)
        self.add_item(unban)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.owner_id or not is_env_owner(self.bot, interaction.author.id):
            await interaction.response.send_message(self.tr("erasure.not_yours"), ephemeral=True)
            return False
        return True

    async def _open_ban(self, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.send_modal(UserBanModal(self.bot, self.tr, BAN_ACTION))

    async def _open_unban(self, interaction: disnake.MessageInteraction) -> None:
        await interaction.response.send_modal(UserBanModal(self.bot, self.tr, UNBAN_ACTION))
