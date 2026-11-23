"""Apresentação do modo rádio da Jukebox (T-434 J2a): só monta texto e componentes; nenhuma decisão (essa é de ``jukebox_radio``).

Separada do controlador para as skins novas (T-SKINS-1) reaproveitarem as mesmas peças: o que mostra o modo rádio lê só a visão imutável
(``JukeboxRadio.view``, §1.5.1), nunca ``player.radio_mode``. Todo nome de estação e título ICY chega GUARDADO
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
SERVER_VALUE = "jb:server"
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


def vote_payload(
    tr: Tr,
    *,
    nonce: str,
    author_id: int,
    target: Target,
    station_name: object,
    votes: int,
    needed: int,
    expires_at: float,
) -> dict[str, Any]:
    """A mensagem da votação no canal do player e o botão ``jukebox_radio_vote:<nonce>`` (o alvo nunca vai no botão). O prazo vai como
    ``<t:..:R>``: o Discord atualiza sozinho, sem edição a cada segundo."""
    member = f"<@{int(author_id)}>"
    deadline = f"<t:{int(expires_at)}:R>"
    if target is Target.LEAVE:
        text = tr("jukebox.radio.vote.leave_text", member=member, votes=votes, needed=needed, deadline=deadline)
    elif target is Target.STATION:
        text = tr(
            "jukebox.radio.vote.station_text",
            member=member,
            station=station_md(station_name),
            votes=votes,
            needed=needed,
            deadline=deadline,
        )
    else:
        text = tr(
            "jukebox.radio.vote.text",
            member=member,
            station=station_md(station_name),
            votes=votes,
            needed=needed,
            deadline=deadline,
        )
    button = disnake.ui.Button(
        label=fit(tr("jukebox.radio.vote.button"), 80),
        emoji="✋",
        style=disnake.ButtonStyle.primary,
        custom_id=f"{VOTE_PREFIX}{nonce}",
    )
    return {"content": text, "components": [button]}


def vote_passed_live(tr: Tr, station_name: object) -> str:
    return tr("jukebox.radio.vote.passed_live", station=station_md(station_name))


def vote_passed_station(tr: Tr, station_name: object, *, now: bool) -> str:
    """Quando a estação toca (A5): já, ou só depois da música atual."""
    key = "jukebox.radio.vote.passed_station_now" if now else "jukebox.radio.vote.passed_station_next"
    return tr(key, station=station_md(station_name))


def vote_passed_leave(tr: Tr, mention: str) -> str:
    return tr("jukebox.radio.vote.passed_leave", member=mention)


def vote_failed(tr: Tr, reason: str) -> str:
    return tr("jukebox.radio.vote.failed", reason=reason)


def vote_expired(tr: Tr) -> str:
    return tr("jukebox.radio.vote.expired")


def dropped_text(tr: Tr, station_name: object, mention: str) -> str:
    """ "📻 {estação} saiu do ar. {membro}, a Jukebox parou." (P11): uma vez, no canal do player."""
    return tr("jukebox.radio.dropped", station=station_md(station_name), member=mention)


def live_line(tr: Tr, view: Any, now: float) -> str:
    """ "🔴 Ao vivo · no ar há {n} min" no lugar da barra de progresso (A6). ``view`` = ``RadioView``."""
    minutes = max(0, int((now - float(view.since)) // 60))
    return tr("jukebox.radio.live_line", minutes=minutes)


def now_playing_lines(tr: Tr, view: Any, now: float) -> list[str]:
    """Tocando agora em R: a estação (sem link), o título ICY quando há e a linha "Ao vivo". ``view`` = ``RadioView``."""
    lines = [f"📻 **{station_md(view.name)}**"]
    if view.title:
        lines.append(external_text(view.title, TITLE_LIMIT))  # guardado SEM escape: escapa uma vez aqui
    lines.append(live_line(tr, view, now))
    return [line for line in lines if line]


def vote_warning(tr: Tr, seconds: int) -> str:
    """A 2ª linha do aviso do passo "Estação escolhida" para quem não é DJ: o clique abre uma votação (prévia; a porta é refeita no clique)."""
    return tr("jukebox.radio.warn.vote", seconds=seconds)


LEAVE_PREFIX = "jukebox_radio_leave:"  # ``jukebox_radio_leave:<id do pedido>:<go|vote|cancel>``: o alvo (sair de R) é fixo no código [s3]
LEAVE_GO, LEAVE_VOTE, LEAVE_CANCEL = "go", "vote", "cancel"


def leave_confirm(
    tr: Tr, *, request_id: object, station_name: object, vote: bool, seconds: int, disabled: bool = False
) -> dict[str, Any]:
    """O passo de confirmação do pedido de música em R (desvio 4, P3 "avisa antes"): variante A (parar e tocar) ou B (abrir votação)."""
    station = station_md(station_name)
    if vote:
        content = (
            tr("jukebox.radio.leave.warn_vote", station=station, seconds=seconds)
            + "\n"
            + tr("jukebox.radio.leave.vote_note")
        )
        first = disnake.ui.Button(
            label=fit(tr("jukebox.radio.leave.vote"), 80),
            emoji="✋",
            style=disnake.ButtonStyle.primary,
            custom_id=f"{LEAVE_PREFIX}{request_id}:{LEAVE_VOTE}",
            disabled=disabled,
        )
    else:
        content = tr("jukebox.radio.leave.warn", station=station)
        first = disnake.ui.Button(
            label=fit(tr("jukebox.radio.leave.confirm"), 80),
            style=disnake.ButtonStyle.danger,
            custom_id=f"{LEAVE_PREFIX}{request_id}:{LEAVE_GO}",
            disabled=disabled,
        )
    cancel = disnake.ui.Button(
        label=fit(tr("jukebox.radio.leave.cancel"), 80),
        style=disnake.ButtonStyle.secondary,
        custom_id=f"{LEAVE_PREFIX}{request_id}:{LEAVE_CANCEL}",
        disabled=disabled,
    )
    return {"content": content, "components": [first, cancel]}


# ---------------------------------------------------------------------- estação (J2b, A5)


def server_entry(tr: Tr, count: int) -> disnake.SelectOption:
    """A entrada fixa "📻 Rádios do Servidor" (A1/A5): as estações do servidor e os gêneros prontos."""
    return disnake.SelectOption(
        label=fit(tr("jukebox.select.server.label"), 100),
        value=SERVER_VALUE,
        emoji="🎙️",
        description=fit(server_description(tr, count), 100),
    )


def server_description(tr: Tr, count: int) -> str:
    """A descrição da entrada: sem estação própria só os gêneros prontos (a entrada aparece mesmo assim); senão o plural do catálogo."""
    count = max(0, int(count))
    if count == 0:
        return tr("jukebox.select.server.description_none")
    return tr("jukebox.select.server.description", count=count)


def station_ended(tr: Tr, station_name: object, failures: int) -> str:
    """A estação parou sozinha (``failures`` faixas seguidas sem tocar, §2.6): uma mensagem no canal do player, sem menção."""
    return tr("jukebox.station.ended", station=station_md(station_name), count=failures)


def station_warning(tr: Tr, *, station_name: object, queued: bool, radio_name: object = None) -> str:
    """O aviso antes de "Tocar estação" (A5), mostrado ANTES do clique; a porta é refeita no clique."""
    if radio_name is not None:
        return tr("jukebox.station.warn.radio", radio=station_md(radio_name), station=station_md(station_name))
    if queued:
        return tr("jukebox.station.warn.queue", station=station_md(station_name))
    return ""


def autoplay_label(player: Any) -> str:
    """O rótulo do botão de autoplay do player: "📻 Estação: {nome}" com a estação ligada (A5), senão o de sempre. Lê só a visão (§1.5.1)."""
    jukebox = getattr(getattr(getattr(getattr(player, "bot", None), "pool", None), "radio", None), "jukebox", None)
    view = jukebox.station_view(player) if jukebox is not None else None
    if view is not None:
        return player.tr("jukebox.station.autoplay_label", station=external_text(view.name, NAME_LIMIT, markdown=False))
    return player.tr("player.disable_autoplay.label") if player.autoplay else player.tr("player.enable_autoplay.label")


def station_line(tr: Tr, view: Any) -> str:
    """A linha "📻 Estação: {nome}" do Tocando agora com a estação ligada (A6). ``view`` = ``StationView``."""
    return tr("jukebox.station.autoplay_label", station=station_md(view.name))
