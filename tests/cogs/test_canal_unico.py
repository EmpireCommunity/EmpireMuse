"""Canal de pedidos único (item 11, P4, S7, S7b, S-V8, S10): só o bot do painel trata o que é digitado, o botão e o modal agem SÓ no
player da call de quem usa (resolvido de novo no envio), o painel edita no máximo a cada 3 s e a call privada nunca mostra faixa."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.i18n import get_i18n
from empiremuse.music_cog import controller_buttons, jukebox_steps
from empiremuse.music_cog.controller_dispatch import ControllerDispatchMixin
from empiremuse.utils import jukebox_panel as panel_module
from empiremuse.utils.music.checks import pick_request_bot, resolve_player
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.jukebox_panel import JukeboxPanel, entry_for, is_private

pytestmark = pytest.mark.critical

GID, PANEL_CHANNEL, PANEL_MESSAGE = 111, 22, 555


def make_bot(bot_id, identifier, *, call=None, ready=True, exclusive=None, in_guild=True, static=True, private=False):
    """Um bot do pool: ``call`` = id da call onde ele toca (``None`` = livre; ``private`` = ``@everyone`` não vê a call)."""
    perms = SimpleNamespace(view_channel=not private, connect=not private)
    channel = SimpleNamespace(
        id=call,
        name=f"call-{call}",
        guild=SimpleNamespace(default_role="everyone"),
        permissions_for=lambda role: perms,
    )
    voice = SimpleNamespace(channel=channel) if call else None
    guild = SimpleNamespace(id=GID, me=SimpleNamespace(voice=voice), get_member=lambda _id: None)
    player = (
        SimpleNamespace(guild=guild, static=static, is_closing=False, current=None, queue=[], paused=False)
        if call
        else None
    )
    cog = SimpleNamespace(bot_id=bot_id, player_controller=AsyncMock(), play=SimpleNamespace(callback=AsyncMock()))
    return SimpleNamespace(
        identifier=identifier,
        user=SimpleNamespace(id=bot_id, display_name=identifier),
        bot_ready=ready,
        exclusive_guild_id=exclusive,
        music=SimpleNamespace(players={GID: player} if player else {}),
        get_guild=lambda gid: guild if in_guild and gid == GID else None,
        get_cog=lambda name: cog,
        cog=cog,
    )


def make_pool(*bots):
    return SimpleNamespace(get_all_bots=lambda: list(bots))


def member(call=None):
    voice = SimpleNamespace(channel=SimpleNamespace(id=call)) if call else None
    return SimpleNamespace(voice=voice, id=7)


# ---------------------------------------------------------------------- S-V8: o player é o da call de quem apertou


@pytest.mark.parametrize(
    ("caller_call", "expected"),
    [(100, "TOKEN"), (200, "TOKEN_2"), (300, None), (None, None)],
    ids=["first_bots_call", "second_bots_call", "call_without_player", "not_in_a_call"],
)
def test_resolve_player_returns_the_player_of_the_callers_call_and_never_another(caller_call, expected):
    pool = make_pool(make_bot(2, "TOKEN_2", call=200), make_bot(1, "TOKEN", call=100))
    resolved = resolve_player(pool, GID, member(caller_call))
    assert (resolved[0].identifier if resolved else None) == expected


def test_a_bot_without_a_player_or_logged_out_is_never_chosen():
    free, logged_out = make_bot(1, "TOKEN"), make_bot(2, "TOKEN_2", call=200)
    logged_out.user = None
    assert resolve_player(make_pool(free, logged_out), GID, member(200)) is None


@pytest.mark.parametrize(
    ("bots", "caller_call", "expected"),
    [
        ([(1, "TOKEN", 100), (2, "TOKEN_2", None)], 100, "TOKEN"),  # o que já está na call de quem pediu
        ([(1, "TOKEN", 100), (2, "TOKEN_2", None)], None, "TOKEN_2"),  # senão o 1º livre
        ([(1, "TOKEN", None), (2, "TOKEN_2", None)], None, "TOKEN"),  # ordem do identifier
        ([(1, "TOKEN", 100), (2, "TOKEN_2", 200)], 300, None),  # todos ocupados em outras calls
    ],
)
def test_a_request_goes_to_the_bot_in_the_callers_call_else_the_first_free_one(bots, caller_call, expected):
    pool = make_pool(*[make_bot(i, ident, call=call) for i, ident, call in reversed(bots)])
    chosen = pick_request_bot(pool, GID, member(caller_call))
    assert (chosen.identifier if chosen else None) == expected


@pytest.mark.parametrize(
    "unfit",
    [{"ready": False}, {"exclusive": 999}, {"in_guild": False}],
    ids=["not_ready", "exclusive_of_another_server", "not_in_the_server"],
)
def test_a_bot_that_cannot_serve_here_is_skipped_when_picking_a_free_one(unfit):
    pool = make_pool(make_bot(1, "TOKEN", **unfit), make_bot(2, "TOKEN_2"))
    assert pick_request_bot(pool, GID, member(None)).identifier == "TOKEN_2"


# ---------------------------------------------------------------------- S7: só o bot do painel trata o que é digitado


class Sentinel(Exception):
    pass


def typed_request(*, panel_bot):
    request = SimpleNamespace(is_panel_bot=AsyncMock(return_value=panel_bot))
    bot = SimpleNamespace(
        pool=SimpleNamespace(servers=SimpleNamespace(jukebox=request)),
        get_channel=MagicMock(side_effect=Sentinel),  # só chega aqui quem passou do portão
    )
    channel = MagicMock(spec=disnake.TextChannel)
    channel.id = PANEL_CHANNEL
    message = SimpleNamespace(channel=channel, guild=SimpleNamespace(id=GID), id=1, author=SimpleNamespace(id=7))
    req = SimpleNamespace(
        data={"player_controller": {"channel": str(PANEL_CHANNEL), "message_id": str(PANEL_MESSAGE)}},
        message=message,
        player=None,
    )
    return SimpleNamespace(bot=bot), req, request


@pytest.mark.asyncio
async def test_a_message_typed_in_the_shared_channel_is_ignored_by_every_bot_but_the_panel_bot():
    self_, req, request = typed_request(panel_bot=False)
    assert await jukebox_steps._sr_validate_channel(self_, req) is True  # ignora
    request.is_panel_bot.assert_awaited_once_with(GID, self_.bot)

    self_, req, _ = typed_request(panel_bot=True)
    with pytest.raises(Sentinel):  # o bot do painel passa do portão e segue o fluxo de sempre
        await jukebox_steps._sr_validate_channel(self_, req)


@pytest.mark.asyncio
async def test_a_store_that_fails_means_not_the_panel_bot_better_to_ignore_than_to_answer_twice():
    self_, req, request = typed_request(panel_bot=True)
    request.is_panel_bot.side_effect = GenericError("indisponível")
    assert await jukebox_steps._sr_validate_channel(self_, req) is True


# ---------------------------------------------------------------------- S7/S-V8: clique no painel


def click(*, call, panel_bot=True, message_id=PANEL_MESSAGE):
    """Um clique no painel recebido pelo bot ``TOKEN`` (o do painel); a pessoa está na ``call`` dada."""
    me = make_bot(1, "TOKEN", call=100)
    other = make_bot(2, "TOKEN_2", call=200)
    request = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(message_id=PANEL_MESSAGE)),
        is_panel_bot=AsyncMock(return_value=panel_bot),
    )
    me.pool = make_pool(me, other)
    me.pool.servers = SimpleNamespace(jukebox=request)
    me.pool.i18n = get_i18n()
    interaction = SimpleNamespace(
        guild_id=GID,
        guild=SimpleNamespace(id=GID, preferred_locale="pt-BR"),
        message=SimpleNamespace(id=message_id),
        author=member(call),
        send=AsyncMock(),
    )
    req = SimpleNamespace(interaction=interaction, control="pause_resume")
    return SimpleNamespace(bot=me), req, other, interaction


@pytest.mark.asyncio
async def test_a_click_from_someone_in_no_call_only_warns_and_changes_nothing():
    self_, req, other, interaction = click(call=None)
    assert await controller_buttons._pc_route_panel_click(self_, req) is True
    interaction.send.assert_awaited_once_with(
        get_i18n().for_locale("pt-BR")("player.panel.join_a_call"), ephemeral=True
    )
    assert not hasattr(interaction, "music_bot")
    other.cog.player_controller.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_click_on_the_panel_bots_own_player_is_marked_so_a_non_static_player_does_not_swallow_it():
    self_, req, _, interaction = click(call=100)
    assert await controller_buttons._pc_route_panel_click(self_, req) is False
    assert interaction.panel_click is True

    self_, req, _, interaction = click(call=100, message_id=999)  # outra mensagem: segue o fluxo normal, sem marca
    assert await controller_buttons._pc_route_panel_click(self_, req) is False
    assert not hasattr(interaction, "panel_click")


@pytest.mark.asyncio
async def test_a_click_acts_on_the_player_of_the_callers_call_even_if_another_bot_received_it():
    self_, req, other, interaction = click(call=200)

    assert await controller_buttons._pc_route_panel_click(self_, req) is True

    other.cog.player_controller.assert_awaited_once_with(interaction, "pause_resume")
    assert interaction.music_bot is other  # e nunca o player da call 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [{"call": 100}, {"call": 200, "message_id": 999}, {"call": 200, "panel_bot": False}],
    ids=["own_call", "another_message", "not_the_panel_bot"],
)
async def test_a_click_is_left_to_the_normal_flow_when_it_is_not_a_cross_bot_panel_click(kwargs):
    self_, req, other, interaction = click(**kwargs)

    assert await controller_buttons._pc_route_panel_click(self_, req) is False
    other.cog.player_controller.assert_not_awaited()
    interaction.send.assert_not_awaited()


# ---------------------------------------------------------------------- S7b: pedido (e modal) resolvido NO ENVIO


def request_inter(author_call, channel_id=PANEL_CHANNEL):
    return SimpleNamespace(
        guild_id=GID,
        guild=SimpleNamespace(id=GID, preferred_locale="pt-BR"),
        channel_id=channel_id,
        channel=SimpleNamespace(parent_id=None),
        author=member(author_call),
    )


def executor_world(me=None, other=None):
    me = me or make_bot(1, "TOKEN")
    other = other or make_bot(2, "TOKEN_2", call=200)
    me.pool = make_pool(me, other)
    request = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(channel=PANEL_CHANNEL)))
    me.pool.servers = SimpleNamespace(jukebox=request)
    me.pool.i18n = get_i18n()
    dispatch = ControllerDispatchMixin.__new__(ControllerDispatchMixin)
    dispatch.bot = me
    return dispatch, me, other


@pytest.mark.asyncio
async def test_the_modal_is_resolved_again_on_submit_so_a_changed_call_acts_on_the_new_players_bot():  # S7b
    dispatch, me, other = executor_world()

    first = request_inter(200)  # quando abriu o modal estava na call do TOKEN_2...
    assert await dispatch.request_executor(first) is other.cog and first.music_bot is other

    second = request_inter(None)  # ...ao enviar já saiu: ninguém toca para ele, o 1º livre atende (o próprio TOKEN)
    assert await dispatch.request_executor(second) is dispatch and second.music_bot is me


@pytest.mark.asyncio
async def test_outside_the_shared_channel_nothing_is_rerouted_and_with_every_bot_busy_the_person_is_told():
    dispatch, me, other = executor_world()
    elsewhere = request_inter(200, channel_id=999)
    assert await dispatch.request_executor(elsewhere) is dispatch and not hasattr(elsewhere, "music_bot")

    busy_dispatch, _, _ = executor_world(make_bot(1, "TOKEN", call=100), make_bot(2, "TOKEN_2", call=200))
    with pytest.raises(GenericError):
        await busy_dispatch.request_executor(request_inter(300))


# ---------------------------------------------------------------------- S10: call privada e entrada do painel


def voice_channel(*, view, connect):
    perms = SimpleNamespace(view_channel=view, connect=connect)
    guild = SimpleNamespace(default_role="everyone")
    return SimpleNamespace(
        guild=guild,
        name="Sala secreta",
        permissions_for=lambda role: perms if role == "everyone" else None,
    )


@pytest.mark.parametrize(
    ("view", "connect", "private"),
    [(True, True, False), (False, True, True), (True, False, True), (False, False, True)],
)
def test_a_call_that_everyone_cannot_see_or_join_is_private(view, connect, private):
    assert is_private(voice_channel(view=view, connect=connect)) is private
    assert is_private(object()) is True  # sem como saber: privada


def test_the_panel_entry_of_a_private_call_has_no_track_link_or_requester():
    bot = make_bot(1, "TOKEN", call=100)
    player = bot.music.players[GID]
    player.guild.me.voice.channel = voice_channel(view=False, connect=True)
    player.current = SimpleNamespace(title="Faixa secreta", uri="https://exemplo.com/secreta", requester=7)
    entry = entry_for(bot, player)
    assert entry.private and entry.title is None and entry.url is None and entry.requester is None

    player.guild.me.voice.channel = voice_channel(view=True, connect=True)
    assert entry_for(bot, player).title == "Faixa secreta"
    player.guild.me.voice = None
    assert entry_for(bot, player) is None  # player sem call não vira bloco


# ---------------------------------------------------------------------- JukeboxPanel: quem conta, edição agrupada, reenvio


def panel_world(*bots):
    pool = make_pool(*bots)
    request = SimpleNamespace(get=AsyncMock(), elect=AsyncMock(), set=AsyncMock(), panel_bot=AsyncMock())
    pool.servers = SimpleNamespace(jukebox=request)
    pool.i18n = get_i18n()
    return JukeboxPanel(pool), pool


def test_only_static_players_that_are_not_closing_count_and_the_list_takes_over_with_two_or_more():
    one, two = make_bot(1, "TOKEN", call=100), make_bot(2, "TOKEN_2", call=200)
    panel, _ = panel_world(two, one)
    assert [b.identifier for b, _ in panel.players(GID)] == ["TOKEN", "TOKEN_2"] and panel.takes_over(GID)

    two.music.players[GID].static = False
    assert not panel.takes_over(GID)
    two.music.players[GID].static, one.music.players[GID].is_closing = True, True
    assert [b.identifier for b, _ in panel.players(GID)] == ["TOKEN_2"] and not panel.takes_over(GID)


@pytest.mark.asyncio
async def test_the_panel_is_edited_at_most_once_every_three_seconds_and_changes_in_the_window_become_one_edit(
    monkeypatch,
):
    panel, _ = panel_world()
    now = [1_000.0]
    monkeypatch.setattr(panel_module.time, "monotonic", lambda: now[0])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(panel_module.asyncio, "sleep", fake_sleep)
    panel._draw = AsyncMock()

    await panel.refresh(GID)  # nunca editou: edita já
    assert panel._draw.await_count == 1
    now[0] += 1.0
    await panel.refresh(GID)  # dentro da janela: agenda UMA edição
    await panel.refresh(GID)
    await panel.refresh(GID)
    assert panel._draw.await_count == 1 and len(panel._pending) == 1

    await panel._pending[GID]
    assert panel._draw.await_count == 2 and sleeps and 1.9 <= sleeps[0] <= 2.1  # os 3 s menos o 1 s já passado


@pytest.mark.asyncio
async def test_a_failing_draw_is_logged_by_type_only_and_never_raises(caplog):
    panel, _ = panel_world()
    panel._draw = AsyncMock(side_effect=RuntimeError("falha secreta no desenho"))
    await panel.refresh(GID, now=True)
    assert "falha secreta" not in caplog.text and "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_resending_is_done_by_the_panel_bot_and_the_new_message_id_is_saved(monkeypatch):
    sent = SimpleNamespace(id=777)
    panel_bot = make_bot(2, "TOKEN_2")
    old = SimpleNamespace(delete=AsyncMock())
    channel = SimpleNamespace(get_partial_message=lambda mid: old)
    panel_bot.get_channel = lambda cid: channel
    panel, pool = panel_world(panel_bot)
    pool.servers.jukebox.get.return_value = SimpleNamespace(channel=PANEL_CHANNEL, message_id=PANEL_MESSAGE, current=3)
    pool.servers.jukebox.panel_bot.return_value = panel_bot
    idle = AsyncMock(return_value=sent)
    import empiremuse.utils.others as others

    monkeypatch.setattr(others, "send_idle_embed", idle)

    assert await panel.resend(GID, replace_message_id=PANEL_MESSAGE) is sent

    assert idle.await_args.args[0] is channel and idle.await_args.kwargs["bot"] is panel_bot
    assert pool.servers.jukebox.set.await_args.args[:3] == (GID, PANEL_CHANNEL, 777)
    old.delete.assert_awaited_once()  # a mensagem do bot que saiu


@pytest.mark.asyncio
async def test_a_roster_change_only_re_elects_the_handoff_is_scheduled_by_the_election_itself():
    panel, pool = panel_world(make_bot(2, "TOKEN_2"))
    panel.resend, panel.handoff = AsyncMock(), AsyncMock()

    await panel.on_roster_change(GID)

    pool.servers.jukebox.elect.assert_awaited_once_with(GID)
    panel.resend.assert_not_awaited()  # não depende de "a eleição mudou" depois de uma espera (outra rotina pode ter gravado antes)


@pytest.mark.asyncio
async def test_schedule_handoff_runs_the_handoff_in_the_background_and_a_failure_is_only_logged():
    panel, _ = panel_world()
    finished = asyncio.Event()

    async def failing(gid):
        finished.set()
        raise RuntimeError("falha")

    panel.handoff = AsyncMock(side_effect=failing)

    panel.schedule_handoff(GID)
    await asyncio.wait_for(finished.wait(), 5)  # sinal do próprio dublê, sem sleep

    panel.handoff.assert_awaited_once_with(GID)


async def settle(panel):
    """Espera as tarefas de fundo do painel terminarem (sem sleep): é o que o ``_spawn`` guarda."""
    if panel._tasks:
        await asyncio.wait_for(asyncio.wait(list(panel._tasks)), 5)


@pytest.mark.asyncio
async def test_a_failed_handoff_stays_pending_and_the_next_panel_bot_call_retries_it_until_it_succeeds():  # CodeRabbit #159
    panel, _ = panel_world()
    outcomes = [RuntimeError("falha transitória"), None]

    async def handoff(gid):
        failure = outcomes.pop(0)
        if failure:
            raise failure

    panel.handoff = AsyncMock(side_effect=handoff)

    panel.schedule_handoff(GID)
    await settle(panel)
    assert panel.handoff.await_count == 1 and GID in panel._handoff_pending  # falhou: fica pendente

    panel.retry_pending_handoff(GID)  # o que panel_bot/is_panel_bot fazem quando a eleição já está gravada
    await settle(panel)
    assert panel.handoff.await_count == 2 and GID not in panel._handoff_pending

    panel.retry_pending_handoff(GID)  # nada pendente: nada a refazer
    await settle(panel)
    assert panel.handoff.await_count == 2


@pytest.mark.asyncio
async def test_a_handoff_requested_during_another_one_runs_again_after_it():
    panel, _ = panel_world()
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def handoff(gid):
        calls.append(gid)
        started.set()
        await release.wait()

    panel.handoff = AsyncMock(side_effect=handoff)
    panel.schedule_handoff(GID)
    await asyncio.wait_for(started.wait(), 5)
    panel.schedule_handoff(GID)  # outra troca de bot gravada enquanto a passagem corre
    release.set()
    await settle(panel)

    assert calls == [GID, GID]


@pytest.mark.asyncio
async def test_forget_schedules_a_refresh_for_the_server():
    panel, _ = panel_world()
    panel.refresh = AsyncMock()
    panel.forget(GID)
    await asyncio.sleep(0)
    panel.refresh.assert_awaited_once_with(GID)


# ---------------------------------------------------------------------- reenvio do painel: um só, sem mensagem órfã


@pytest.mark.asyncio
async def test_two_simultaneous_resends_send_one_panel_and_the_second_gets_the_existing_one(monkeypatch):
    panel_bot = make_bot(2, "TOKEN_2")
    panel_bot.get_channel = lambda cid: SimpleNamespace(id=cid)
    panel, pool = panel_world(panel_bot)
    pool.servers.jukebox.get.return_value = SimpleNamespace(channel=PANEL_CHANNEL, message_id=None, current=1)
    pool.servers.jukebox.panel_bot.return_value = panel_bot
    sent = SimpleNamespace(id=777, author=SimpleNamespace(id=2))  # do bot do painel (id 2)
    state = {"message": None}

    async def send_idle(channel, *, bot, force):
        await asyncio.sleep(0)  # cede o laço: sem a trava o 2º reenvio passaria aqui também
        state["message"] = sent
        return sent

    async def fetch_existing(pool_, gid):
        return state["message"]

    import empiremuse.utils.others as others

    idle = AsyncMock(side_effect=send_idle)
    monkeypatch.setattr(others, "send_idle_embed", idle)
    monkeypatch.setattr(panel_module, "fetch_panel_message", fetch_existing)

    first, second = await asyncio.gather(panel.resend(GID), panel.resend(GID))

    assert idle.await_count == 1 and first is sent and second is sent


@pytest.mark.asyncio
async def test_a_panel_message_from_a_different_author_is_replaced_not_reused(monkeypatch):
    panel_bot = make_bot(2, "TOKEN_2")
    old_author_message = SimpleNamespace(id=444, author=SimpleNamespace(id=1))  # o painel do bot que saiu
    deleted = AsyncMock()
    channel = SimpleNamespace(id=PANEL_CHANNEL, get_partial_message=lambda mid: SimpleNamespace(delete=deleted))
    panel_bot.get_channel = lambda cid: channel
    panel, pool = panel_world(panel_bot)
    pool.servers.jukebox.get.return_value = SimpleNamespace(channel=PANEL_CHANNEL, message_id=444, current=1)
    pool.servers.jukebox.panel_bot.return_value = panel_bot
    new = SimpleNamespace(id=999, author=SimpleNamespace(id=2))
    import empiremuse.utils.others as others

    idle = AsyncMock(return_value=new)
    monkeypatch.setattr(others, "send_idle_embed", idle)
    monkeypatch.setattr(panel_module, "fetch_panel_message", AsyncMock(return_value=old_author_message))

    assert await panel.resend(GID) is new

    idle.assert_awaited_once()  # uma mensagem de outro bot não pode ser editada por este: manda uma nova
    deleted.assert_awaited_once()  # e a antiga sai
    assert pool.servers.jukebox.set.await_args.args[:3] == (GID, PANEL_CHANNEL, 999)


@pytest.mark.asyncio
async def test_a_config_conflict_while_resending_deletes_the_new_message_and_raises(monkeypatch):
    from empiremuse.utils.servers import ServerConfigConflict

    panel_bot = make_bot(2, "TOKEN_2")
    panel_bot.get_channel = lambda cid: SimpleNamespace(id=cid)
    panel, pool = panel_world(panel_bot)
    pool.servers.jukebox.get.return_value = SimpleNamespace(channel=PANEL_CHANNEL, message_id=None, current=1)
    pool.servers.jukebox.panel_bot.return_value = panel_bot
    pool.servers.jukebox.set.side_effect = ServerConfigConflict
    sent = SimpleNamespace(id=777, delete=AsyncMock())
    import empiremuse.utils.others as others

    monkeypatch.setattr(others, "send_idle_embed", AsyncMock(return_value=sent))
    monkeypatch.setattr(panel_module, "fetch_panel_message", AsyncMock(return_value=None))

    with pytest.raises(ServerConfigConflict):
        await panel.resend(GID)

    sent.delete.assert_awaited_once()  # nenhum painel órfão no canal


@pytest.mark.asyncio
async def test_background_tasks_are_kept_while_running_and_dropped_when_done():
    panel, _ = panel_world()
    panel.refresh = AsyncMock()
    panel.forget(GID)
    assert len(panel._tasks) == 1  # referência forte enquanto roda
    await asyncio.gather(*panel._tasks)
    await asyncio.sleep(0)
    assert panel._tasks == set()


# ---------------------------------------------------------------------- player estático nunca manda mensagem própria no canal único


def static_player(resend):
    panel = SimpleNamespace(resend=resend)
    text_channel = SimpleNamespace(send=AsyncMock())
    player = SimpleNamespace(
        bot=SimpleNamespace(pool=SimpleNamespace(jukebox_panel=panel)),
        message=None,
        static=True,
        guild_id=GID,
        text_channel=text_channel,
        allowed_mentions=disnake.AllowedMentions.none(),
        channel_cleanup=AsyncMock(),
    )
    return player, SimpleNamespace(data={"content": "np"}), text_channel


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["no_message", "resend_fails"])
async def test_a_static_player_never_posts_its_own_message_when_the_panel_cannot_be_resent(outcome):
    from empiremuse.utils.music.models import np_steps

    resend = AsyncMock(return_value=None) if outcome == "no_message" else AsyncMock(side_effect=RuntimeError("falhou"))
    player, req, text_channel = static_player(resend)

    await np_steps._np_send_new(player, req)

    text_channel.send.assert_not_awaited()  # só o autor da mensagem (o bot do painel) pode mandar/editar o painel
    assert player.message is None


@pytest.mark.asyncio
async def test_a_static_player_takes_the_resent_panel_message_and_edits_it():
    from empiremuse.utils.music.models import np_steps

    sent = SimpleNamespace(id=9, edit=AsyncMock())
    player, req, text_channel = static_player(AsyncMock(return_value=sent))

    await np_steps._np_send_new(player, req)

    assert player.message is sent and sent.edit.await_args.kwargs["content"] == "np"
    text_channel.send.assert_not_awaited()


# ---------------------------------------------------------------------- check_channel: quem toca e não é o painel só usa o canal


def channel_check(*, panel_bot, channel_db):
    request = SimpleNamespace(is_panel_bot=AsyncMock(return_value=panel_bot))
    bot = SimpleNamespace(
        user=SimpleNamespace(id=1),
        pool=SimpleNamespace(servers=SimpleNamespace(jukebox=request)),
        get_channel=lambda cid: channel_db,
        fetch_channel=AsyncMock(),
    )
    dispatch = ControllerDispatchMixin.__new__(ControllerDispatchMixin)
    dispatch.reset_controller_db = AsyncMock()
    everything_allowed = MagicMock()  # qualquer permissão consultada vale True
    server = SimpleNamespace(id=GID, me=object())
    other_channel = MagicMock(spec=disnake.TextChannel)
    other_channel.id, other_channel.guild = 1, server
    other_channel.permissions_for.return_value = everything_allowed
    inter = SimpleNamespace(guild_id=GID, channel_id=1, channel=other_channel)
    guild_data = {"player_controller": {"channel": str(PANEL_CHANNEL), "message_id": "1"}}
    guild = SimpleNamespace(me=SimpleNamespace(guild_permissions=SimpleNamespace(read_message_history=False)))
    return dispatch, bot, inter, guild_data, guild, other_channel


@pytest.mark.asyncio
async def test_a_bot_that_is_not_the_panel_bot_only_uses_the_channel_without_resetting_or_creating_a_post():
    forum = MagicMock(spec=disnake.ForumChannel)
    forum.create_thread = AsyncMock()
    thread = MagicMock(spec=disnake.Thread)
    thread.id, thread.owner_id, thread.guild, thread.parent = 22, 2, SimpleNamespace(id=GID), forum
    dispatch, bot, inter, data, guild, other = channel_check(panel_bot=False, channel_db=thread)

    channel_db, warn, message = await dispatch.check_channel(data, inter, other, guild, bot)

    assert channel_db is thread and warn is None and message is None
    dispatch.reset_controller_db.assert_not_awaited()
    forum.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_panel_bot_that_already_owns_the_post_does_not_create_another_one():
    forum = MagicMock(spec=disnake.ForumChannel)
    forum.create_thread = AsyncMock()
    thread = MagicMock(spec=disnake.Thread)
    thread.id, thread.owner_id, thread.parent = 22, 1, forum
    thread.guild = SimpleNamespace(id=GID, me=object())
    thread.locked = thread.archived = False
    thread.permissions_for.return_value = MagicMock()
    dispatch, bot, inter, data, guild, other = channel_check(panel_bot=True, channel_db=thread)

    channel_db, _, _ = await dispatch.check_channel(data, inter, other, guild, bot)

    assert channel_db is thread  # owner_id (um int) é comparado com o id do bot, não o objeto .owner
    forum.create_thread.assert_not_awaited()
    dispatch.reset_controller_db.assert_not_awaited()


# ---------------------------------------------------------------------- cliques: a ajuda não precisa de call, e o clique roteado vale no outro bot


@pytest.mark.asyncio
async def test_the_help_button_answers_even_without_a_call_and_nothing_else_happens():
    self_, req, other, interaction = click(call=None)
    req.control = controller_buttons.PlayerControls.help_button
    helped = AsyncMock()
    original = controller_buttons._pc_help_button
    controller_buttons._pc_help_button = helped
    try:
        assert await controller_buttons._pc_route_panel_click(self_, req) is True
    finally:
        controller_buttons._pc_help_button = original

    helped.assert_awaited_once_with(self_, req)
    interaction.send.assert_not_awaited()
    other.cog.player_controller.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_routed_click_is_marked_so_the_other_bot_does_not_drop_it_for_not_being_its_players_message():
    self_, req, other, interaction = click(call=200)
    await controller_buttons._pc_route_panel_click(self_, req)
    assert interaction.panel_click is True

    # no outro bot: a mensagem do clique não é a do player, mas com a marca o clique segue
    interaction.message = "mensagem do painel"
    player = SimpleNamespace(
        message="mensagem do player",
        static=True,
        interaction_cooldown=False,
        guild=SimpleNamespace(me=SimpleNamespace(voice=SimpleNamespace(channel="call"))),
    )
    request = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(message_id=None)))
    me = SimpleNamespace(
        bot=SimpleNamespace(
            music=SimpleNamespace(players={GID: player}),
            pool=SimpleNamespace(servers=SimpleNamespace(jukebox=request)),
        ),
    )
    req.control = controller_buttons.PlayerControls.pause_resume
    req.player = None
    dropped = await controller_buttons._pc_player_checks(me, req)
    interaction.panel_click = False
    dropped_without_mark = await controller_buttons._pc_player_checks(me, req)
    assert dropped_without_mark is True and dropped is not True


# ---------------------------------------------------------------------- /setup e /reset valem para os players de TODOS os bots


def players_with_two_bots():
    me, other = make_bot(1, "TOKEN", call=100), make_bot(2, "TOKEN_2", call=200)
    for bot in (me, other):
        player = bot.music.players[GID]
        player.setup_hints, player.setup_features = MagicMock(), MagicMock()
        player.process_hint, player.invoke_np = MagicMock(), AsyncMock()
        player.text_channel = player.message = "antes"
    panel = SimpleNamespace(
        players=lambda gid: [(me, me.music.players[GID]), (other, other.music.players[GID])], refresh=AsyncMock()
    )
    me.pool = SimpleNamespace(jukebox_panel=panel)
    return me, other, panel


@pytest.mark.asyncio
async def test_setup_rebinds_the_players_of_the_other_bots_to_the_new_channel_and_refreshes_the_panel():
    from empiremuse.utils.music import jukebox_channel

    me, other, panel = players_with_two_bots()
    channel = SimpleNamespace(id=22, guild=SimpleNamespace(id=GID))
    other_view_of_channel = SimpleNamespace(id=22)  # o outro bot enxerga o canal pelo cache dele
    other.get_channel = lambda cid: other_view_of_channel
    other.music.players[GID].last_data = {"skin": "antiga"}
    message = SimpleNamespace(id=555)

    await jukebox_channel._rebind_other_players(me, channel, message)

    other_player, own_player = other.music.players[GID], me.music.players[GID]
    assert other_player.text_channel is other_view_of_channel and other_player.message is message
    assert other_player.last_data == {}  # o painel novo está ocioso: o redesenho não pode ser pulado
    other_player.setup_hints.assert_called_once()
    other_player.setup_features.assert_called_once()
    assert own_player.text_channel == "antes"  # o do próprio bot o comando já cuida
    panel.refresh.assert_awaited_once_with(GID, now=True)


@pytest.mark.asyncio
async def test_reset_turns_the_players_of_the_other_bots_back_into_normal_ones():
    from empiremuse.utils.music import jukebox_channel

    me, other, panel = players_with_two_bots()
    other.music.players[GID].static = me.music.players[GID].static = True
    other.get_channel = lambda cid: None
    inter_channel = SimpleNamespace(id=33)

    await jukebox_channel._reset_other_players(me, GID, inter_channel)

    other_player = other.music.players[GID]
    assert other_player.static is False and other_player.message is None and other_player.text_channel is inter_channel
    assert me.music.players[GID].static is True  # o do bot do comando é tratado à parte
    other_player.invoke_np.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_one_failing_player_does_not_stop_the_others_from_being_rebound(caplog):
    from empiremuse.utils.music import jukebox_channel

    me, other, panel = players_with_two_bots()
    third = make_bot(3, "TOKEN_3", call=300)
    third.music.players[GID].setup_hints = MagicMock(side_effect=RuntimeError("falhou"))
    third.music.players[GID].setup_features = MagicMock()
    panel.players = lambda gid: [
        (me, me.music.players[GID]),
        (third, third.music.players[GID]),
        (other, other.music.players[GID]),
    ]
    channel = SimpleNamespace(id=22, guild=SimpleNamespace(id=GID))
    third.get_channel = other.get_channel = lambda cid: channel

    await jukebox_channel._rebind_other_players(me, channel, SimpleNamespace(id=1))

    other.music.players[GID].setup_features.assert_called_once()  # o 2º segue mesmo com o 1º falhando
    panel.refresh.assert_awaited_once_with(GID, now=True)


# ---------------------------------------------------------------------- correções do CodeRabbit: painel novo, tarefas e encerramento


def static_players_world(*, resent, newest=None):
    one, two = make_bot(1, "TOKEN", call=100), make_bot(2, "TOKEN_2", call=200)
    for bot in (one, two):
        bot.music.players[GID].message = "mensagem antiga"
        bot.music.players[GID].last_data = {"skin": "antiga"}
    panel, pool = panel_world(one, two)
    after = SimpleNamespace(channel=PANEL_CHANNEL, message_id=getattr(newest or resent, "id", PANEL_MESSAGE))
    pool.servers.jukebox.get.side_effect = [after]  # depois do reenvio: o que ficou gravado
    pool.servers.jukebox.elect.return_value = SimpleNamespace(bot=object(), changed=True)
    panel.resend = AsyncMock(return_value=resent)
    panel.refresh = AsyncMock()
    return panel, one, two


@pytest.mark.asyncio
async def test_after_the_panel_is_resent_every_static_player_edits_the_new_message_and_is_redrawn_at_once():
    new = SimpleNamespace(id=777)
    panel, one, two = static_players_world(resent=new)

    await panel.handoff(GID)

    panel.resend.assert_awaited_once_with(
        GID
    )  # sem id para substituir: se o painel já é do bot, o reenvio devolve o existente
    for bot in (one, two):
        player = bot.music.players[GID]
        assert player.message is new and player.last_data == {}  # "nada mudou" não pode pular o redesenho
    panel.refresh.assert_awaited_once_with(GID, now=True)


@pytest.mark.asyncio
async def test_a_resend_that_lost_the_race_to_a_newer_one_does_not_rebind_the_players_to_the_deleted_message():
    older, newer = SimpleNamespace(id=777), SimpleNamespace(id=888)
    panel, one, two = static_players_world(resent=older, newest=newer)

    await panel.handoff(GID)

    for bot in (one, two):
        assert bot.music.players[GID].message == "mensagem antiga"
    panel.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_when_nothing_could_be_resent_the_players_keep_what_they_had():
    panel, one, two = static_players_world(resent=None)

    await panel.handoff(GID)

    assert one.music.players[GID].message == "mensagem antiga" and one.music.players[GID].last_data == {
        "skin": "antiga"
    }
    panel.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_drawing_the_list_resets_each_players_cache_so_going_back_to_one_player_redraws_its_skin(monkeypatch):
    from empiremuse.utils.music.interactions import jukebox_panel as ui
    from empiremuse.utils import server_playlists

    one, two = make_bot(1, "TOKEN", call=100), make_bot(2, "TOKEN_2", call=200)
    for bot in (one, two):
        bot.music.players[GID].last_data = {"skin": "antiga"}
    panel, pool = panel_world(one, two)
    pool.servers.jukebox.panel_bot = AsyncMock(return_value=one)
    message = SimpleNamespace(guild=SimpleNamespace(id=GID, preferred_locale="pt-BR"), edit=AsyncMock())
    monkeypatch.setattr(panel_module, "fetch_panel_message", AsyncMock(return_value=message))
    monkeypatch.setattr(ui, "render_panel", lambda *args, **kwargs: {"content": "lista"})
    monkeypatch.setattr(server_playlists, "safe_list", AsyncMock(return_value=()))
    monkeypatch.setattr(server_playlists, "select_options", lambda *args, **kwargs: [])

    await panel._draw(GID)

    message.edit.assert_awaited_once()
    assert message.edit.await_args.kwargs["allowed_mentions"].everyone is False
    assert one.music.players[GID].last_data == {} and two.music.players[GID].last_data == {}


def test_one_player_in_an_open_call_keeps_its_skin_but_in_a_private_call_the_list_takes_over():  # S-V7
    open_call, hidden_call = make_bot(1, "TOKEN", call=100), make_bot(1, "TOKEN", call=100, private=True)
    assert not panel_world(open_call)[0].takes_over(GID)
    assert panel_world(hidden_call)[0].takes_over(GID)


@pytest.mark.asyncio
async def test_one_player_in_a_private_call_is_drawn_as_the_list_with_no_track_or_requester(monkeypatch):  # S-V7
    from empiremuse.utils.music.interactions import jukebox_panel as ui
    from empiremuse.utils import server_playlists

    track = SimpleNamespace(title="Faixa secreta", uri="https://exemplo.test/x", requester="7")
    one = make_bot(1, "TOKEN", call=100, private=True)
    one.music.players[GID].current = track
    one.music.players[GID].invoke_np = AsyncMock()
    panel, pool = panel_world(one)
    pool.servers.jukebox.panel_bot = AsyncMock(return_value=one)
    message = SimpleNamespace(guild=SimpleNamespace(id=GID, preferred_locale="pt-BR"), edit=AsyncMock())
    drawn = []
    monkeypatch.setattr(panel_module, "fetch_panel_message", AsyncMock(return_value=message))
    monkeypatch.setattr(ui, "render_panel", lambda panel_bot, guild, entries, **kw: drawn.extend(entries) or {})
    monkeypatch.setattr(server_playlists, "safe_list", AsyncMock(return_value=()))
    monkeypatch.setattr(server_playlists, "select_options", lambda *args, **kwargs: [])

    await panel._draw(GID)

    one.music.players[GID].invoke_np.assert_not_awaited()  # a skin (que mostra a faixa) não é usada
    assert len(drawn) == 1 and drawn[0].private and (drawn[0].title, drawn[0].url, drawn[0].requester) == (None,) * 3
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_closing_the_panel_cancels_its_background_tasks():
    panel, _ = panel_world()
    gate = asyncio.Event()

    async def forever():
        await gate.wait()

    task = panel._spawn(forever())
    await asyncio.sleep(0)
    panel.close()
    await asyncio.sleep(0)  # deixa o cancelamento chegar à tarefa
    cancelled = task.cancelling() > 0
    task.cancel()  # limpeza: se a asserção falhar, a tarefa não fica pendurada
    assert cancelled


@pytest.mark.asyncio
async def test_the_pool_shutdown_closes_the_request_panel_before_closing_the_bots():
    from empiremuse.utils.client import BotPool

    order = []
    closing_bot = SimpleNamespace(is_closed=lambda: False, close=AsyncMock(side_effect=lambda: order.append("bot")))
    pool = SimpleNamespace(
        heartbeat_task=None,
        logs_task=None,
        update_watch_task=None,
        version_check_task=None,
        playlist_icons_task=None,
        helper_watch_tasks=set(),
        jukebox_panel=SimpleNamespace(close=lambda: order.append("panel")),
        i18n=SimpleNamespace(close=lambda: order.append("i18n")),
        get_all_bots=lambda: [closing_bot],
        lavalink_instance=None,
        usage=None,
    )

    await BotPool.shutdown(pool)

    assert order == ["panel", "i18n", "bot"]  # as leituras de idioma pendentes são canceladas no encerramento


@pytest.mark.asyncio
async def test_unloading_the_guild_log_cancels_the_roster_tasks():
    from empiremuse.cogs.misc import GuildLog

    async def forever():
        await asyncio.Event().wait()

    tasks = {asyncio.get_running_loop().create_task(forever()) for _ in range(2)}
    await asyncio.sleep(0)
    GuildLog.cog_unload(SimpleNamespace(_roster_tasks=tasks))
    await asyncio.sleep(0)
    cancelled = all(task.cancelling() > 0 for task in tasks)
    for task in tasks:
        task.cancel()  # limpeza: nada fica pendurado se a asserção falhar
    assert cancelled


# ---------------------------------------------------------------------- /setup e /reset: uma operação por vez no servidor (CodeRabbit #159)


def operation_world(stored):
    """Um bot cujo ``pool.servers.jukebox`` é o ``ServerJukebox`` de verdade (só a leitura do banco é falsa)."""
    from empiremuse.utils.server_jukebox import ServerJukebox

    request = ServerJukebox(SimpleNamespace(pool=None))
    request.get = AsyncMock(return_value=stored)
    return SimpleNamespace(pool=SimpleNamespace(servers=SimpleNamespace(jukebox=request)))


def config_of(message):
    from empiremuse.utils.server_jukebox import JukeboxConfig

    return JukeboxConfig(PANEL_CHANNEL, message, None, 1)


@pytest.mark.asyncio
async def test_setup_and_reset_never_run_at_the_same_time_on_the_same_server():
    from empiremuse.utils.music import jukebox_channel as rc

    bot = operation_world(config_of(PANEL_MESSAGE))
    first_in, release, order = asyncio.Event(), asyncio.Event(), []

    async def setup():
        async with rc.exclusive_operation(bot, GID, config_of(PANEL_MESSAGE)):
            order.append("setup começou")
            first_in.set()
            await release.wait()
            order.append("setup terminou")

    reset_tried = asyncio.Event()

    async def reset():
        reset_tried.set()  # a tarefa já está rodando e vai pedir a trava
        async with rc.exclusive_operation(bot, GID, config_of(PANEL_MESSAGE)):
            order.append("reset começou")

    first = asyncio.ensure_future(setup())
    await asyncio.wait_for(first_in.wait(), 5)
    second = asyncio.ensure_future(reset())
    await asyncio.wait_for(reset_tried.wait(), 5)
    assert not second.done() and order == ["setup começou"]  # o reset tentou e espera a trava

    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert order == ["setup começou", "setup terminou", "reset começou"]


@pytest.mark.asyncio
async def test_a_stale_config_is_refused_inside_the_lock_before_any_effect_on_discord():
    from empiremuse.utils.music import jukebox_channel as rc
    from empiremuse.utils.servers import ServerConfigConflict

    bot = operation_world(config_of(999))  # um setup concorrente já gravou outra mensagem
    effects = []

    with pytest.raises(ServerConfigConflict):
        async with rc.exclusive_operation(bot, GID, config_of(PANEL_MESSAGE)):  # o comando leu a mensagem antiga
            effects.append("apagou o canal")

    assert effects == []
    assert not bot.pool.servers.jukebox.operation(GID).locked()  # e a trava foi solta


@pytest.mark.asyncio
@pytest.mark.parametrize(("existing", "deleted"), [(False, True), (True, False)], ids=["created_here", "pre_existing"])
async def test_a_setup_that_loses_the_race_deletes_only_the_channel_it_created(monkeypatch, existing, deleted):
    from empiremuse.utils.music import jukebox_channel as rc
    from empiremuse.utils.servers import ServerConfigConflict

    bot = operation_world(config_of(999))
    inner = AsyncMock()
    monkeypatch.setattr(rc, "_apply_jukebox_channel", inner)
    target = SimpleNamespace(
        id=PANEL_CHANNEL + 1, delete=AsyncMock()
    )  # o canal configurado agora é outro (PANEL_CHANNEL)

    with pytest.raises(ServerConfigConflict):
        await rc.apply_jukebox_channel(
            bot, SimpleNamespace(id=GID), object(), target, existing_channel=existing, config=config_of(PANEL_MESSAGE)
        )

    inner.assert_not_awaited()  # nenhum efeito do setup
    assert target.delete.await_count == (1 if deleted else 0)


@pytest.mark.asyncio
async def test_a_created_channel_that_another_operation_meanwhile_configured_is_not_deleted():  # CodeRabbit #159
    from empiremuse.utils.music import jukebox_channel as rc
    from empiremuse.utils.servers import ServerConfigConflict

    bot = operation_world(config_of(999))  # a outra operação já configurou o canal PANEL_CHANNEL
    target = SimpleNamespace(id=PANEL_CHANNEL, delete=AsyncMock())  # o mesmo canal que este /setup tinha criado

    with pytest.raises(ServerConfigConflict):
        await rc.apply_jukebox_channel(
            bot, SimpleNamespace(id=GID), object(), target, existing_channel=False, config=config_of(PANEL_MESSAGE)
        )

    target.delete.assert_not_awaited()
    assert not bot.pool.servers.jukebox.operation(GID).locked()


@pytest.mark.asyncio
async def test_the_operation_lock_is_one_per_server_and_the_same_for_the_same_server():
    request = operation_world(config_of(PANEL_MESSAGE)).pool.servers.jukebox
    assert request.operation(GID) is request.operation(GID)
    assert request.operation(GID) is not request.operation(GID + 1)  # um servidor não segura o outro


@pytest.mark.asyncio
async def test_setup_and_reset_run_their_steps_holding_the_servers_operation_lock(monkeypatch):
    from empiremuse.utils.music import jukebox_channel as rc

    bot = operation_world(config_of(PANEL_MESSAGE))
    held = {}

    async def spy(name, *args, **kwargs):
        held[name] = bot.pool.servers.jukebox.operation(GID).locked()

    async def setup_step(*args, **kwargs):
        await spy("setup")

    async def reset_step(*args, **kwargs):
        await spy("reset")

    monkeypatch.setattr(rc, "_apply_jukebox_channel", setup_step)
    monkeypatch.setattr(rc, "_jukebox_reset", reset_step)
    guild = SimpleNamespace(id=GID)

    await rc.apply_jukebox_channel(
        bot, guild, object(), object(), existing_channel=True, config=config_of(PANEL_MESSAGE)
    )
    await rc.jukebox_reset(
        bot,
        guild,
        object(),
        object(),
        delete_channel=None,
        channel_inter=object(),
        config=config_of(PANEL_MESSAGE),
        notify=None,
    )

    assert held == {"setup": True, "reset": True}
    assert not bot.pool.servers.jukebox.operation(GID).locked()  # e soltou no fim


@pytest.mark.asyncio
async def test_a_reset_with_a_stale_config_is_refused_before_touching_discord(monkeypatch):
    from empiremuse.utils.music import jukebox_channel as rc
    from empiremuse.utils.servers import ServerConfigConflict

    bot = operation_world(config_of(999))
    inner = AsyncMock()
    monkeypatch.setattr(rc, "_jukebox_reset", inner)

    with pytest.raises(ServerConfigConflict):
        await rc.jukebox_reset(
            bot,
            SimpleNamespace(id=GID),
            object(),
            object(),
            delete_channel="sim",
            channel_inter=object(),
            config=config_of(PANEL_MESSAGE),
            notify=None,
        )

    inner.assert_not_awaited()  # nada de apagar o canal de uma configuração que já é outra
