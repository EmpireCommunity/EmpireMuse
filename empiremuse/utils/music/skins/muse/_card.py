"""As skins de imagem da Jukebox (Expandida, Slim e Rádio da Jukebox): o texto do embed e os componentes. O desenho é do pipeline.

Acordo com o render (T-SKINS-S5A, contrato §1 e §2.2):
- ``card``: a família do renderizador (``utils/render/cards/<card>.py``); ``theme``: ``black`` ou ``white``.
- ``project``: a ``project(spec)`` pura do próprio renderizador (quem desenha decide o que entra na comparação); o
  ``_np_render`` faz ``skin.project(card_facts.build(player, settings))``.
- ``load(player)`` devolve o payload de sempre, com o embed do cartão em ``embeds[0]``, SEM ``image`` e com o autor só com
  ``url`` (o link da faixa) e ``icon_url``: o ``_np_render`` põe a imagem, o título no ``author.name`` e o anexo, e tira o
  autor em call privada.
- O texto do embed é só o que a imagem não faz (D4): o fim em ``<t:R>``, "pausada", "ao vivo", a menção de quem pediu, a
  última ação e o rodapé. Nada da faixa em call privada.
- ``auto_update = 0`` (redesenho por evento), exceto a barra em texto da Expandida (``TEXT_BAR_IN_EMBED``, opção C), que
  depende do spike S0: se editar o texto reiniciar o GIF, basta desligar aqui.
"""

from __future__ import annotations

import time
from importlib import import_module
from typing import Any, Callable

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.text import external_text
from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import music_source_image, time_format
from empiremuse.utils.music.skins.muse import _text as tx
from empiremuse.utils.music.skins.muse._base import base_features, playing_on_bot, preview_url
from empiremuse.utils.music.skins.muse._controls import components

TEXT_BAR_IN_EMBED = True  # opção C (respostas-alicerce-2 §1); pendente do S0
CARDS_PACKAGE = "empiremuse.utils.render.cards"
# prefixo do id -> família do renderizador. O retrato (``muse_radio_portrait_*``) fica fora do S5 (D14 com o dono).
CARD_OF = {"muse_expanded": "expanded", "muse_slim": "slim", "muse_radio": "visor_jukebox"}
THEMES = ("black", "white")
TEXT_BAR_CARDS = frozenset({"expanded"})
DESCRIPTION_MAX = 4096
AUTHOR_MAX = 256
PAUSE_ICON = "player_pause_icon.png"


def parse_id(skin_id: str) -> tuple[str, str]:
    """``muse_expanded_black`` -> (``expanded``, ``black``). Id fora do acordo levanta (o carregador registra no log)."""
    prefix, _, theme = skin_id.rpartition("_")
    if prefix not in CARD_OF or theme not in THEMES:
        raise ValueError(f"skin de imagem desconhecida: {skin_id}")
    return CARD_OF[prefix], theme


def projector(card: str) -> Callable[[Any], Any]:
    """A ``project(spec)`` do renderizador da família (nome do pacote fixo; ``card`` só vem de ``CARD_OF``)."""
    return import_module(f"{CARDS_PACKAGE}.{card}").project


def text_bar_on(card: str) -> bool:
    return TEXT_BAR_IN_EMBED and card in TEXT_BAR_CARDS


def timeline(player: Any, ctx: tx.Context, tr: tx.Tr, *, bar: bool) -> list[str]:
    """O tempo que a imagem não mostra: R (no ar há / tentativa), ao vivo, pausada, ou o fim em ``<t:R>`` (com a barra)."""
    if ctx.radio is not None:
        return tx.radio_lines(ctx, tr)[-1:]
    current = player.current
    if current.is_stream:
        return [f"🔴 {tr('jbx.state.radio')}"]
    if player.paused:
        return [f"-# {tr('jbx.paused_word')}"]
    ends = f"-# {tr('jbx.text.ends', t=tx.relative(tx.track_end(player, ctx.now)))}"
    if not bar:
        return [ends]
    return [f"{tx.text_bar(tx.bar_segment(player))} `{time_format(current.duration)}`", ends]


def body(player: Any, ctx: tx.Context, tr: tx.Tr, *, bar: bool) -> list[str]:
    lines = timeline(player, ctx, tr, bar=bar)
    if ctx.radio is None and not ctx.hidden:
        if requested := tx.requested_line(player, ctx, tr):
            lines.append(f"-# {requested}")
        if (end := tx.queue_end(player, ctx.now)) is not None:
            lines.append(f"-# {tr('jbx.text.queue_ends', t=tx.relative(end))}")
    if action := tx.last_action(player, ctx):
        lines.append(action)
    return lines


def set_author_link(embed: disnake.Embed, player: Any, ctx: tx.Context) -> None:
    """Só o link e o ícone da fonte; o nome (o título da faixa) quem põe é o ``_np_render``. Nada em call privada nem em R."""
    if ctx.hidden or ctx.radio is not None:
        return
    current = player.current
    icon = asset_url(PAUSE_ICON) if player.paused else music_source_image(current.info.get("sourceName"))
    name = (
        external_text(current.single_title, AUTHOR_MAX, markdown=False) or "—"
    )  # o _np_render troca por author_title(spec)
    embed.set_author(name=name, url=tx.track_url(current), icon_url=icon)


class ImageSkin:
    __slots__ = ("name", "preview", "card", "theme", "project")

    def __init__(self, skin_id: str) -> None:
        self.card, self.theme = parse_id(skin_id)
        self.name = skin_id
        self.preview = preview_url(skin_id)
        self.project = projector(self.card)

    def setup_features(self, player: Any) -> None:
        if text_bar_on(self.card):
            from empiremuse.utils.music.card_facts import text_bar_interval_s

            interval = text_bar_interval_s(playing_on_bot(player))
        else:
            interval = 0
        base_features(player, auto_update=interval, mini_queue=False)

    def build(self, player: Any, now: float) -> dict:
        tr = player.tr
        ctx = tx.context(player, now)
        embed = disnake.Embed(color=player.bot.get_color(player.guild.me))
        set_author_link(embed, player, ctx)
        embed.description = fit("\n".join(body(player, ctx, tr, bar=text_bar_on(self.card))), DESCRIPTION_MAX) or None
        tx.set_footer(embed, player, tr)
        return {"content": None, "embeds": [embed], "components": components(player, hidden=ctx.hidden)}

    def load(self, player: Any) -> dict:
        return self.build(player, time.time())
