"""Construtor único do select de playlists da Jukebox, público e efêmero."""

from collections.abc import Sequence

import disnake

from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.jukebox_select import SELECT_CAPACITY, plan
from empiremuse.rules.playlists import Playlist
from empiremuse.utils.server_playlists import safe_list, select_options

GUILD_PIN = "player_guild_pin"
GUILD_PIN_PAGE = "player_guild_pin_page"


def build_guild_pin(
    bot,
    tr: Translator,
    playlists: Sequence[Playlist],
    page: int = 1,
    fixed_entries: Sequence[disnake.SelectOption] = (),
    *,
    placeholder_key: str = "player.idle.server_songs_placeholder",
) -> disnake.ui.Select | None:
    if not playlists and not fixed_entries:
        return None
    layout = plan(len(playlists), len(fixed_entries), page)
    options = list(fixed_entries) if layout.page == 1 else []
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
    fixed_entries: Sequence[disnake.SelectOption] = (),
    *,
    placeholder_key: str = "player.idle.server_songs_placeholder",
) -> disnake.ui.Select | None:
    return build_guild_pin(
        bot, tr, await safe_list(bot, guild_id), page, fixed_entries, placeholder_key=placeholder_key
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
