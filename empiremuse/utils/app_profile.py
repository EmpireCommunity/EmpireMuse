# -*- coding: utf-8 -*-
"""Grava a descrição e as tags da aplicação do bot no Discord quando diferem do configurado (``APP_DESCRIPTION``/``APP_TAGS``).

Roda uma vez por bot por processo, depois do ready, com o ``appinfo`` que o bot já buscou. Uma tentativa só: qualquer falha (inclusive
429) vira um WARNING e a inicialização segue. O log traz só o tipo do erro e o status HTTP, nunca token, cabeçalho ou corpo.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Mapping

import disnake


from empiremuse.rules.app_profile import build_profile, panel_url_of, plan_changes

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

EDIT_TIMEOUT = 20  # s: o disnake espera o 429 por conta própria; passou disso, desiste sem travar a tarefa


def public_panel_url(config: Mapping[str, Any], *, now: float) -> str:
    """O link público do modo escolhido: externo validado, vazio quando inválido, ou genérico para hospedagem própria."""
    from empiremuse.api.settings import external_panel_requested, load_external_panel_settings
    from empiremuse.config_loader import resolve_port

    if external_panel_requested(config):
        try:
            settings = load_external_panel_settings(config, web_port=resolve_port(config.get("PORT")), now=now)
        except ValueError:
            return ""
        return settings.base_url if settings is not None else ""
    return panel_url_of(config.get("PANEL_BASE_URL"), config.get("PANEL_ENABLED"))


async def sync_app_profile(bot: BotCore, bots: int) -> None:
    """Compara o perfil atual da aplicação com o configurado e envia só os campos que mudaram."""
    try:
        await _sync(bot, bots)
    except disnake.HTTPException as exc:
        logger.warning("Perfil da aplicação do bot %s não atualizado (HTTP %s)", bot.identifier, exc.status)
    except Exception as exc:  # noqa: BLE001 - o perfil é enfeite: nada aqui derruba o bot
        logger.warning("Perfil da aplicação do bot %s não atualizado (%s)", bot.identifier, type(exc).__name__)


async def _sync(bot: BotCore, bots: int) -> None:
    appinfo = bot.appinfo
    if appinfo is None:
        return
    config = bot.config
    if bot.is_reserve:
        await bot.pool.wait_main_outcome()  # o texto da reserva cita o principal: espera ele logar (ou falhar)
    main = bot.pool.main_bot
    description = config["APP_DESCRIPTION_RESERVE"] if bot.is_reserve else config["APP_DESCRIPTION"]
    if bot.is_reserve and main is None:
        description = ""  # sem o principal o texto sairia sem as linhas dele: melhor deixar o que já está no Discord
    desired = build_profile(
        # a reserva tem o próprio texto (diz onde ficam os comandos de barra); o principal e os bots exclusivos usam o geral
        description=description,
        tags=config["APP_TAGS"],
        prefix=config["DEFAULT_PREFIX"],
        bots=bots,
        panel_url=public_panel_url(config, now=time.time()),
        support_url=config["SUPPORT_SERVER"],
        main=main.user.name if main is not None and main.user is not None else "",
    )
    for warning in desired.warnings:
        logger.warning("Perfil da aplicação do bot %s: %s", bot.identifier, warning)

    changes = plan_changes(appinfo.description, appinfo.tags, desired)
    if not changes:
        return

    await asyncio.wait_for(appinfo.edit(**changes), EDIT_TIMEOUT)
    logger.info("Perfil da aplicação do bot %s atualizado (%s)", bot.identifier, ", ".join(sorted(changes)))
