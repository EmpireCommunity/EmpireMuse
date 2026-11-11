# -*- coding: utf-8 -*-
"""Gerência das favoritas do SERVIDOR (``/radio favoritas``, J3): lista, Remover e Limpar com confirmação.

Só quem gerencia o servidor abre e age. O ator vem da interação (nunca de ``custom_id`` nem de valor de select) e o
servidor é o do comando; o gate é conferido ao abrir, de novo em cada clique e uma terceira vez pelo store
(``RadioFavorites.remove/clear``, contrato m4). A mensagem é efêmera e só mostra a lista do servidor: nenhuma favorita
pessoal passa por aqui. Nomes de estação saem pela função única de texto externo.

Gancho (cogs/radio.py, de quem é o comando): ``await open_server_favorites(inter, tr, self.bot.pool)``.
"""

from __future__ import annotations

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.text import external_text
from empiremuse.utils.others import paginator
from empiremuse.utils.radio.favorite_refs import is_genre, ref_labels
from empiremuse.utils.radio.favorites import MAX_FAVORITES, FavoriteForbidden
from empiremuse.utils.radio.perms import can_manage_guild
from empiremuse.utils.radio.premium import RadioUnavailable

VIEW_TIMEOUT_S = 180
LABEL_LIMIT = 100
PLACEHOLDER_LIMIT = 150
TEXT_NAME_LIMIT = 40
TEXT_LIST_MAX_SIZE = 1400
GENRE_EMOJI, CUSTOM_EMOJI = "🎼", "🎛️"
NO_MENTIONS = disnake.AllowedMentions.none()


async def open_server_favorites(inter: disnake.ApplicationCommandInteraction, tr, pool) -> None:
    """Abre a gerência numa mensagem efêmera; quem não gerencia o servidor recebe a recusa e nada mais."""
    guild_id = inter.guild_id
    if guild_id is None or not await can_manage_guild(pool, guild_id, inter.author.id):
        await inter.send(tr("radio.refused.no_manage_guild"), ephemeral=True)
        return
    try:
        stations, refs = await load_server_list(pool, tr, guild_id)
    except RadioUnavailable:
        await inter.send(tr("radio.owner.unavailable"), ephemeral=True)
        return
    view = ServerRadioFavoritesView(pool=pool, inter=inter, tr=tr, guild_id=guild_id, stations=stations, refs=refs)
    await inter.send(view.build_txt(), view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def load_server_list(pool, tr, guild_id: int):
    """As ao vivo e as ``custom``/``genre`` do servidor (``refs`` = ``[(id, rótulo)]``), na ordem de inserção de cada grupo."""
    stations, refs = await pool.radio.favorites.list_all(guild_id)
    return stations, await ref_labels(pool.radio, tr, guild_id, refs)


class ServerRadioFavoritesView(disnake.ui.View):
    def __init__(self, *, pool, inter, tr, guild_id: int, stations, refs=()) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_S)
        self.pool, self.inter, self.tr, self.guild_id = pool, inter, tr, guild_id
        self.author_id = inter.author.id
        self.stations = list(stations)
        self.refs: list[tuple[str, str]] = list(refs)
        self.current: str | None = None
        self.confirming = False
        self.log = ""

    # ------------------------------------------------------------------ texto e componentes

    def build_txt(self) -> str:
        tr = self.tr
        txt = tr("radio.favorites.server.heading", total=self.total, max=MAX_FAVORITES)
        if self.total:
            lines = [
                f"> ` {n:02} ` ⭐ {external_text(s.name, TEXT_NAME_LIMIT, markdown=False)}"
                for n, s in enumerate(self.stations, 1)
            ]
            lines += [
                f"> ` {n:02} ` {GENRE_EMOJI if is_genre(ref) else CUSTOM_EMOJI} {external_text(label, TEXT_NAME_LIMIT)}"
                for n, (ref, label) in enumerate(self.refs, len(self.stations) + 1)
            ]
            txt += paginator("\n".join(lines), max_size=TEXT_LIST_MAX_SIZE)[0] + "\n"
        else:
            txt += tr("radio.favorites.server.empty")
        if self.confirming:
            txt += tr("radio.favorites.server.clear_confirm", total=self.total)
        if self.log:
            txt += tr("fav.last_interaction", log=self.log)
        self.update_components()
        return txt

    def update_components(self) -> None:
        self.clear_items()
        if self.confirming:
            self._button(self.tr("radio.favorites.server.clear_confirm_button"), "✅", self.confirm_clear_callback)
            self._button(self.tr("radio.favorites.server.cancel.label"), "↩️", self.cancel_clear_callback)
            return
        if self.total:
            self._add_select()
        empty = not self.total
        self._button(self.tr("fav.remove.label"), "♻️", self.remove_callback, disabled=empty)
        self._button(self.tr("radio.favorites.server.clear.label"), "🚮", self.ask_clear_callback, disabled=empty)
        self._button(self.tr("fav.close.label"), "❌", self.close_callback)

    def _button(self, label: str, emoji: str, callback, **kwargs) -> None:
        button = disnake.ui.Button(label=fit(label, 80), emoji=emoji, **kwargs)
        button.callback = callback
        self.add_item(button)

    @property
    def total(self) -> int:
        return len(self.stations) + len(self.refs)

    def _entries(self) -> list[tuple[str, str, str]]:
        """``(valor, rótulo, emoji)`` de cada entrada: ao vivo (uuid) e depois ``custom``/``genre`` (id)."""
        live = [(s.uuid, external_text(s.name, LABEL_LIMIT, markdown=False) or "—", "⭐") for s in self.stations]
        return live + [(ref, label, GENRE_EMOJI if is_genre(ref) else CUSTOM_EMOJI) for ref, label in self.refs]

    def _add_select(self) -> None:
        entries = self._entries()[:MAX_FAVORITES]
        if self.current not in {value for value, _, _ in entries}:
            self.current = entries[0][0]
        options = [
            disnake.SelectOption(label=label, value=value, emoji=emoji, default=value == self.current)
            for value, label, emoji in entries
        ]
        select = disnake.ui.Select(
            options=options,
            min_values=1,
            max_values=1,
            placeholder=fit(self.tr("radio.favorites.server.placeholder"), PLACEHOLDER_LIMIT),
        )
        select.callback = self.select_callback
        self.add_item(select)

    # ------------------------------------------------------------------ callbacks

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.author_id:
            return True
        await inter.send(self.tr("fav.only_member", mention=f"<@{self.author_id}>"), ephemeral=True)
        return False

    async def select_callback(self, inter: disnake.MessageInteraction) -> None:
        self.current = inter.values[0]
        await inter.response.defer()

    async def remove_callback(self, inter: disnake.MessageInteraction) -> None:
        uuid = self.current
        if uuid is None:
            await inter.send(self.tr("fav.select_item"), ephemeral=True)
            return
        ref_names = dict(self.refs)
        is_ref = uuid in ref_names
        name = ref_names[uuid] if is_ref else next((s.name for s in self.stations if s.uuid == uuid), "")
        gone = is_ref and name == self.tr(
            "radio.favorites.ref.missing"
        )  # a estação já tinha sido apagada: sem nome para citar
        remove = (  # as ``custom``/``genre`` têm o próprio remover; os dois conferem o gate da gerência
            (lambda favorites: favorites.remove_ref(self.guild_id, inter.author.id, uuid))
            if is_ref
            else (lambda favorites: favorites.remove(self.guild_id, inter.author.id, uuid))
        )
        await self._change(
            inter,
            remove,
            "radio.favorites.log.ref_gone" if gone else "radio.favorites.server.removed",
            {} if gone else {"station": external_text(name, TEXT_NAME_LIMIT)},
        )

    async def ask_clear_callback(self, inter: disnake.MessageInteraction) -> None:
        await inter.response.defer()  # o gate pode levar até 5 s por bot e o Discord dá 3 s: responde antes de conferir
        if not await can_manage_guild(
            self.pool, self.guild_id, inter.author.id
        ):  # o gate no clique, não só ao desenhar
            await inter.followup.send(self.tr("radio.refused.no_manage_guild"), ephemeral=True)
            return
        self.confirming = True
        await inter.edit_original_message(content=self.build_txt(), view=self, allowed_mentions=NO_MENTIONS)

    async def cancel_clear_callback(self, inter: disnake.MessageInteraction) -> None:
        self.confirming = False
        await inter.response.edit_message(content=self.build_txt(), view=self, allowed_mentions=NO_MENTIONS)

    async def confirm_clear_callback(self, inter: disnake.MessageInteraction) -> None:
        self.confirming = False
        await self._change(
            inter,
            lambda favorites: favorites.clear(self.guild_id, inter.author.id),
            "radio.favorites.server.cleared",
            {},
        )

    async def close_callback(self, inter: disnake.MessageInteraction) -> None:
        await inter.response.edit_message(content=self.tr("fav.closed"), view=None)
        self.stop()

    async def _change(self, inter: disnake.MessageInteraction, operation, log_key: str, fmt: dict[str, str]) -> None:
        """Remove ou limpa como quem clicou. ``FavoriteForbidden`` e ``RadioUnavailable`` viram texto; nada vaza ao log."""
        favorites = self.pool.radio.favorites
        await inter.response.defer()  # antes do gate: ele pode passar dos 3 s que o Discord dá para a primeira resposta
        if not await can_manage_guild(
            self.pool, self.guild_id, inter.author.id
        ):  # o gate no clique, não só ao desenhar
            await inter.followup.send(self.tr("radio.refused.no_manage_guild"), ephemeral=True)
            return
        try:
            changed = await operation(favorites)
        except FavoriteForbidden:
            self.log = self.tr("radio.refused.no_manage_guild")
        except RadioUnavailable:
            self.log = self.tr("radio.owner.unavailable")
        else:
            self.log = self.tr(log_key, **fmt) if changed else self.tr("radio.favorites.nothing_changed")
            try:  # o aviso do que mudou vem ANTES de reler: uma releitura que falha não esconde uma mudança que aconteceu
                self.stations, self.refs = await load_server_list(self.pool, self.tr, self.guild_id)
            except RadioUnavailable:
                self.log += "\n" + self.tr("radio.owner.unavailable")
        await inter.edit_original_message(content=self.build_txt(), view=self, allowed_mentions=NO_MENTIONS)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        try:
            await self.inter.edit_original_message(view=self)
        except disnake.HTTPException:
            pass
        self.stop()
