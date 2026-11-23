"""Construtor único do select de playlists da Jukebox, público e efêmero."""

import logging
from collections.abc import Sequence

import disnake

from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.jukebox_select import SELECT_CAPACITY, plan
from empiremuse.rules.playlists import Playlist
from empiremuse.utils.music import jukebox_ui
from empiremuse.utils.server_playlists import safe_list, select_options

GUILD_PIN = "player_guild_pin"
GUILD_PIN_PAGE = "player_guild_pin_page"

logger = logging.getLogger(__name__)

RADIO_SLOTS = 2  # "📻 Rádios ao Vivo" e "🎙️ Rádios do Servidor": as vagas que a página 1 reserva quando o bot tem rádios


async def radio_entries(bot, tr: Translator, guild_id: int) -> tuple[disnake.SelectOption, ...]:
    """As entradas fixas do topo da página 1 (T-434 §3, A1): "📻 Rádios ao Vivo" e "🎙️ Rádios do Servidor" (J2b), só em servidor premium. Banco fora ou rádios desligadas:
    nenhuma (o select não perde vagas). O clique confere o premium de novo, sem cache."""
    hub = getattr(getattr(bot, "pool", None), "radio", None)
    if hub is None:
        return ()
    try:
        premium = await hub.premium.get(guild_id)
    except Exception as error:  # noqa: BLE001 - o select é desenhado sem a entrada; só o tipo vai ao log
        logger.warning("Premium não lido para o select da Jukebox (%s)", type(error).__name__)
        return ()
    if premium is None:
        return ()
    try:
        count = await hub.stations.count_for_guild(guild_id)
    except Exception as error:  # noqa: BLE001 - sem a contagem a entrada sai com 0; só o tipo vai ao log
        logger.warning("Estações do servidor não contadas para o select da Jukebox (%s)", type(error).__name__)
        count = 0
    return jukebox_ui.live_entry(tr), jukebox_ui.server_entry(tr, count)


def build_guild_pin(
    bot,
    tr: Translator,
    playlists: Sequence[Playlist],
    page: int = 1,
    fixed_entries: Sequence[disnake.SelectOption] = (),
    *,
    placeholder_key: str = "player.idle.server_songs_placeholder",
    show_fixed: bool = True,
    reserved: int | None = None,
) -> disnake.ui.Select | None:
    """``reserved`` (por padrão, quantas são as ``fixed_entries``) são as vagas da página 1 que entram na divisão de TODA página: a
    divisão das playlists nunca muda entre o select público e o efêmero. ``show_fixed`` só decide se as entradas aparecem."""
    if not playlists and not (fixed_entries and show_fixed):
        return None
    layout = plan(len(playlists), len(fixed_entries) if reserved is None else max(reserved, len(fixed_entries)), page)
    options = list(fixed_entries) if layout.page == 1 and show_fixed else []
    if layout.previous:
        options.append(
            disnake.SelectOption(label=tr("jukebox.select.previous.label"), value=f"jb:page:{layout.previous}")
        )
    for option in select_options(bot, tuple(playlists[layout.start : layout.stop]), limit=SELECT_CAPACITY):
        option.value = f"pl:{option.value}"
        options.append(option)
    if layout.next:
        options.append(disnake.SelectOption(label=tr("jukebox.select.next.label"), value=f"jb:page:{layout.next}"))
    placeholder = (
        tr("jukebox.select.page.placeholder", p=layout.page, t=layout.pages)
        if layout.pages > 1
        else tr(placeholder_key, p=layout.page, t=layout.pages)
    )
    return disnake.ui.Select(
        placeholder=fit(placeholder, 150),
        options=options,
        custom_id=GUILD_PIN,
        min_values=0,
        max_values=1,
        required=False,
    )


async def load_guild_pin(
    bot,
    tr: Translator,
    guild_id: int,
    page: int = 1,
    *,
    radio: bool = True,
    placeholder_key: str = "player.idle.server_songs_placeholder",
) -> disnake.ui.Select | None:
    """O select do canal com as playlists do servidor; ``radio`` mostra as entradas fixas (só a página 1 as mostra; o efêmero de página não).

    A vaga das entradas na página 1 entra na divisão de todas as páginas sempre que o bot tem rádios, mesmo sem premium ou com o banco
    fora nesse clique: a divisão não muda de uma página para a outra, e a navegação nunca pula nem repete uma playlist."""
    fixed = await radio_entries(bot, tr, guild_id)
    reserved = RADIO_SLOTS if getattr(getattr(bot, "pool", None), "radio", None) is not None else 0
    return build_guild_pin(
        bot,
        tr,
        await safe_list(bot, guild_id),
        page,
        fixed,
        placeholder_key=placeholder_key,
        show_fixed=radio,
        reserved=reserved,
    )


def replace_guild_pin(message, select: disnake.ui.Select | None) -> list[disnake.ui.ActionRow]:
    """Substitui só o select público, preservando botões e os demais selects do painel."""
    rows = disnake.ui.ActionRow.rows_from_message(message)
    for row in rows:
        for item in list(row.children):
            if getattr(item, "custom_id", None) == GUILD_PIN:
                row.remove_item(item)
                if select is not None:
                    row.append_item(select)
    return [row for row in rows if row.children]
