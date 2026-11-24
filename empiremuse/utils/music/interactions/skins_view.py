"""A tela do ``/skins`` (2.3, contrato S6 §5; spec §9.1 e §9.4): selects do player, o par das rádios, prévia e gravação.

- **Rascunho por invocação** (``skins_draft.Draft``): nada é gravado até o Salvar; Cancelar descarta; depois de
  ``VIEW_TIMEOUT_S`` os componentes desligam.
- **Leitura e gravação** (as rádios do servidor, as listas, o Salvar e o redesenho das rádios) ficam em ``skins_store.py``.
- **Permissão:** Gerenciar Servidor para tudo (conferida de novo no Salvar); o par das rádios só aparece, e só grava, com
  a permissão de quem configura rádio (``can_configure_radios``, a mesma do ``/radio``).
- **Prévia:** o asset ``preview`` do registro, anexado (PNG até 400 KiB, GIF até 2 MiB), com o rodapé ``skin.concept``
  enquanto a prévia for a imagem conceitual. Caminho fora de ``assets/``, arquivo ausente ou grande demais: sem imagem.
- **Rótulos com texto de fora** (nome de canal, de bot, de personalizada) passam por ``label_text`` (sem markdown) e, em
  embed, por ``embed_text`` (escapado).
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import disnake

from empiremuse.paths import PROJECT_DIR
from empiremuse.rules.i18n import Translator, fit
from empiremuse.rules.jukebox_select import plan as page_plan
from empiremuse.rules.skins import (
    CLASSIC_SKINS,
    FAMILY_CLASSIC,
    JUKEBOX_SKINS,
    RADIO_SKINS,
    Kind,
    SkinEntry,
    disc_motion_on,
    is_chosen,
    jukebox_skin,
    player_preview,
    radio_look,
    saved_player_skins,
)
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.interactions.skins_draft import (
    FOCUS_RADIO,
    FOLLOW,
    INVALID,
    MODE_NORMAL,
    MODE_STATIC,
    RADIO_PREFIX,
    TARGET_ALL,
    Choices,
    Draft,
    RadioRow,
    SavePlan,
    custom_value,
    player_value,
    radio_skin_value,
    stored_custom_name,
    target_value,
)
from empiremuse.utils.music.interactions.skins_store import embed_text, label_text

logger = logging.getLogger(__name__)

VIEW_TIMEOUT_S = 180
SELECT_MAX = 25
LABEL_MAX = 100
DESCRIPTION_MAX = 100
ALT_MAX = 1024
PREVIEW_MAX_BYTES = {".png": 400 * 1024, ".gif": 2 * 1024 * 1024}
PAGE_VALUE = "page:"
ASSETS_DIR = PROJECT_DIR / "assets"
CUSTOM_EMOJI = "💠"

_PLAYER_ENTRIES: Mapping[str, SkinEntry] = {entry.id: entry for entry in JUKEBOX_SKINS + CLASSIC_SKINS}
_RADIO_ENTRIES: Mapping[str, SkinEntry] = {entry.id: entry for entry in RADIO_SKINS}


# ---------------------------------------------------------------------- permissões


def can_manage_skins(member: Any) -> bool:
    """Gerenciar Servidor (como o ``/skin`` de antes); o dono e o administrador também passam por ela no Discord."""
    perms = getattr(member, "guild_permissions", None)
    return bool(perms is not None and (perms.manage_guild or perms.administrator))


def can_configure_radios(member: Any) -> bool:
    """A permissão de quem configura rádio (``/radio``, ``radio_commands.pick``: Gerenciar Servidor). Fica separada para
    valer a mais restrita das duas se a do ``/radio`` mudar (SK-12)."""
    perms = getattr(member, "guild_permissions", None)
    return bool(perms is not None and perms.manage_guild)


# ---------------------------------------------------------------------- nomes e prévias


def skin_name(tr: Translator, value: str, *, markdown: bool = True) -> str:
    """Nome de uma skin do player para mostrar: o do catálogo, o id (Clássicas) ou o nome da personalizada (escapado para
    embed e conteúdo; ``markdown=False`` só em select)."""
    custom = stored_custom_name(value)
    if custom is not None:
        return embed_text(custom) if markdown else label_text(custom)
    entry = _PLAYER_ENTRIES.get(value)
    return tr(entry.name_key) if entry is not None and entry.name_key else value


def radio_skin_name(tr: Translator, look: str) -> str:
    entry = _RADIO_ENTRIES.get(look)
    return tr(entry.name_key) if entry is not None else look


def preview_path(relative: str | None) -> Path | None:
    """O arquivo da prévia, só dentro de ``assets/``, PNG ou GIF e dentro do teto; senão ``None`` (a prévia sai sem imagem)."""
    if not relative:
        return None
    root = ASSETS_DIR.resolve()
    path = (ASSETS_DIR / relative).resolve()
    limit = PREVIEW_MAX_BYTES.get(path.suffix.lower())
    if limit is None or not path.is_relative_to(root) or not path.is_file():
        return None
    return path if path.stat().st_size <= limit else None


# ---------------------------------------------------------------------- rascunho inicial


def initial_draft(pool: Any, settings: Mapping[str, Any], global_data: Mapping[str, Any], choices: Choices) -> Draft:
    """O que a tela abre mostrando, com a mesma leitura do player (``saved_player_skins``/``jukebox_skin``, Q1 incluído)."""
    defaults = (pool.default_skin, pool.default_static_skin)
    server = (
        jukebox_skin(settings.get("skin"), defaults[0], is_chosen(settings, "skin")),
        jukebox_skin(settings.get("static_skin"), defaults[1], is_chosen(settings, "static_skin")),
    )
    glob_choice = saved_player_skins(settings, {**global_data, "global_skin": True})
    # ``saved_player_skins`` já aplicou o Q1 em cada nível: o que sobra é escolha
    glob = (jukebox_skin(glob_choice[0], defaults[0], True), jukebox_skin(glob_choice[1], defaults[1], True))
    return Draft(
        global_mode=bool(global_data.get("global_skin")),
        server=server,
        glob=glob,
        disc_motion=disc_motion_on(global_data.get("disc_motion")),
        radio_all=radio_look(None, global_data.get("radio_skin")),
        radio_own=tuple((row.bot_id, row.look) for row in choices.radios),
    )


def summary_lines(tr: Translator, plan: SavePlan, radios: Sequence[RadioRow]) -> list[str]:
    """As linhas do resumo depois do Salvar, só do que foi gravado."""
    lines = []
    fields = {**plan.global_fields, **plan.server_fields}
    for names, key in (
        (("skin", "player_skin"), "skin.select.normal"),
        (("static_skin", "player_skin_static"), "skin.select.static"),
    ):
        for name in names:
            if name in fields:
                lines.append(f"{tr(key)}: {skin_name(tr, str(fields[name]))}")
    if "global_skin" in fields:
        lines.append(tr("skin.global.on" if fields["global_skin"] else "skin.global.off"))
    if "disc_motion" in fields:
        lines.append(tr("skin.disc_motion.on" if fields["disc_motion"] else "skin.disc_motion.off"))
    if "radio_skin" in fields:
        lines.append(f"{tr('skin.radio.all')}: {radio_skin_name(tr, str(fields['radio_skin']))}")
    labels = {row.bot_id: row.text for row in radios}
    for item in plan.radio_looks:
        skin = tr("skin.radio.follow") if item.look is None else radio_skin_name(tr, item.look)
        lines.append(f"{labels.get(item.bot_id, item.bot_id)}: {skin}")
    return lines


# ---------------------------------------------------------------------- componentes


def _kind_emoji(bot: Any, entry: SkinEntry | None) -> str:
    return app_emoji(bot, "disc" if entry is not None and entry.kind in (Kind.IMAGE, Kind.NATIVE) else "comment")


def _player_description(tr: Translator, value: str, default: str) -> str:
    entry = _PLAYER_ENTRIES.get(value)
    if entry is None or entry.family == FAMILY_CLASSIC:
        parts = [tr("skin.classic")]
    else:
        parts = [tr(entry.desc_key)]
    if value == default:
        parts.insert(0, tr("skin.default"))
    return fit(" · ".join(parts), DESCRIPTION_MAX)


def player_options(
    bot: Any, tr: Translator, ids: Sequence[str], current: str, default: str
) -> list[disnake.SelectOption]:
    """As opções de um select do player: a personalizada atual (se houver) e as de fábrica, até 25."""
    options = []
    custom = stored_custom_name(current)
    if custom is not None:
        options.append(
            disnake.SelectOption(
                label=fit(label_text(custom), LABEL_MAX),
                value=custom_value(custom),
                description=fit(tr("skin.custom.desc"), DESCRIPTION_MAX),
                emoji=CUSTOM_EMOJI,
                default=True,
            )
        )
    for skin_id in ids:
        options.append(
            disnake.SelectOption(
                label=fit(skin_name(tr, skin_id, markdown=False), LABEL_MAX),
                value=skin_id,
                description=_player_description(tr, skin_id, default),
                emoji=_kind_emoji(bot, _PLAYER_ENTRIES.get(skin_id)),
                default=skin_id == current,
            )
        )
    return options[:SELECT_MAX]


def target_options(tr: Translator, draft: Draft, radios: Sequence[RadioRow]) -> list[disnake.SelectOption]:
    all_skin = radio_skin_name(tr, draft.radio_all)
    options = [
        disnake.SelectOption(
            label=fit(tr("skin.radio.all"), LABEL_MAX),
            value=TARGET_ALL,
            description=fit(tr("skin.radio.all_desc"), DESCRIPTION_MAX),
            default=draft.target == TARGET_ALL,
        )
    ]
    for row in radios[: SELECT_MAX - 1]:
        own = draft.own(row.bot_id)
        text = (
            tr("skin.radio.follows", skin=all_skin)
            if own is None
            else tr("skin.radio.own", skin=radio_skin_name(tr, own))
        )
        value = f"{RADIO_PREFIX}{row.bot_id}"
        options.append(
            disnake.SelectOption(
                label=fit(row.label, LABEL_MAX),
                value=value,
                description=fit(text, DESCRIPTION_MAX),
                default=draft.target == value,
            )
        )
    return options


def radio_skin_options(bot: Any, tr: Translator, draft: Draft) -> list[disnake.SelectOption]:
    shown = draft.shown_radio_skin()
    options = []
    if draft.target_bot() is not None:
        options.append(
            disnake.SelectOption(
                label=fit(tr("skin.radio.follow"), LABEL_MAX),
                value=FOLLOW,
                description=fit(radio_skin_name(tr, draft.radio_all), DESCRIPTION_MAX),
                default=shown is None,
            )
        )
    for entry in RADIO_SKINS:
        options.append(
            disnake.SelectOption(
                label=fit(tr(entry.name_key), LABEL_MAX),
                value=entry.id,
                description=fit(tr(entry.desc_key), DESCRIPTION_MAX),
                emoji=_kind_emoji(bot, entry),
                default=shown == entry.id,
            )
        )
    return options


def _target_label(tr: Translator, draft: Draft, radios: Sequence[RadioRow], *, markdown: bool) -> str:
    """O alvo das rádios: ``label`` no select, ``text`` (escapado) no embed."""
    bot_id = draft.target_bot()
    row = next((row for row in radios if row.bot_id == bot_id), None)
    if row is None:
        return tr("skin.radio.all")
    return row.text if markdown else row.label


# ---------------------------------------------------------------------- a view


class SkinsView(disnake.ui.View):
    """A mensagem do ``/skins``. Quem abriu é o único que interage; o resultado (``save``/``cancel``) e a interação do botão
    ficam em ``result`` e ``interaction`` para o comando gravar depois do ``wait()``."""

    def __init__(
        self,
        *,
        bot: Any,
        author_id: int,
        tr: Translator,
        choices: Choices,
        initial: Draft,
        colour: Any = None,
        footer: str | None = None,
        note: str | None = None,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_S)
        self.bot = bot
        self.author_id = author_id
        self.tr = tr
        self.choices = choices
        self.initial = initial
        self.draft = initial
        self.colour = colour
        self.footer = footer
        self.note = note  # uma linha a mais no topo (pelo prefixo: as rádios se configuram no ``/skins``)
        self.defaults = (bot.pool.default_skin, bot.pool.default_static_skin)
        self.result: str | None = None
        self.interaction: disnake.MessageInteraction | None = None
        self.message: Any = None
        self.rebuild()

    # ------------------------------------------------------------------ montagem

    def rebuild(self) -> None:
        self.clear_items()
        tr, draft = self.tr, self.draft
        for row, mode, key in ((0, MODE_NORMAL, "skin.select.normal"), (1, MODE_STATIC, "skin.select.static")):
            default = self.defaults[1 if mode == MODE_STATIC else 0]
            options = player_options(self.bot, tr, self.choices.ids(mode), draft.player(mode), default)
            self._select(row, fit(tr(key), LABEL_MAX), options, self._on_player(mode))
        if self.choices.radios:
            target = _target_label(tr, draft, self.choices.radios, markdown=False)
            self._select(
                2,
                fit(tr("skin.select.radio_target", target=target), LABEL_MAX),
                target_options(tr, draft, self.choices.radios),
                self._on_target,
            )
            skin = radio_skin_name(tr, draft.effective_radio_skin())
            self._select(
                3,
                fit(tr("skin.select.radio_skin", target=target, skin=skin), LABEL_MAX),
                radio_skin_options(self.bot, tr, draft),
                self._on_radio_skin,
            )
        self._buttons()

    def _select(self, row: int, placeholder: str, options: list[disnake.SelectOption], callback: Any) -> None:
        if not options:
            return
        select = disnake.ui.Select(placeholder=placeholder, options=options, min_values=1, max_values=1, row=row)
        select.callback = callback
        self.add_item(select)

    def _button(self, label: str, emoji: str, callback: Any, *, on: bool | None = None, disabled: bool = False) -> None:
        style = disnake.ButtonStyle.secondary
        if on is not None:
            style = disnake.ButtonStyle.success if on else disnake.ButtonStyle.secondary
        button = disnake.ui.Button(label=fit(label, 80), emoji=emoji, style=style, row=4, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def _buttons(self) -> None:
        tr, draft = self.tr, self.draft
        self._button(
            tr("skin.global.on" if draft.global_mode else "skin.global.off"),
            "🌐",
            self._on_global,
            on=draft.global_mode,
        )
        self._button(
            tr("skin.disc_motion.on" if draft.disc_motion else "skin.disc_motion.off"),
            "💿",
            self._on_disc,
            on=draft.disc_motion,
        )
        has_custom = bool(self.choices.custom_normal or self.choices.custom_static)
        self._button(tr("skin.select.custom"), CUSTOM_EMOJI, self._on_custom, disabled=not has_custom)
        self._button(tr("skin.save"), "💾", self._on_save)
        self._button(tr("skin.cancel"), "❌", self._on_cancel)

    def _preview(self) -> tuple[str, str, str | None, bool]:
        """(nome, linha do alvo e descrição, caminho relativo do asset, é conceito) da escolha em foco."""
        tr, draft = self.tr, self.draft
        if draft.focus == FOCUS_RADIO and self.choices.radios:
            look = draft.effective_radio_skin()
            entry = _RADIO_ENTRIES.get(look)
            scope = _target_label(tr, draft, self.choices.radios, markdown=True)
            desc = tr(entry.desc_key) if entry is not None else ""
            return (
                radio_skin_name(tr, look),
                f"{scope}\n{desc}",
                entry.preview if entry else None,
                bool(entry and entry.concept),
            )
        mode = MODE_STATIC if draft.focus == MODE_STATIC else MODE_NORMAL
        value = draft.player(mode)
        default = self.defaults[1 if mode == MODE_STATIC else 0]
        scope = tr("skin.select.static" if mode == MODE_STATIC else "skin.select.normal")
        entry = _PLAYER_ENTRIES.get(value)
        desc = (
            tr("skin.custom.desc") if stored_custom_name(value) is not None else _player_description(tr, value, default)
        )
        concept = bool(entry and entry.concept)
        return skin_name(tr, value), f"{scope}\n{desc}", player_preview(value, static=mode == MODE_STATIC), concept

    def payload(self) -> dict[str, Any]:
        """Os embeds (o seletor e a prévia), a view e o anexo da prévia, se houver."""
        tr = self.tr
        intro = f"{tr('skin.ui.intro')}\n{tr('skin.ui.global_on' if self.draft.global_mode else 'skin.ui.global_off')}"
        if self.note:
            intro += f"\n-# {self.note}"
        main = disnake.Embed(title=tr("skin.ui.title"), description=intro, colour=self.colour)
        if self.footer:
            main.set_footer(text=self.footer)
        name, text, relative, concept = self._preview()
        title = fit(tr("skin.preview.title", skin=name), 256)
        preview = disnake.Embed(title=title, description=text, colour=self.colour)
        data: dict[str, Any] = {"embeds": [main, preview], "view": self}
        path = preview_path(relative)
        if path is not None:
            filename = f"preview{path.suffix.lower()}"
            data["file"] = disnake.File(path, filename=filename, description=fit(title, ALT_MAX))
            preview.set_image(url=f"attachment://{filename}")
            if concept:
                preview.set_footer(text=tr("skin.concept"))
        return data

    async def refresh(self, inter: disnake.MessageInteraction | None = None) -> None:
        """Mostra o rascunho: responde à interação do componente, ou edita a mensagem (escolha feita na das personalizadas).
        Com a tela encerrada (Salvar, Cancelar ou o tempo), não edita: religaria componentes que ninguém escuta mais."""
        if self.is_finished():
            return
        self.rebuild()
        data = {**self.payload(), "attachments": []}
        if inter is not None:
            await inter.response.edit_message(**data)
        elif self.message is not None:
            await self.message.edit(**data)

    # ------------------------------------------------------------------ callbacks

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.author_id:
            return True
        await inter.send(self.tr("views.only_user", mention=f"<@{self.author_id}>"), ephemeral=True)
        return False

    async def _refuse(self, inter: disnake.MessageInteraction) -> None:
        await inter.send(self.tr("skin.invalid"), ephemeral=True)

    def _on_player(self, mode: str) -> Any:
        async def callback(inter: disnake.MessageInteraction) -> None:
            raw = (inter.values or [None])[0]
            current = stored_custom_name(self.draft.player(mode))
            customs = (current,) if current is not None else ()
            value = player_value(raw, self.choices.ids(mode), customs)
            if value is None:
                return await self._refuse(inter)
            self.draft = self.draft.with_player(mode, value)
            await self.refresh(inter)

        return callback

    async def _on_target(self, inter: disnake.MessageInteraction) -> None:
        target = target_value((inter.values or [None])[0], self.choices.radios)
        if target is None:
            return await self._refuse(inter)
        self.draft = dataclasses.replace(self.draft, target=target, focus=FOCUS_RADIO)
        await self.refresh(inter)

    async def _on_radio_skin(self, inter: disnake.MessageInteraction) -> None:
        look = radio_skin_value((inter.values or [None])[0], self.draft.target)
        if look is INVALID:
            return await self._refuse(inter)
        self.draft = self.draft.with_radio_skin(look if isinstance(look, str) else None)
        await self.refresh(inter)

    async def _on_global(self, inter: disnake.MessageInteraction) -> None:
        self.draft = dataclasses.replace(self.draft, global_mode=not self.draft.global_mode)
        await self.refresh(inter)

    async def _on_disc(self, inter: disnake.MessageInteraction) -> None:
        self.draft = dataclasses.replace(self.draft, disc_motion=not self.draft.disc_motion)
        await self.refresh(inter)

    async def _on_custom(self, inter: disnake.MessageInteraction) -> None:
        view = CustomSkinsView(self)
        await inter.response.send_message(view.text(), view=view, ephemeral=True)
        view.interaction = inter  # a efêmera só se edita pela interação que a abriu (``on_timeout``)

    async def _on_save(self, inter: disnake.MessageInteraction) -> None:
        if self.is_finished():  # clique já agendado antes do Cancelar ou do tempo
            return await self._refuse(inter)
        self.result, self.interaction = "save", inter
        self.stop()

    async def _on_cancel(self, inter: disnake.MessageInteraction) -> None:
        if self.is_finished():
            return await self._refuse(inter)
        self.result, self.interaction = "cancel", inter
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (disnake.ui.Button, disnake.ui.Select)):
                item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except disnake.HTTPException as error:
            logger.info("Falha ao desligar o /skins (%s)", type(error).__name__)


class CustomSkinsView(disnake.ui.View):
    """As personalizadas do servidor numa mensagem efêmera: um select por modo, paginado por ``rules/jukebox_select.plan``.
    A escolha entra no rascunho do ``/skins`` (no modo global ou do servidor, o que estiver ligado); nada é gravado aqui."""

    def __init__(self, parent: SkinsView) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_S)
        self.parent = parent
        self.pages = {MODE_NORMAL: 1, MODE_STATIC: 1}
        self.picked: str | None = None
        self.interaction: disnake.MessageInteraction | None = None
        self.rebuild()

    def text(self) -> str:
        tr = self.parent.tr
        return tr("skin.custom.picked", skin=self.picked) if self.picked else tr("skin.custom.title")

    def rebuild(self) -> None:
        self.clear_items()
        tr = self.parent.tr
        for row, mode, key in ((0, MODE_NORMAL, "skin.select.normal"), (1, MODE_STATIC, "skin.select.static")):
            names = self.parent.choices.customs(mode)
            if not names:
                continue
            page = page_plan(len(names), 0, self.pages[mode])
            options = []
            if page.previous is not None:
                options.append(
                    disnake.SelectOption(
                        label=tr("skin.custom.page", page=page.previous),
                        value=f"{PAGE_VALUE}{page.previous}",
                        emoji="◀",
                    )
                )
            for name in names[page.start : page.stop]:
                options.append(
                    disnake.SelectOption(
                        label=fit(label_text(name), LABEL_MAX), value=custom_value(name), emoji=CUSTOM_EMOJI
                    )
                )
            if page.next is not None:
                options.append(
                    disnake.SelectOption(
                        label=tr("skin.custom.page", page=page.next), value=f"{PAGE_VALUE}{page.next}", emoji="▶"
                    )
                )
            select = disnake.ui.Select(placeholder=fit(tr(key), LABEL_MAX), options=options, row=row)
            select.callback = self._on_pick(mode)
            self.add_item(select)

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        return await self.parent.interaction_check(inter)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, disnake.ui.Select):
                item.disabled = True
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_message(view=self)
        except disnake.HTTPException as error:
            logger.info("Falha ao desligar as personalizadas do /skins (%s)", type(error).__name__)

    def _on_pick(self, mode: str) -> Any:
        async def callback(inter: disnake.MessageInteraction) -> None:
            raw = (inter.values or [None])[0]
            if isinstance(raw, str) and raw.startswith(PAGE_VALUE):
                number = raw[len(PAGE_VALUE) :]
                self.pages[mode] = int(number) if number.isascii() and number.isdigit() else 1
                self.rebuild()
                return await inter.response.edit_message(content=self.text(), view=self)
            value = player_value(raw, (), self.parent.choices.customs(mode))
            if (
                value is None or self.is_finished() or self.parent.is_finished()
            ):  # tempo desta tela, Salvar, Cancelar ou o da principal
                return await self.parent._refuse(inter)
            self.parent.draft = self.parent.draft.with_player(mode, value)
            self.picked = skin_name(self.parent.tr, value)
            self.rebuild()
            await inter.response.edit_message(content=self.text(), view=self)
            try:
                await self.parent.refresh()
            except disnake.HTTPException as error:
                logger.info("Falha ao atualizar o /skins (%s)", type(error).__name__)

        return callback
