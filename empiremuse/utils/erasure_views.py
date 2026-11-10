"""Views e fluxo de confirmação do apagamento de dados (§3.2 e §3.4 do contrato).

Fora de ``cogs/`` de propósito: tanto o slash (``cogs/data_erasure.py``) quanto o comando do dono (``cogs/legacy_cmds.py``) as usam, e um
cog não importa outro (recarregar um deles deixaria a classe velha na memória do outro).

Regras que valem para as duas portas:

* O alvo vem SEMPRE de ``interaction.author.id`` (slash) ou da concessão resgatada pelo próprio titular (dono); nenhum parâmetro, ``custom_id``
  ou seleção escolhe o alvo. O clique do dono sozinho nunca apaga.
* O serviço (``pool.erasure.erase_user``) pega o lock do usuário sozinho: aqui ninguém segura ``pool.user_locks`` em volta dele.
* O serviço é lento (varre coleções): o clique reconhece a interação ANTES de chamá-lo (``edit_message`` com o texto de andamento).
* ``redeem`` da concessão é SÍNCRONO e vem antes do reconhecimento: é a guarda contra repetição (dois cliques seguidos não resgatam duas vezes).
* O membro NUNCA vê contagens nem texto de exceção: um texto fixo por desfecho. O texto de ``PARTIAL`` não promete que repetir resolve.
* Todo ``custom_id`` é único por invocação (``nonce`` ou o código da concessão): duas views sem ``message_id`` no ``ViewStore`` não se pisam.

Textos: só as chaves ``erasure.*`` dos catálogos. A DM do titular (``erasure.owner.dm`` e os rótulos ``erasure.owner.dm_*``) sai no idioma de
reserva, porque antes do clique não há locale do titular: por isso esses textos são bilíngues e iguais nos dois catálogos. Depois do clique a
interação traz o idioma do titular e todas as respostas passam a usá-lo.
"""

from __future__ import annotations

import logging
import math
import secrets
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.erasure import (  # noqa: F401 - CONFIRM_ID, CANCEL_ID e GRANT_PREFIX são reexportados (fonte única em rules)
    CANCEL_ID,
    CONFIRM_ID,
    GRANT_PREFIX,
    OWNER_GRANT_TTL_S,
    ErasureStatus,
    grant_log_ref,
)
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
NONCE_BYTES = 6
OWNER_SEND_ID = "erasure_owner_send"
OWNER_CANCEL_ID = "erasure_owner_cancel"


def translator_for(pool: BotPool, locale: Any = None):
    """Tradutor do idioma do Discord de quem interage (sem consultar servidor: o comando também roda em DM)."""
    return pool.i18n.for_locale(pool.i18n.resolve(None, locale))


async def run_erasure(pool: BotPool, tr, user_id: int, *, grant: Optional[Grant] = None) -> str:
    """Chama o serviço e devolve o TEXTO fixo do desfecho (nunca contagem nem exceção).

    ``grant``: concessão do dono já resgatada pelo titular (``source="owner"``). Ela NÃO volta se nada foi apagado (limite ou serviço
    ocupado): o titular é avisado (``erasure.owner.retry_holder``) e o dono emite outra.
    """
    try:
        if grant is None:
            report = await pool.erasure.erase_user(user_id, source="slash")
        else:
            report = await pool.erasure.erase_user(
                user_id, source="owner", owner_id=grant.owner_id, grant_id=grant.grant_id
            )
    except ErasureRateLimited as error:
        if grant is not None:
            return tr("erasure.owner.retry_holder")
        return tr("erasure.rate_limited", minutes=max(1, math.ceil(error.retry_after_s / SECONDS_PER_MINUTE)))
    except ErasureBusy:
        return tr("erasure.owner.retry_holder" if grant is not None else "erasure.busy")
    except Exception as error:  # noqa: BLE001 - só o tipo vai ao log; o membro vê um texto fixo
        logger.error("apagamento: falha inesperada na porta (%s)", type(error).__name__)
        return tr("erasure.failed")
    return tr("erasure.done") if report.status is ErasureStatus.OK else tr("erasure.partial")


async def guarded(label: str, work: Any, *, domain: str = "apagamento") -> bool:
    """Espera um envio ou edição de enfeite/resposta final: falha vira log do TIPO (a interação ou a mensagem pode ter expirado).

    ``domain`` é o prefixo do log (cada fluxo usa o seu: ``apagamento``, ``banimento``).
    """
    try:
        await work
    except Exception as error:  # noqa: BLE001 - nada aqui pode quebrar o fluxo nem vazar texto de exceção
        logger.warning("%s: %s falhou (%s)", domain, label, type(error).__name__)
        return False
    return True


async def clear_buttons(message: Optional[disnake.Message], label: str) -> None:
    """Tira os botões de uma mensagem que expirou (a view já está parada)."""
    if message is not None:
        await guarded(label, message.edit(view=None))


class ErasureConfirmView(disnake.ui.View):
    """Confirmação do TITULAR: slash (``grant is None``, ``source="slash"``) ou DM de concessão do dono (``grant`` dado, ``source="owner"``).

    Só ``holder_id`` clica. No slash o alvo é ``interaction.author.id`` (igual a ``holder_id``). No pedido do dono o alvo é o titular da
    concessão resgatada pelo clique: sem esse clique, nada é apagado. Na DM do titular a view vale ``OWNER_GRANT_TTL_S`` e, ao expirar,
    revoga a concessão e tira os botões (``dm_message``).
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
        super().__init__(timeout=CONFIRM_TIMEOUT_S if grant is None else OWNER_GRANT_TTL_S)
        self.bot, self.tr, self.holder_id, self.grant, self.origin = bot, tr, holder_id, grant, origin
        self.dm_message: Optional[disnake.Message] = None
        # Único por invocação: o código da concessão (DM) ou um ``nonce`` (slash). Dois membros nunca dividem ``custom_id``.
        tag = grant.grant_id if grant else secrets.token_hex(NONCE_BYTES)
        # Na DM do titular o idioma ainda é desconhecido: os rótulos são bilíngues (``erasure.owner.dm_*``); no slash, o idioma de quem pediu.
        confirm = disnake.ui.Button(
            label=fit(tr("erasure.owner.dm_confirm" if grant else "erasure.confirm"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.danger,
            custom_id=f"{GRANT_PREFIX if grant else CONFIRM_ID}:{tag}",
        )
        cancel = disnake.ui.Button(
            label=fit(tr("erasure.owner.dm_cancel" if grant else "erasure.cancel"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.secondary,
            custom_id=f"{GRANT_PREFIX + '_cancel' if grant else CANCEL_ID}:{tag}",
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
            # Uso único, titular exato, 24 h. SÍNCRONO e ANTES do reconhecimento de propósito: é a guarda contra repetição.
            grant = pool.erasure_grants.redeem(self.grant.grant_id, interaction.author.id)
            if grant is None:
                await interaction.response.edit_message(content=tr("erasure.grant_expired"), view=None)
                return
        # ``edit_message`` reconhece a interação (3 s) e tira os botões; a varredura do serviço vem depois, sem prazo de resposta.
        # Se o ack falhar, o clique é genuíno e a concessão já é do titular: segue para o serviço e entrega o desfecho pela edição protegida.
        await guarded("aviso de andamento", interaction.response.edit_message(content=tr("erasure.running"), view=None))
        text = await run_erasure(pool, tr, interaction.author.id, grant=grant)
        await guarded("resposta final", interaction.edit_original_message(content=text))

    async def on_timeout(self) -> None:
        self.stop()
        if self.grant is not None:
            self.bot.pool.erasure_grants.revoke(self.grant.grant_id)
            await clear_buttons(self.dm_message, "limpar a DM expirada")
        elif self.origin is not None:
            await guarded(
                "limpar a mensagem expirada",
                self.origin.edit_original_message(content=self.tr("erasure.cancelled"), view=None),
            )


class OwnerRequestView(disnake.ui.View):
    """Vista do DONO (§3.4 passos 2 e 3): mostra o usuário resolvido e, em *Enviar pedido*, manda a DM de concessão ao titular. NUNCA apaga."""

    def __init__(self, bot: BotCore, tr, owner_id: int, target: disnake.User) -> None:
        super().__init__(timeout=OWNER_REQUEST_TIMEOUT_S)
        self.bot, self.tr, self.owner_id, self.target = bot, tr, owner_id, target
        self.sent_message: Optional[disnake.Message] = None  # a mensagem do dono, para tirar os botões ao expirar
        nonce = secrets.token_hex(NONCE_BYTES)
        send = disnake.ui.Button(
            label=fit(tr("erasure.owner.send"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.primary,
            custom_id=f"{OWNER_SEND_ID}:{nonce}",
        )
        cancel = disnake.ui.Button(
            label=fit(tr("erasure.cancel"), BUTTON_LABEL_LIMIT),
            style=disnake.ButtonStyle.secondary,
            custom_id=f"{OWNER_CANCEL_ID}:{nonce}",
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

    async def on_timeout(self) -> None:
        self.stop()
        await clear_buttons(self.sent_message, "limpar o pedido expirado do dono")

    async def send_request(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        pool = self.bot.pool
        try:
            grant = pool.erasure_grants.issue(self.owner_id, self.target.id)
        except GrantLimited as error:
            key = "erasure.owner.limit" if error.reason == "owner_limit" else "erasure.owner.pending"
            await interaction.response.edit_message(content=self.tr(key), embed=None, view=None)
            return
        # Trilha de auditoria da emissão (nunca agregada); o desfecho do apagamento tem a sua própria linha no serviço.
        logger.warning(
            "apagamento: pedido do dono emitido owner=%s target=%s grant=%s",
            self.owner_id,
            self.target.id,
            grant_log_ref(grant.grant_id),
            extra={"empiremuse_emit": True},
        )
        view: Optional[ErasureConfirmView] = None
        try:
            await interaction.response.edit_message(content=self.tr("erasure.owner.sending"), embed=None, view=None)
            holder_tr = pool.i18n.for_locale(
                None
            )  # sem interação do titular não há idioma do Discord: usa o de reserva
            view = ErasureConfirmView(self.bot, holder_tr, self.target.id, grant=grant)
            view.dm_message = await self.target.send(
                holder_tr("erasure.owner.dm"), view=view, allowed_mentions=disnake.AllowedMentions.none()
            )
        except disnake.Forbidden:  # DM fechada: nada pendente fica para trás
            key = "erasure.owner.dm_closed"
        except Exception as error:  # noqa: BLE001 - qualquer outra falha também revoga; só o tipo vai ao log
            logger.warning("apagamento: pedido do dono não enviado (%s)", type(error).__name__)
            key = "erasure.owner.send_failed"
        else:
            key = "erasure.owner.sent"
        if key != "erasure.owner.sent":
            pool.erasure_grants.revoke(grant.grant_id)
            if view is not None:
                view.stop()
        await guarded("aviso ao dono", interaction.edit_original_message(content=self.tr(key)))
