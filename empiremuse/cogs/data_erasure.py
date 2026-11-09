"""``/apagar_meus_dados`` (``/delete_my_data``): o membro apaga os próprios dados com UM clique de confirmação (§3.2 do contrato).

Também mora aqui o fluxo do titular para o pedido do dono (§3.4): a DM de concessão e o botão que só o titular aperta.

Regras que valem para as duas portas:

* O alvo vem SEMPRE de ``interaction.author.id`` (slash) ou da concessão resgatada pelo próprio titular (dono); nenhum parâmetro, ``custom_id``
  ou seleção escolhe o alvo. O clique do dono sozinho nunca apaga.
* O serviço (``pool.erasure.erase_user``) pega o lock do usuário sozinho: aqui ninguém segura ``pool.user_locks`` em volta dele.
* O serviço é lento (varre coleções): o clique reconhece a interação ANTES de chamá-lo (``edit_message`` com o texto de andamento).
* O membro NUNCA vê contagens nem texto de exceção: um texto fixo por desfecho. O texto de ``PARTIAL`` não promete que repetir resolve.

Textos: só as chaves ``erasure.*`` dos catálogos. A DM do titular (``erasure.owner.dm`` e os rótulos ``erasure.owner.dm_*``) sai no idioma de
reserva, porque antes do clique não há locale do titular: por isso esses três textos são bilíngues e iguais nos dois catálogos. Depois do
clique a interação traz o idioma do titular e todas as respostas passam a usá-lo.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Optional

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.rules.erasure import ErasureStatus
from empiremuse.rules.i18n import fit
from empiremuse.utils.erasure import ErasureBusy, ErasureRateLimited
from empiremuse.utils.erasure_grants import Grant, GrantLimited

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore, BotPool

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 60.0
OWNER_REQUEST_TIMEOUT_S = 60.0
BUTTON_LABEL_LIMIT = 80
SECONDS_PER_MINUTE = 60
ERASE_COMMAND_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.user)
GRANT_CONFIRM_ID = "erasure_grant"
GRANT_CANCEL_ID = "erasure_grant_cancel"
CONFIRM_ID = "erasure_confirm"
CANCEL_ID = "erasure_cancel"


def translator_for(pool: BotPool, locale: Any = None):
    """Tradutor do idioma do Discord de quem interage (sem consultar servidor: o comando também roda em DM)."""
    return pool.i18n.for_locale(pool.i18n.resolve(None, locale))


async def run_erasure(pool: BotPool, tr, user_id: int, *, grant: Optional[Grant] = None) -> str:
    """Chama o serviço e devolve o TEXTO fixo do desfecho (nunca contagem nem exceção).

    ``grant``: concessão do dono já resgatada pelo titular (``source="owner"``); é devolvida se nada foi apagado (limite ou serviço ocupado).
    """
    try:
        if grant is None:
            report = await pool.erasure.erase_user(user_id, source="slash")
        else:
            report = await pool.erasure.erase_user(
                user_id, source="owner", owner_id=grant.owner_id, grant_id=grant.grant_id
            )
    except ErasureRateLimited as error:
        _restore(pool, grant)
        return tr("erasure.rate_limited", minutes=max(1, math.ceil(error.retry_after_s / SECONDS_PER_MINUTE)))
    except ErasureBusy:
        _restore(pool, grant)
        return tr("erasure.busy")
    except Exception as error:  # noqa: BLE001 - só o tipo vai ao log; o membro vê um texto fixo
        logger.error("apagamento: falha inesperada na porta (%s)", type(error).__name__)
        return tr("erasure.failed")
    return tr("erasure.done") if report.status is ErasureStatus.OK else tr("erasure.partial")


def _restore(pool: BotPool, grant: Optional[Grant]) -> None:
    if grant is not None:
        pool.erasure_grants.restore(grant)


class ErasureConfirmView(disnake.ui.View):
    """Confirmação do TITULAR: slash (``grant is None``, ``source="slash"``) ou DM de concessão do dono (``grant`` dado, ``source="owner"``).

    Só ``holder_id`` clica. No slash o alvo é ``interaction.author.id`` (igual a ``holder_id``). No pedido do dono o alvo é o titular da
    concessão resgatada pelo clique: sem esse clique, nada é apagado.
    """

    def __init__(
        self,
        bot: BotCore,
        tr,
        holder_id: int,
        *,
        grant: Optional[Grant] = None,
        origin: Optional[disnake.Interaction] = None,
    ) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S if grant is None else None)
        self.bot, self.tr, self.holder_id, self.grant, self.origin = bot, tr, holder_id, grant, origin
        suffix = f":{grant.grant_id}" if grant else ""
        # Na DM do titular o idioma ainda é desconhecido: os rótulos são bilíngues (``erasure.owner.dm_*``); no slash, o idioma de quem pediu.
        confirm = disnake.ui.Button(
            label=fit(tr("erasure.owner.dm_confirm" if grant else "erasure.confirm"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.danger,
            custom_id=(GRANT_CONFIRM_ID if grant else CONFIRM_ID) + suffix,
        )
        cancel = disnake.ui.Button(
            label=fit(tr("erasure.owner.dm_cancel" if grant else "erasure.cancel"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.secondary,
            custom_id=(GRANT_CANCEL_ID if grant else CANCEL_ID) + suffix,
        )
        confirm.callback, cancel.callback = self.confirmed, self.cancelled
        self.add_item(confirm)
        self.add_item(cancel)

    def reply_translator(self, interaction: disnake.MessageInteraction):
        """Idioma do Discord de quem clicou: na DM do titular é o primeiro momento em que ele é conhecido."""
        return translator_for(self.bot.pool, interaction.locale)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.holder_id:
            await interaction.response.send_message(
                self.reply_translator(interaction)("erasure.not_yours"), ephemeral=True
            )
            return False
        return True

    async def cancelled(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        if self.grant is not None:
            self.bot.pool.erasure_grants.revoke(self.grant.grant_id)
        await interaction.response.edit_message(
            content=self.reply_translator(interaction)("erasure.cancelled"), view=None
        )

    async def confirmed(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        pool = self.bot.pool
        tr = self.reply_translator(interaction)
        grant = None
        if self.grant is not None:
            # Uso único, titular exato, 24 h: quem não é o titular nunca chega aqui (``interaction_check``) e a concessão se gasta só agora.
            grant = pool.erasure_grants.redeem(self.grant.grant_id, interaction.author.id)
            if grant is None:
                await interaction.response.edit_message(content=tr("erasure.grant_expired"), view=None)
                return
        # ``edit_message`` reconhece a interação (3 s) e tira os botões; a varredura do serviço vem depois, sem prazo de resposta.
        await interaction.response.edit_message(content=tr("erasure.running"), view=None)
        text = await run_erasure(pool, tr, interaction.author.id, grant=grant)
        await interaction.edit_original_message(content=text)

    async def on_timeout(self) -> None:
        self.stop()
        if self.origin is None:
            return
        try:
            await self.origin.edit_original_message(content=self.tr("erasure.cancelled"), view=None)
        except disnake.HTTPException as error:  # a mensagem pode já ter sido tratada ou o token expirado: só enfeite
            logger.debug("apagamento: não limpou a mensagem expirada (%s)", type(error).__name__)


class OwnerRequestView(disnake.ui.View):
    """Vista do DONO (§3.4 passos 2 e 3): mostra o usuário resolvido e, em *Enviar pedido*, manda a DM de concessão ao titular. NUNCA apaga."""

    def __init__(self, bot: BotCore, tr, owner_id: int, target: disnake.User) -> None:
        super().__init__(timeout=OWNER_REQUEST_TIMEOUT_S)
        self.bot, self.tr, self.owner_id, self.target = bot, tr, owner_id, target
        send = disnake.ui.Button(
            label=fit(tr("erasure.owner.send"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.primary,
            custom_id="erasure_owner_send",
        )
        cancel = disnake.ui.Button(
            label=fit(tr("erasure.cancel"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.secondary,
            custom_id="erasure_owner_cancel",
        )
        send.callback, cancel.callback = self.send_request, self.cancelled
        self.add_item(send)
        self.add_item(cancel)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.owner_id or interaction.author.id not in self.bot.env_owner_ids:
            await interaction.response.send_message(self.tr("erasure.not_yours"), ephemeral=True)
            return False
        return True

    async def cancelled(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        await interaction.response.edit_message(content=self.tr("erasure.owner.cancelled"), embed=None, view=None)

    async def send_request(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        pool = self.bot.pool
        try:
            grant = pool.erasure_grants.issue(self.owner_id, self.target.id)
        except GrantLimited as error:
            key = "erasure.owner.limit" if error.reason == "owner_limit" else "erasure.owner.pending"
            await interaction.response.edit_message(content=self.tr(key), embed=None, view=None)
            return
        await interaction.response.edit_message(content=self.tr("erasure.owner.sending"), embed=None, view=None)
        holder_tr = pool.i18n.for_locale(None)  # sem interação do titular não há idioma do Discord: usa o de reserva
        view = ErasureConfirmView(self.bot, holder_tr, self.target.id, grant=grant)
        try:
            await self.target.send(
                holder_tr("erasure.owner.dm"), view=view, allowed_mentions=disnake.AllowedMentions.none()
            )
        except disnake.HTTPException as error:  # DM fechada (Forbidden) ou falha de envio: nada pendente fica para trás
            pool.erasure_grants.revoke(grant.grant_id)
            view.stop()
            logger.warning("apagamento: DM de concessão não enviada (%s)", type(error).__name__)
            await interaction.edit_original_message(content=self.tr("erasure.owner.dm_closed"))
            return
        await interaction.edit_original_message(content=self.tr("erasure.owner.sent"))


class DataErasure(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot

    @commands.slash_command(
        name=localized("cmd.delete_my_data.name"),
        description=localized("cmd.delete_my_data.description"),
        extras={"allow_private": True},
        cooldown=ERASE_COMMAND_COOLDOWN,
    )
    @commands.contexts(guild=True, bot_dm=True)
    async def erase_my_data(self, inter: disnake.ApplicationCommandInteraction) -> None:
        # Resposta efêmera sem consulta lenta: a varredura só roda no clique de confirmação. Sem comando de prefixo (a mensagem seria pública).
        tr = translator_for(self.bot.pool, inter.locale)
        view = ErasureConfirmView(self.bot, tr, inter.author.id, origin=inter)
        await inter.response.send_message(tr("erasure.ask"), view=view, ephemeral=True)


def setup(bot: BotCore) -> None:
    bot.add_cog(DataErasure(bot))
