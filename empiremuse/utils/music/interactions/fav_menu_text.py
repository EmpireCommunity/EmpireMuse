# -*- coding: utf-8 -*-
from __future__ import annotations

from itertools import islice

import disnake

from empiremuse.utils.music.converters import md_code, md_link
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


def _format_playlist(bot, item):
    """Uma linha da lista do servidor: ordem, emoji da plataforma, ícone e nome (rótulo em code) e gêneros (texto externo em code)."""
    e = get_source_emoji_cfg(bot, item.url)
    genres = f" · {md_code(', '.join(item.genres))}" if item.genres else ""
    label = md_link(item.name, item.url, limit=40)
    if item.icon:
        label = f"{item.icon} {label}"
    return f"` {item.order:02} ` {e} {label}{genres}" if e else f"` {item.order:02} ` {label}{genres}"


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

        elif self.mode == ViewMode.guild_fav_manager:
            txt = self._guild_fav_manager_txt()

        elif self.mode == ViewMode.integrations_manager:
            txt = self._integrations_manager_txt(cmd)

        else:
            raise GenericError(f"**Modo não implementado:** {self.mode} | {type(self.mode)}")

        if self.log:
            txt += f"\n**Última interação:**\n{self.log}\n"

        if self.mode == ViewMode.integrations_manager:
            txt += f"\n**Links de perfis/canais suportados:**\n{', '.join(supported_platforms)}"

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
        return (
            "\n\n**Como usá-los?**\n"
            f"* Usando o comando {cmd} (selecionando o favorito no preenchimento automático da busca)\n"
            "* Clicando no botão/select de tocar favorito/integração do player.\n"
            f"* Usando o comando {self.prefix}{self.bot.get_cog('Music').play_legacy.name} sem incluir um nome ou link de uma música/vídeo.\n"
            "* Usando o botão de tocar favorito abaixo.\n"
        )

    def _fav_manager_txt(self, cmd: str) -> str:
        txt = "### Gerenciador de favoritos.\n"

        if not self.data["fav_links"]:
            txt += "Você não possui favoritos (clique no botão de adicionar abaixo).\n"

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

    def _guild_fav_manager_txt(self) -> str:
        txt = "### Gerenciador de playlists do servidor.\n"

        if not self.playlists:
            txt += "Não há playlists adicionadas no servidor (clique no botão de adicionar abaixo).\n"
            return txt

        txt += (
            "**Playlists do servidor (iguais para todos os bots):**\n"
            + paginator(
                "\n".join(f"> {_format_playlist(self.bot, item)}" for item in islice(self.playlists, MAX_LISTED_ITEMS)),
                max_size=LIST_MAX_SIZE,
            )[0]
        )

        txt += (
            "\n\n**Como usá-las?**\n"
            "* Usando o menu de seleção do player durante o modo de espera.\n"
            "* Usando o menu de favoritos do servidor no comando /play (com filtro por gênero)."
        )

        return txt

    def _integrations_manager_txt(self, cmd: str) -> str:
        txt = "### Gerenciador de integrações de canais/perfis com playlists públicas.\n"

        if not self.data["integration_links"]:
            txt += "**Você não possui integrações no momento (clique no botão de adicionar abaixo).**\n"
            return txt

        txt += (
            f"### Suas integrações atuais:\n"
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
