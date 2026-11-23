"""O ponto único do pedido de música com a Jukebox no modo rádio (T-434 §2.2, P3/Q3): toda superfície que põe música na fila chama isto antes.

``/play`` (barra, prefixo e os componentes que passam por ele), o pedido digitado no canal da Jukebox, "Adicionar na fila" dos embeds, ⏮ e
``/readd``. Fora de R não faz nada. Em R, antes de qualquer efeito, o autor vê o passo de confirmação (desvio 4 da J2a, P3 "avisa antes"):

- variante A (DJ/admin, ou um voto que passaria no ato): "Parar a rádio e tocar" / "Cancelar";
- variante B (membro, com votação): "Abrir votação" / "Cancelar" (plano B: o voto decide só sair de R; se passar, ele pede de novo);
- sem confirmação, recusa direta: modo restrito, votação já aberta, autor em espera.

A prévia só escolhe o texto. O clique refaz a porta por ``jukebox_radio.leave_for_music``, sob o lock [s3]. Só o autor responde, o alvo nunca vai
no ``custom_id`` e a espera é a do ``wait_for`` que o ``play`` já usa (45 s). Quem esperou foi só o autor, na própria corrotina: nada fica guardado
para os outros votarem.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import disnake

from empiremuse.utils.music.errors import GenericError

CONFIRM_TIMEOUT_S = 45
CLOSED_GRACE_S = 5.0  # depois de fechado, o passo ainda é lembrado um instante: o ouvinte não confunde o clique do autor com o de outro
_PENDING: dict[
    str, tuple[int, float]
] = {}  # id do pedido → (autor, quando expira): o ouvinte responde a quem NÃO é o autor


def _sweep(now: float) -> None:
    """Tira os passos expirados (chamado ao abrir e ao consultar um passo: o registro nunca cresce sem limite, Sentinela N2)."""
    for key, (_, until) in list(_PENDING.items()):
        if until < now:
            _PENDING.pop(key, None)


def pending_author(request_id: str, now: Optional[float] = None) -> Optional[int]:
    """O autor de um passo de confirmação ainda aberto (``None`` se não há, ou já expirou)."""
    _sweep(time.monotonic() if now is None else now)
    entry = _PENDING.get(request_id)
    return entry[0] if entry is not None else None


def receiving_client(inter: Any, bot: Any) -> Any:
    """O cliente que recebe o clique na mensagem de confirmação: o que recebeu o pedido (``inter.bot`` em interação e no prefixo), nunca o bot
    do player, que no pool pode ser outro (Crivo MA-N1). A mensagem do canal da Jukebox não tem ``.bot``: vale o bot do cog que a leu."""
    return getattr(inter, "bot", None) or bot


def radio_mode_request(pool: Any, inter: Any) -> bool:
    """O pedido de música cai num player em R? Para o ``/play`` adiar de forma efêmera ANTES do ``defer`` (Crivo MI-N1): a 1ª resposta herda a
    visibilidade do ``defer``, e o passo de confirmação tem de ser só do autor."""
    from empiremuse.utils.music.checks import resolve_player

    try:
        resolved = resolve_player(pool, inter.guild_id, inter.author)
    except Exception:  # noqa: BLE001 - sem como saber, o adiamento segue o de sempre
        return False
    return resolved is not None and getattr(resolved[1], "radio_mode", None) is not None


def _request_id(inter: Any) -> str:
    return str(getattr(inter, "id", None) or getattr(getattr(inter, "message", None), "id", ""))


async def _send(inter: Any, payload: dict[str, Any]) -> Any:
    """Efêmera em interação (barra e componentes); resposta à mensagem no prefixo e no canal da Jukebox (prefixo não tem efêmero)."""
    if isinstance(inter, disnake.Interaction):
        if inter.response.is_done():
            return await inter.followup.send(**payload, ephemeral=True, wait=True)
        await inter.response.send_message(**payload, ephemeral=True)
        return await inter.original_message()
    message = getattr(inter, "message", None) or inter
    return await message.reply(**payload, mention_author=False, allowed_mentions=disnake.AllowedMentions.none())


async def _close(target: Any, text: str, *, clicked: bool) -> None:
    """Fecha o passo: só o texto do resultado, sem botão."""
    try:
        if clicked:
            await target.edit_original_message(content=text, components=[])
        else:
            await target.edit(content=text, components=[])
    except Exception:  # noqa: BLE001,S110 - a mensagem pode ter sido apagada; o resultado já valeu
        pass


async def confirm_leave_radio(
    pool: Any,
    bot: Any,
    inter: Any,
    player: Any,
    *,
    wait_for: Optional[Callable[..., Awaitable[Any]]] = None,
    clock: Callable[[], float] = time.monotonic,
    stopped_key: str = "jukebox.radio.leave.stopped",
) -> bool:
    """``True`` = o pedido segue (fora de R, a rádio parou, ou já tinha parado). ``False`` = parar em silêncio (cancelado, expirado, votação
    aberta ou recusa no clique: a mensagem já disse). ``GenericError`` = recusa direta, sem confirmação."""
    from empiremuse.rules.radio_mode import VOTE_TTL_S, Decision
    from empiremuse.utils.music import jukebox_ui
    from empiremuse.utils.radio.jukebox_radio import Refused

    jukebox = getattr(getattr(pool, "radio", None), "jukebox", None)
    if jukebox is None or getattr(player, "radio_mode", None) is None:
        return True
    tr = player.tr
    member = player.guild.get_member(inter.author.id) or inter.author
    try:
        decision = await jukebox.leave_preview(member, player)
    except Refused as refused:
        key, fields = refused.key, refused.fields  # só a chave e os campos do controlador, nunca o texto de uma exceção
    else:
        key, fields = "", {}
    if key:
        raise GenericError(tr(key, **fields))
    if decision is None:
        return True
    view = jukebox.view(player)
    request_id, vote = _request_id(inter), decision is Decision.VOTE

    def payload(disabled: bool = False) -> dict[str, Any]:
        return jukebox_ui.leave_confirm(
            tr,
            request_id=request_id,
            station_name=view.name if view is not None else "",
            vote=vote,
            seconds=VOTE_TTL_S,
            disabled=disabled,
        )

    message = await _send(inter, payload())
    _sweep(clock())
    _PENDING[request_id] = (member.id, clock() + 2 * CONFIRM_TIMEOUT_S)
    prefix = f"{jukebox_ui.LEAVE_PREFIX}{request_id}:"

    def check(click: Any) -> bool:
        custom_id = str(getattr(getattr(click, "data", None), "custom_id", ""))
        return custom_id.startswith(prefix) and getattr(click.author, "id", None) == member.id

    try:
        click = await (wait_for or receiving_client(inter, bot).wait_for)(
            "button_click", timeout=CONFIRM_TIMEOUT_S, check=check
        )
    except asyncio.TimeoutError:
        await _close(message, tr("jukebox.radio.leave.timeout"), clicked=False)
        return False
    finally:
        _PENDING[request_id] = (
            member.id,
            clock() + CLOSED_GRACE_S,
        )  # fechado: some logo (Sentinela N2), sem corrida com o ouvinte
    choice = str(click.data.custom_id).rsplit(":", 1)[-1]
    await click.response.edit_message(**payload(disabled=True))  # processando: botões desativados até o resultado
    if choice == jukebox_ui.LEAVE_CANCEL:
        await _close(click, tr("jukebox.radio.leave.cancelled"), clicked=True)
        return False
    if player.radio_mode is None:
        # a rádio acabou nos 45 s; se o player foi destruído no meio, o pedido não segue sobre ele (Sentinela N1)
        alive = _alive(player)
        return await _close_and_fence(
            click, player, tr("jukebox.radio.leave.already_off" if alive else "jukebox.radio.player_closed"), alive
        )
    try:
        await jukebox.leave_for_music(click, player)  # a porta de verdade, refeita sob o lock
    except Refused as refused:
        key, fields = refused.key, refused.fields
    else:
        alive = _alive(player)  # destruído na espera do lock: ``forget`` zerou o modo e a porta não segue (Crivo S-2)
        return await _close_and_fence(click, player, tr(stopped_key if alive else "jukebox.radio.player_closed"), alive)
    await _close(click, tr(key, **fields), clicked=True)
    return False


def _alive(player: Any) -> bool:
    """O player ainda é o da guild e não está fechando."""
    return not player.is_closing and player.bot.music.players.get(player.guild.id) is player


async def _close_and_fence(click: Any, player: Any, text: str, proceed: bool) -> bool:
    """Fecha o passo e devolve ``proceed``. O epoch é lido ANTES do ``_close`` (sem ``await`` desde a porta): se outro membro pôs o player
    em R durante a edição, o pedido é descartado com o aviso (Crivo MI-D2, 11.4), e a superfície nem chega a enfileirar."""
    epoch = getattr(player, "radio_epoch", None)
    await _close(click, text, clicked=True)
    if proceed:
        enqueue_fence(player, epoch)
    return proceed


def enqueue_fence(player: Any, epoch: Optional[int]) -> None:
    """A cerca de ``radio_epoch`` (§1.5 [m3], 11.4): chamada logo antes de enfileirar, sem ``await`` até o ``queue.append/extend``. Se uma
    transição do modo rádio aconteceu desde a porta, a música não entra e quem pediu recebe o aviso de música descartada."""
    if epoch is not None and getattr(player, "radio_epoch", None) != epoch:
        raise GenericError(player.tr("jukebox.radio.discarded"))


async def answer_stray_click(interaction: Any, tr_of: Callable[[], Awaitable[Callable[..., str]]]) -> bool:
    """O clique num passo de confirmação de outra pessoa (a resposta do prefixo é pública): recusa efêmera, sem efeito. ``True`` se respondeu.

    O autor é lido ANTES de qualquer ``await``: o clique do próprio autor nunca recebe uma segunda resposta."""
    from empiremuse.utils.music import jukebox_ui

    custom_id = str(getattr(getattr(interaction, "data", None), "custom_id", ""))
    if not custom_id.startswith(jukebox_ui.LEAVE_PREFIX):
        return False
    request_id = custom_id[len(jukebox_ui.LEAVE_PREFIX) :].rsplit(":", 1)[0]
    author = pending_author(request_id)
    if author is not None and author == interaction.author.id:
        return False  # é do autor: quem responde é o ``wait_for``
    key = "jukebox.radio.leave.not_author" if author is not None else "jukebox.radio.leave.timeout"
    tr = await tr_of()
    await interaction.send(tr(key), ephemeral=True)
    return True
