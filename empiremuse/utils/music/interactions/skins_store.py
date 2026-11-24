"""Leitura e gravação do ``/skins`` (2.3, contrato S6 §5): as rádios do servidor, as listas do momento, o Salvar e o
redesenho dos painéis das rádios. A tela (``skins_view.py``) fica só com a UI; o rascunho puro está em ``skins_draft.py``.

- **Servidor sempre o da interação:** as rádios listadas são as de ``list_for_guild(guild_id)``; o ``radio_id`` é montado
  com o ``bot_id`` do valor e o ``guild_id`` da interação, e o ``set_look`` relê a rádio e confere o servidor (F-7).
- **Texto de fora** (nome de canal e de bot): ``label_text`` para select e placeholder, ``embed_text`` (markdown escapado)
  para embed e conteúdo.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from empiremuse.rules.radio import radio_id
from empiremuse.rules.settings import allowed_names
from empiremuse.rules.skins import CLASSIC_NORMAL, CLASSIC_STATIC, with_choice_marks
from empiremuse.rules.text import external_text
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.interactions.skins_draft import Choices, RadioRow, SavePlan, ordered_ids
from empiremuse.utils.music.skin_store import load_custom_skins
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

logger = logging.getLogger(__name__)

NAME_MAX = 60  # nomes de canal, bot e personalizada nos rótulos (unidades do ``external_text``)


def label_text(value: object) -> str:
    """Texto de fora para rótulo e placeholder de select (não renderizam markdown)."""
    return external_text(value, NAME_MAX, markdown=False)


def embed_text(value: object) -> str:
    """Texto de fora para embed e conteúdo de mensagem: o markdown escapado."""
    return external_text(value, NAME_MAX)


# ---------------------------------------------------------------------- leitura


async def radio_rows(bot: Any, guild: Any) -> tuple[RadioRow, ...]:
    """As rádios do servidor da interação, na ordem do ``/radio``. Rádio indisponível no processo = nenhuma."""
    try:
        radios = await bot.pool.radio.store.list_for_guild(guild.id)
    except RadioUnavailable, AttributeError:
        return ()
    names = {b.user.id: b.user.display_name for b in bot.pool.get_all_bots() if getattr(b, "user", None) is not None}
    rows = []
    for radio in radios:
        if radio.guild_id != guild.id:
            continue
        channel = guild.get_channel(radio.channel_id)
        channel_name = channel.name if channel is not None else str(radio.channel_id)
        bot_name = names.get(radio.bot_id, radio.bot_id)
        label = f"#{label_text(channel_name)} · {label_text(bot_name)}"
        text = f"#{embed_text(channel_name)} · {embed_text(bot_name)}"
        rows.append(RadioRow(radio.bot_id, label, radio.look, radio.rev, text))
    return tuple(rows)


async def load_choices(bot: Any, guild: Any, global_data: Mapping[str, Any], *, radio_allowed: bool) -> Choices:
    """As listas do momento: skins carregadas (menos as ignoradas), personalizadas do servidor e, com a permissão, as rádios."""
    normal = ordered_ids(allowed_names(bot.player_skins, bot.config["IGNORE_SKINS"]), CLASSIC_NORMAL)
    static = ordered_ids(allowed_names(bot.player_static_skins, bot.config["IGNORE_STATIC_SKINS"]), CLASSIC_STATIC)
    customs = await load_custom_skins(bot, guild.id, global_data)  # só as que o player carrega
    return Choices(
        normal,
        static,
        tuple(customs.normal),
        tuple(customs.static),
        await radio_rows(bot, guild) if radio_allowed else (),
    )


# ---------------------------------------------------------------------- gravação


async def save_skins(bot: Any, guild_id: int, plan: SavePlan, *, updated_by: str) -> tuple[int, ...]:
    """Grava o plano: uma escrita do documento global (relido agora, só os campos do plano), um ``patch`` do servidor e um
    ``set_look`` por rádio alterada (compare-and-set no ``rev`` que a tela leu). Devolve os ``bot_id`` das rádios que não
    gravaram (mudaram, sumiram ou não são deste servidor); as outras ficam gravadas."""
    if plan.global_fields:
        fresh = await bot.get_global_data(guild_id, db_name=DBModel.guilds)
        # a marca de escolha do 2.3 (``rules/skins.is_chosen``): a Clássica ``default`` escolhida aqui fica
        await bot.update_global_data(
            guild_id, {**fresh, **with_choice_marks(plan.global_fields)}, db_name=DBModel.guilds
        )
    if plan.server_fields:
        await bot.pool.servers.patch(guild_id, "settings", with_choice_marks(plan.server_fields))
    failed = []
    for item in plan.radio_looks:
        try:
            await bot.pool.radio.store.set_look(
                guild_id, radio_id(item.bot_id, guild_id), item.look, expected_rev=item.rev, updated_by=updated_by
            )
        except (RadioConflict, RadioNotFound, RadioInvalid, RadioUnavailable) as error:
            logger.info("Skin da rádio não gravada (%s)", type(error).__name__)
            failed.append(item.bot_id)
    return tuple(failed)


def redraw_radio_panels(pool: Any, guild_id: int, bot_ids: Sequence[int]) -> None:
    """Marca para redesenho o painel das rádios afetadas que rodam neste processo (a próxima janela de edição o refaz)."""
    manager = getattr(getattr(pool, "radio", None), "manager", None)
    if manager is None:
        return
    for bot_id in bot_ids:
        ports = getattr(manager.runner(radio_id(bot_id, guild_id)), "ports", None)
        redraw = getattr(ports, "refresh_panel", None)  # público nas duas portas (S7)
        if redraw is None:
            continue
        try:
            redraw()
        except Exception as error:  # noqa: BLE001 - o dado já foi gravado; o painel se refaz no próximo evento
            logger.warning("Falha ao marcar o painel da rádio (%s)", type(error).__name__)
