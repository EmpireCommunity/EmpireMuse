"""Apresentação do modo rádio da Jukebox (T-434 J2a): só monta texto e componentes; nenhuma decisão (essa é de ``jukebox_radio``).

Separada do controlador para as skins novas (T-SKINS-1) reaproveitarem as mesmas peças. Todo nome de estação e título ICY chega GUARDADO
(escapado pelo diretório ou sem escape no ``radio_mode``) e sai por ``stored_text``: com escape de markdown onde o destino renderiza markdown
e sem link nem menção em qualquer destino (MJ1). Quem é mencionado é só o autor, por ``<@id>`` com ``allowed_mentions`` só dele.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.radio_mode import Target
from empiremuse.rules.text import external_text, stored_text

LIVE_VALUE = "jb:live"
VOTE_PREFIX = "jukebox_radio_vote:"
NAME_LIMIT = 80
TITLE_LIMIT = 120

Tr = Callable[..., str]


def station_md(name: object) -> str:
    """O nome da estação para um texto com markdown (embed, mensagem): escapado uma vez, sem link nem menção."""
    return stored_text(name, NAME_LIMIT) or "—"


def live_entry(tr: Tr) -> disnake.SelectOption:
    """A entrada fixa "📻 Rádios ao Vivo" do topo do select da Jukebox (A1), só com premium (quem decide é o construtor)."""
    return disnake.SelectOption(
        label=fit(tr("jukebox.select.live.label"), 100),
        value=LIVE_VALUE,
        emoji="📻",
        description=fit(tr("jukebox.select.live.description"), 100),
    )


def chosen_warning(tr: Tr, *, playing: bool, queued: bool, radio_name: object = None) -> str:
    """O aviso do passo "Estação escolhida" (A4), mostrado ANTES do clique; a porta é refeita no clique."""
    if radio_name is not None:
        return tr("jukebox.radio.warn.switch", station=station_md(radio_name))
    if queued:
        return tr("jukebox.radio.warn.stop_music_queue")
    if playing:
        return tr("jukebox.radio.warn.stop_music")
    return ""


def vote_payload(tr: Tr, vote: Any, votes: int, needed: int) -> dict[str, Any]:
    """A mensagem da votação no canal do player e o botão ``jukebox_radio_vote:<nonce>`` (o alvo nunca vai no botão)."""
    member = f"<@{int(vote.author_id)}>"
    if vote.target is Target.LEAVE:
        text = tr("jukebox.radio.vote.leave_text", member=member, votes=votes, needed=needed)
    else:
        text = tr(
            "jukebox.radio.vote.text", member=member, station=station_md(vote.station_name), votes=votes, needed=needed
        )
    button = disnake.ui.Button(
        label=fit(tr("jukebox.radio.vote.button"), 80),
        emoji="✋",
        style=disnake.ButtonStyle.primary,
        custom_id=f"{VOTE_PREFIX}{vote.nonce}",
    )
    return {"content": text, "components": [button]}


def vote_passed_live(tr: Tr, station_name: object) -> str:
    return tr("jukebox.radio.vote.passed_live", station=station_md(station_name))


def vote_passed_leave(tr: Tr, mention: str) -> str:
    return tr("jukebox.radio.vote.passed_leave", member=mention)


def vote_failed(tr: Tr, reason: str) -> str:
    return tr("jukebox.radio.vote.failed", reason=reason)


def vote_expired(tr: Tr) -> str:
    return tr("jukebox.radio.vote.expired")


def dropped_text(tr: Tr, station_name: object, mention: str) -> str:
    """ "📻 {estação} saiu do ar. {membro}, a Jukebox parou." (P11): uma vez, no canal do player."""
    return tr("jukebox.radio.dropped", station=station_md(station_name), member=mention)


def live_line(tr: Tr, mode: Any, now: float) -> str:
    """ "🔴 Ao vivo · no ar há {n} min" no lugar da barra de progresso (A6)."""
    minutes = max(0, int((now - float(mode.started_at)) // 60))
    return tr("jukebox.radio.live_line", minutes=minutes)


def now_playing_lines(tr: Tr, mode: Any, now: float) -> list[str]:
    """Tocando agora em R: a estação (sem link), o título ICY quando há e a linha "Ao vivo"."""
    lines = [f"📻 **{station_md(mode.name)}**"]
    if mode.title:
        lines.append(external_text(mode.title, TITLE_LIMIT))  # guardado SEM escape: escapa uma vez aqui
    lines.append(live_line(tr, mode, now))
    return [line for line in lines if line]
