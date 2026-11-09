# -*- coding: utf-8 -*-
from __future__ import annotations

from itertools import islice

import disnake

from empiremuse.utils.music.converters import md_link
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.others import (
    get_source_emoji_cfg,
    paginator,
)
from empiremuse.utils.music.interactions.common import (
    ViewMode,
)

# Itens listados no texto do menu e tamanho máximo do bloco da lista.
MAX_LISTED_ITEMS = 22
LIST_MAX_SIZE = 1400


def _format_fav(bot, index, data):
    name, url = data
    e = get_source_emoji_cfg(bot, url)
    if e:
        return f"` {index:02} ` {e} {md_link(name, url, limit=40)}"
    return f"` {index:02} ` {md_link(name, url, limit=40)}"


def _format_itg(bot, index, data):
    name, url = data
    if isinstance(url, dict):
        url = url["url"]
    e = get_source_emoji_cfg(bot, url)
    if e:
        return f"` {index:02} ` {e} {md_link(name[5:], url, limit=40)}"
    return f"` {index:02} ` {md_link(name, url, limit=40)}"


class FavMenuTextMixin:
    """Texto da mensagem do gerenciador de favoritos, conforme o modo atual."""

    def build_txt(self):
        supported_platforms = self._supported_platforms()

        self.update_components()

        try:
            cmd = (
                f"</play:"
                + str(self.bot.get_global_command_named("play", cmd_type=disnake.ApplicationCommandType.chat_input).id)
                + ">"
            )
        except AttributeError:
            cmd = "/play"

        if self.mode == ViewMode.fav_manager:
            txt = self._fav_manager_txt(cmd)

        elif self.mode == ViewMode.integrations_manager:
            txt = self._integrations_manager_txt(cmd)

        elif self.mode == ViewMode.radio_favorites:
            txt = self.radio_manager_txt()

        else:
            raise GenericError(self.tr("fav.mode.unknown", mode=f"{self.mode} | {type(self.mode)}"))

        if self.log:
            txt += self.tr("fav.last_interaction", log=self.log)

        if self.mode == ViewMode.integrations_manager:
            txt += self.tr("fav.itg.supported", platforms=", ".join(supported_platforms))

        return txt

    def _supported_platforms(self) -> list:
        if self.mode != ViewMode.integrations_manager:
            return []

        supported_platforms = [
            md_link("Youtube", "https://www.youtube.com/"),
            md_link("Soundcloud", "https://soundcloud.com/"),
        ]

        if self.bot.spotify:
            supported_platforms.append(md_link("Spotify", "https://open.spotify.com/"))

        supported_platforms.append(md_link("Deezer", "https://www.deezer.com/"))

        return supported_platforms

    def _how_to_use_txt(self, cmd: str) -> str:
        return self.tr(
            "fav.how_to_use", cmd=cmd, prefix=str(self.prefix), command=self.bot.get_cog("Music").play_legacy.name
        )

    def _fav_manager_txt(self, cmd: str) -> str:
        txt = self.tr("fav.manager.heading")

        if not self.data["fav_links"]:
            txt += self.tr("fav.manager.empty")

        else:
            txt += paginator(
                "\n".join(
                    f"> {_format_fav(self.bot, n + 1, d)}"
                    for n, d in enumerate(islice(self.data["fav_links"].items(), MAX_LISTED_ITEMS))
                ),
                max_size=LIST_MAX_SIZE,
            )[0]

        if not self.light_mode:
            txt += self._how_to_use_txt(cmd)

        return txt

    def _integrations_manager_txt(self, cmd: str) -> str:
        txt = self.tr("fav.itg.manager.heading")

        if not self.data["integration_links"]:
            txt += self.tr("fav.itg.manager.empty")
            return txt

        txt += (
            self.tr("fav.itg.manager.current")
            + paginator(
                "\n".join(
                    f"> {_format_itg(self.bot, n + 1, d)}"
                    for n, d in enumerate(islice(self.data["integration_links"].items(), MAX_LISTED_ITEMS))
                ),
                max_size=LIST_MAX_SIZE,
            )[0]
        )

        if not self.light_mode:
            txt += self._how_to_use_txt(cmd)

        return txt
