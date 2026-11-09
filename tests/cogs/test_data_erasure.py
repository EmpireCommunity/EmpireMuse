"""E2 (T-494): slash ``/apagar_meus_dados``, concessão do dono (§3.4) e comando ``apagardados`` em DM. Serviço, banco e Discord são falsos.

Só o clique do TITULAR apaga; o clique do dono sozinho nunca (T11, T12/R1, R7)."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest
from disnake.ext import commands

from empiremuse import paths
from empiremuse.cogs import data_erasure
from empiremuse.cogs.legacy_cmds import Owner
from empiremuse.i18n import get_i18n
from empiremuse.rules.erasure import (
    ERASE_COMMAND_NAMES,
    OWNER_GRANT_TTL_S,
    ErasureReport,
    ErasureStatus,
    grant_log_ref,
    owner_target_id,
    parse_owner_request,
)
from empiremuse.utils import erasure_views as views
from empiremuse.utils.erasure import ErasureBusy, ErasureRateLimited
from empiremuse.utils.erasure_grants import GrantLimited, OwnerGrants

pytestmark = pytest.mark.critical

OWNER = 111111111111111111
HOLDER = 222222222222222222
OTHER = 333333333333333333


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def world(status=ErasureStatus.OK, counts=None):
    clock = Clock()
    report = ErasureReport(status, counts or {"users": 7}, () if status is ErasureStatus.OK else ("radios",))
    pool = SimpleNamespace(
        i18n=get_i18n(),
        erasure=SimpleNamespace(erase_user=AsyncMock(return_value=report)),
        erasure_grants=OwnerGrants(clock=clock),
    )
    bot = SimpleNamespace(
        pool=pool, env_owner_ids={OWNER}, default_prefix="!!", fetch_user=AsyncMock(), is_reserve=False
    )
    return SimpleNamespace(pool=pool, bot=bot, clock=clock, tr=get_i18n().for_locale("pt-BR"))


def click(user_id, locale=disnake.Locale.pt_BR):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id),
        locale=locale,
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        edit_original_message=AsyncMock(),
    )


def final_text(interaction):
    return interaction.edit_original_message.await_args.kwargs["content"]


# --------------------------------------------------------------------- T11: slash


@pytest.mark.asyncio
async def test_slash_confirm_erases_the_clicking_author_only():
    w = world()
    view = views.ErasureConfirmView(w.bot, w.tr, HOLDER)
    inter = click(HOLDER)

    assert await view.interaction_check(inter) is True
    await view.confirmed(inter)

    w.pool.erasure.erase_user.assert_awaited_once_with(HOLDER, source="slash")
    inter.response.edit_message.assert_awaited_once()  # reconhece antes da varredura lenta
    assert final_text(inter) == w.tr("erasure.done")


@pytest.mark.asyncio
async def test_slash_button_of_another_user_is_refused_and_erases_nothing():
    w = world()
    view = views.ErasureConfirmView(w.bot, w.tr, HOLDER)
    intruder = click(OTHER)

    assert await view.interaction_check(intruder) is False

    intruder.response.send_message.assert_awaited_once()
    w.pool.erasure.erase_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_and_timeout_erase_nothing_and_timeout_clears_the_message():
    w = world()
    origin = click(HOLDER)
    view = views.ErasureConfirmView(w.bot, w.tr, HOLDER, origin=origin)
    inter = click(HOLDER)

    await view.cancelled(inter)
    await view.on_timeout()

    w.pool.erasure.erase_user.assert_not_awaited()
    origin.edit_original_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_slash_command_replies_ephemerally_without_calling_the_service():
    w = world()
    cog = data_erasure.DataErasure(w.bot)
    inter = SimpleNamespace(
        locale=disnake.Locale.pt_BR,
        author=SimpleNamespace(id=HOLDER),
        guild=None,  # DM com o bot
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await cog.erase_my_data.callback(cog, inter)

    assert inter.response.send_message.await_args.kwargs["ephemeral"] is True
    w.pool.erasure.erase_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_never_sees_counts_and_partial_does_not_promise_a_retry_fixes_it():
    w = world(ErasureStatus.PARTIAL, {"users": 12345, "radios": 987})
    view = views.ErasureConfirmView(w.bot, w.tr, HOLDER)
    inter = click(HOLDER)

    await view.confirmed(inter)

    text = final_text(inter)
    assert text == w.tr("erasure.partial")
    assert "12345" not in text and "987" not in text
    assert "tente de novo" not in text.lower() and "repet" not in text.lower()


@pytest.mark.asyncio
async def test_rate_limit_busy_and_unexpected_error_give_fixed_texts_without_exception_text():
    w = world()
    view_texts = []
    for error in (ErasureRateLimited(125), ErasureBusy(), RuntimeError("segredo-da-excecao")):
        w.pool.erasure.erase_user.side_effect = error
        view = views.ErasureConfirmView(w.bot, w.tr, HOLDER)
        inter = click(HOLDER)
        await view.confirmed(inter)
        view_texts.append(final_text(inter))

    assert "3 min" in view_texts[0]
    assert view_texts[1] == w.tr("erasure.busy")
    assert view_texts[2] == w.tr("erasure.failed") and "segredo" not in view_texts[2]


def test_command_names_in_the_catalogs_match_the_single_constant():
    for locale, name in ERASE_COMMAND_NAMES.items():
        catalog = json.loads((paths.PACKAGE_DIR / "locales" / locale / "commands.json").read_text(encoding="utf-8"))
        assert catalog["cmd.delete_my_data.name"] == name


# --------------------------------------------------------------------- concessões


def test_grant_is_single_use_tied_to_the_holder_and_expires():
    clock = Clock()
    grants = OwnerGrants(clock=clock)
    grant = grants.issue(OWNER, HOLDER)

    assert grants.redeem(grant.grant_id, OTHER) is None  # outro usuário não gasta a concessão
    assert grants.redeem(grant.grant_id, HOLDER) == grant
    assert grants.redeem(grant.grant_id, HOLDER) is None  # já usada

    second = grants.issue(OWNER, HOLDER)
    clock.t += OWNER_GRANT_TTL_S
    assert grants.redeem(second.grant_id, HOLDER) is None  # expirada


@pytest.mark.parametrize("code", ["", " ", "   ", "\n", None, 7, b"x" * 20, "a b" * 8, " " + "a" * 22])
def test_grant_code_blank_or_malformed_is_refused(code):
    grants = OwnerGrants()
    grants.issue(OWNER, HOLDER)

    assert grants.redeem(code, HOLDER) is None


@pytest.mark.parametrize("owner,target", [(0, HOLDER), (OWNER, -1), (True, HOLDER), (OWNER, "5"), (OWNER, 2**64)])
def test_grant_rejects_invalid_ids(owner, target):
    with pytest.raises(ValueError):
        OwnerGrants().issue(owner, target)


def test_grant_limits_ten_per_hour_per_owner_and_one_pending_per_holder():
    clock = Clock()
    grants = OwnerGrants(clock=clock)
    first = grants.issue(OWNER, HOLDER)
    with pytest.raises(GrantLimited) as pending:
        grants.issue(OWNER, HOLDER)
    assert pending.value.reason == "target_pending"
    grants.revoke(first.grant_id)
    for offset in range(9):
        grants.issue(OWNER, 400000000000000000 + offset)
    with pytest.raises(GrantLimited) as limit:
        grants.issue(OWNER, 500000000000000000)
    assert limit.value.reason == "owner_limit"
    clock.t += 3600
    grants.issue(OWNER, 500000000000000000)


# --------------------------------------------------------------------- R1: titular confirma, dono não apaga


async def owner_flow(w, *, dm_fails=False):
    """O dono aperta *Enviar pedido*; devolve a view da DM do titular (ou None)."""
    target = SimpleNamespace(id=HOLDER, send=AsyncMock())
    if dm_fails:
        target.send.side_effect = disnake.Forbidden(MagicMock(status=403), "closed")
    owner_view = views.OwnerRequestView(w.bot, w.tr, OWNER, target)
    inter = click(OWNER)
    assert await owner_view.interaction_check(inter) is True
    await owner_view.send_request(inter)
    return owner_view, inter, target


@pytest.mark.asyncio
async def test_owner_click_alone_never_erases_and_sends_the_dm_to_the_holder():
    w = world()

    _, inter, target = await owner_flow(w)

    w.pool.erasure.erase_user.assert_not_awaited()
    holder_view = target.send.await_args.kwargs["view"]
    assert holder_view.holder_id == HOLDER and holder_view.grant.target_id == HOLDER
    assert final_text(inter) == w.tr("erasure.owner.sent")


@pytest.mark.asyncio
async def test_only_the_holder_click_erases_with_owner_source_and_audit_ids():
    w = world()
    _, _, target = await owner_flow(w)
    holder_view = target.send.await_args.kwargs["view"]
    grant = holder_view.grant

    assert await holder_view.interaction_check(click(OTHER)) is False
    w.pool.erasure.erase_user.assert_not_awaited()
    await holder_view.confirmed(click(HOLDER))

    w.pool.erasure.erase_user.assert_awaited_once_with(HOLDER, source="owner", owner_id=OWNER, grant_id=grant.grant_id)


@pytest.mark.asyncio
async def test_expired_or_reused_grant_has_no_effect():
    w = world()
    _, _, target = await owner_flow(w)
    holder_view = target.send.await_args.kwargs["view"]
    await holder_view.confirmed(click(HOLDER))
    w.pool.erasure.erase_user.reset_mock()

    again = click(HOLDER)
    await holder_view.confirmed(again)  # reuso
    w.clock.t += OWNER_GRANT_TTL_S
    _, _, target2 = await owner_flow(w)
    w.clock.t += OWNER_GRANT_TTL_S
    late = click(HOLDER)
    await target2.send.await_args.kwargs["view"].confirmed(late)  # expirada

    w.pool.erasure.erase_user.assert_not_awaited()
    assert again.response.edit_message.await_args.kwargs["content"] == w.tr("erasure.grant_expired")
    assert late.response.edit_message.await_args.kwargs["content"] == w.tr("erasure.grant_expired")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ErasureRateLimited(60), ErasureBusy()])
async def test_holder_click_blocked_by_limit_or_busy_keeps_the_grant_consumed_and_the_owner_can_reissue(error):
    w = world()
    _, _, target = await owner_flow(w)
    holder_view = target.send.await_args.kwargs["view"]
    w.pool.erasure.erase_user.side_effect = error
    first = click(HOLDER)

    await holder_view.confirmed(first)
    again = click(HOLDER)
    await holder_view.confirmed(again)  # a mesma concessão não volta

    assert final_text(first) == w.tr("erasure.owner.retry_holder")
    assert w.pool.erasure.erase_user.await_count == 1
    assert again.response.edit_message.await_args.kwargs["content"] == w.tr("erasure.grant_expired")
    w.pool.erasure_grants.issue(OWNER, HOLDER)  # o dono reemite na hora


@pytest.mark.asyncio
async def test_holder_cancel_revokes_the_pending_request():
    w = world()
    _, _, target = await owner_flow(w)
    holder_view = target.send.await_args.kwargs["view"]

    await holder_view.cancelled(click(HOLDER))

    w.pool.erasure_grants.issue(OWNER, HOLDER)  # não há mais pendente
    w.pool.erasure.erase_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_dm_warns_the_owner_and_leaves_nothing_pending():
    w = world()

    _, inter, _ = await owner_flow(w, dm_fails=True)

    assert final_text(inter) == w.tr("erasure.owner.dm_closed")
    w.pool.erasure_grants.issue(OWNER, HOLDER)
    w.pool.erasure.erase_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_view_refuses_anyone_but_the_owner():
    w = world()
    view = views.OwnerRequestView(w.bot, w.tr, OWNER, SimpleNamespace(id=HOLDER, send=AsyncMock()))

    assert await view.interaction_check(click(OTHER)) is False
    assert await view.interaction_check(click(HOLDER)) is False


# --------------------------------------------------------------------- entrada em DM (R1, R7)


@pytest.mark.parametrize(
    "content,expected",
    [
        ("!!apagardados 123456789012345678", "123456789012345678"),
        ("!!apagardados   123456789012345678  ", "123456789012345678"),
        ("!!apagardados", ""),
        ("!!apagardados\xa0123456789012345678", None),  # NBSP não separa
        ("!!apagardados\t123456789012345678", "123456789012345678"),
        (
            "!!apagardados 123456789012345678\nfoo",
            "123456789012345678\nfoo",
        ),  # segunda linha fica no argumento e é recusada
        ("!!apagardadosx 1", None),
        ("!apagardados 123456789012345678", None),
        ("apagardados 123456789012345678", None),
        ("!!APAGARDADOS 123456789012345678", None),
        ("oi", None),
    ],
)
def test_parse_owner_request_is_exact(content, expected):
    assert parse_owner_request(content, "!!") == expected


@pytest.mark.parametrize(
    "argument,expected",
    [
        ("123456789012345678", 123456789012345678),
        ("12345", None),
        ("", None),
        ("-123456789012345678", None),
        ("+123456789012345678", None),
        ("0123456789012345678", None),
        ("123456789012345678 9", None),
        ("１２３４５６７８９０１２３４５６７８", None),  # dígitos largos (não ASCII)
        ("99999999999999999999", None),  # 20 dígitos, passa de 64 bits
        ("<@123456789012345678>", None),
        ("123456789012345678\nfoo", None),
        ("\xa0123456789012345678", None),
    ],
)
def test_owner_target_id_is_a_strict_positive_snowflake(argument, expected):
    assert owner_target_id(argument) == expected


def listener_world(*, author=OWNER, dm=True, content="!!apagardados 123456789012345678", target=None):
    w = world()
    cog = Owner.__new__(Owner)
    cog.bot = w.bot
    cog.log_owner_command = MagicMock()
    channel = MagicMock(spec=disnake.DMChannel if dm else disnake.TextChannel)
    channel.send = AsyncMock()
    message = SimpleNamespace(
        channel=channel, guild=None if dm else object(), author=SimpleNamespace(id=author), content=content
    )
    w.bot.fetch_user.return_value = target or SimpleNamespace(
        id=123456789012345678,
        bot=False,
        display_name="Ana *x*",
        display_avatar=SimpleNamespace(url="https://cdn.example/a.png"),
        send=AsyncMock(),
    )
    return w, cog, message


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"author": OTHER}, {"dm": False}, {"author": OTHER, "dm": False}])
async def test_listener_ignores_non_owner_and_non_dm_in_silence(kwargs):
    w, cog, message = listener_world(**kwargs)

    await cog.erasure_request_dm(message)

    message.channel.send.assert_not_awaited()
    w.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_listener_ignores_other_messages_from_the_owner():
    w, cog, message = listener_world(content="oi")

    await cog.erasure_request_dm(message)

    message.channel.send.assert_not_awaited()
    w.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["!!apagardados", "!!apagardados abc", "!!apagardados 12345", "!!apagardados 1 2"])
async def test_listener_refuses_invalid_ids_without_looking_anyone_up(content):
    w, cog, message = listener_world(content=content)

    await cog.erasure_request_dm(message)

    message.channel.send.assert_awaited_once_with(w.tr("erasure.owner.invalid_id"))
    w.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_listener_unknown_user_and_bot_accounts_are_refused():
    w, cog, message = listener_world()
    w.bot.fetch_user.side_effect = disnake.NotFound(MagicMock(status=404), "unknown")
    await cog.erasure_request_dm(message)
    message.channel.send.assert_awaited_once_with(w.tr("erasure.owner.not_found"))

    w, cog, message = listener_world(
        target=SimpleNamespace(id=123456789012345678, bot=True, display_name="b", display_avatar=None)
    )
    await cog.erasure_request_dm(message)
    message.channel.send.assert_awaited_once_with(w.tr("erasure.owner.not_found"))


@pytest.mark.asyncio
async def test_listener_shows_the_resolved_user_to_the_owner_and_does_not_erase():
    w, cog, message = listener_world()

    await cog.erasure_request_dm(message)

    kwargs = message.channel.send.await_args.kwargs
    assert isinstance(kwargs["view"], views.OwnerRequestView)
    assert "123456789012345678" in kwargs["embed"].description and "Ana" in kwargs["embed"].description
    w.pool.erasure.erase_user.assert_not_awaited()
    cog.log_owner_command.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("display_name", "expected_in_text", "forbidden_in_text"),
    [
        ("@everyone", "@\u200beveryone", "@everyone"),
        ("**Ana**", "\\*\\*Ana\\*\\*", "**Ana**"),
        ("\u202e\u200b", "**123456789012345678** (`123456789012345678`)", "\u202e"),  # só invisíveis: cai no ID
    ],
)
async def test_listener_renders_the_target_name_as_untrusted_text(display_name, expected_in_text, forbidden_in_text):
    target = SimpleNamespace(
        id=123456789012345678,
        bot=False,
        display_name=display_name,
        display_avatar=SimpleNamespace(url="https://cdn.example/a.png"),
    )
    _, cog, message = listener_world(target=target)

    await cog.erasure_request_dm(message)

    description = message.channel.send.await_args.kwargs["embed"].description
    assert expected_in_text in description and forbidden_in_text not in description


@pytest.mark.asyncio
async def test_holder_dm_buttons_are_bilingual_and_the_replies_use_the_clicker_locale():
    w = world()
    fallback = w.pool.i18n.for_locale(None)  # a DM sai no idioma de reserva: o titular ainda não clicou
    grant = w.pool.erasure_grants.issue(OWNER, HOLDER)
    view = views.ErasureConfirmView(w.bot, fallback, HOLDER, grant=grant)
    labels = [item.label for item in view.children]
    assert all(" · " in label for label in labels)  # os dois idiomas no próprio rótulo

    inter = click(HOLDER, locale=disnake.Locale.pt_BR)
    await view.confirmed(inter)

    assert final_text(inter) == w.tr("erasure.done")  # ``w.tr`` é o pt-BR; o de reserva seria o en-US


def test_owner_audit_line_is_never_aggregated(caplog):
    cog = Owner.__new__(Owner)
    cog.bot = SimpleNamespace(pool=SimpleNamespace(secret_values=lambda: frozenset()))

    with caplog.at_level(logging.WARNING):
        cog.log_owner_command(SimpleNamespace(author=SimpleNamespace(id=OWNER)), "apagardados", "x")

    assert caplog.records[-1].empiremuse_emit is True


# --------------------------------------------------------------------- revisão T-494c


def test_real_command_declaration_allows_dm_and_keeps_the_cooldown():
    command = data_erasure.DataErasure.erase_my_data
    assert command.extras["allow_private"] is True
    assert (
        command.contexts.guild is True and command.contexts.bot_dm is True and command.contexts.private_channel is False
    )
    assert command.name == "delete_my_data"
    bucket = command._buckets  # um pedido a cada 15 s por usuário
    assert (bucket._cooldown.rate, bucket._cooldown.per, bucket._type) == (1, 15.0, commands.BucketType.user)


@pytest.mark.asyncio
async def test_view_timeouts_match_the_flow():
    w = world()
    grant = w.pool.erasure_grants.issue(OWNER, HOLDER)

    assert views.ErasureConfirmView(w.bot, w.tr, HOLDER).timeout == views.CONFIRM_TIMEOUT_S
    assert views.ErasureConfirmView(w.bot, w.tr, HOLDER, grant=grant).timeout == OWNER_GRANT_TTL_S


@pytest.mark.asyncio
async def test_holder_dm_timeout_revokes_the_grant_and_removes_the_buttons():
    w = world()
    _, _, target = await owner_flow(w)
    holder_view = target.send.await_args.kwargs["view"]
    holder_view.dm_message = SimpleNamespace(edit=AsyncMock())

    await holder_view.on_timeout()

    holder_view.dm_message.edit.assert_awaited_once_with(view=None)
    w.pool.erasure_grants.issue(OWNER, HOLDER)  # nada pendente


@pytest.mark.asyncio
async def test_owner_request_timeout_removes_the_buttons_even_if_the_edit_fails():
    w = world()
    view = views.OwnerRequestView(w.bot, w.tr, OWNER, SimpleNamespace(id=HOLDER))
    view.sent_message = SimpleNamespace(edit=AsyncMock(side_effect=disnake.HTTPException(MagicMock(status=500), "x")))

    await view.on_timeout()

    view.sent_message.edit.assert_awaited_once_with(view=None)


@pytest.mark.asyncio
async def test_ack_comes_before_the_dm_send():
    w = world()
    owner_inter = click(OWNER)
    seen = {}
    target = SimpleNamespace(id=HOLDER, send=AsyncMock())

    async def send(*args, **kwargs):
        seen["send"] = owner_inter.response.edit_message.await_count

    target.send.side_effect = send
    await views.OwnerRequestView(w.bot, w.tr, OWNER, target).send_request(owner_inter)

    assert seen == {"send": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_path", [False, True])
async def test_ack_comes_before_erase_user(owner_path):
    w = world()
    holder = click(HOLDER)
    seen = {}

    async def erase(*args, **kwargs):
        seen["erase"] = holder.response.edit_message.await_count
        return ErasureReport(ErasureStatus.OK, {}, ())

    w.pool.erasure.erase_user.side_effect = erase
    grant = w.pool.erasure_grants.issue(OWNER, HOLDER) if owner_path else None

    await views.ErasureConfirmView(w.bot, w.tr, HOLDER, grant=grant).confirmed(holder)

    assert seen == {"erase": 1}


@pytest.mark.asyncio
async def test_final_edit_failure_is_swallowed_and_only_the_type_is_logged(caplog):
    w = world()
    view = views.ErasureConfirmView(w.bot, w.tr, HOLDER)
    inter = click(HOLDER)
    inter.edit_original_message.side_effect = RuntimeError("texto-secreto")

    with caplog.at_level(logging.WARNING):
        await view.confirmed(inter)

    assert "RuntimeError" in caplog.text and "texto-secreto" not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_failure_after_issue_revokes_and_tells_the_owner(caplog):
    w = world()
    target = SimpleNamespace(id=HOLDER, send=AsyncMock(side_effect=RuntimeError("falha-interna")))
    view = views.OwnerRequestView(w.bot, w.tr, OWNER, target)
    inter = click(OWNER)

    with caplog.at_level(logging.WARNING):
        await view.send_request(inter)

    assert final_text(inter) == w.tr("erasure.owner.send_failed")
    assert "falha-interna" not in caplog.text
    w.pool.erasure_grants.issue(OWNER, HOLDER)  # revogada


@pytest.mark.asyncio
async def test_failure_of_the_ack_itself_also_revokes():
    w = world()
    target = SimpleNamespace(id=HOLDER, send=AsyncMock())
    view = views.OwnerRequestView(w.bot, w.tr, OWNER, target)
    inter = click(OWNER)
    inter.response.edit_message.side_effect = RuntimeError("ack")

    await view.send_request(inter)

    target.send.assert_not_awaited()
    w.pool.erasure_grants.issue(OWNER, HOLDER)


@pytest.mark.asyncio
async def test_issue_writes_an_unaggregated_audit_line(caplog):
    w = world()

    with caplog.at_level(logging.WARNING):
        _, _, target = await owner_flow(w)

    record = next(r for r in caplog.records if "pedido do dono emitido" in r.getMessage())
    grant = target.send.await_args.kwargs["view"].grant
    assert record.empiremuse_emit is True
    assert f"owner={OWNER} target={HOLDER} grant={grant_log_ref(grant.grant_id)}" in record.getMessage()
    assert grant.grant_id not in caplog.text


@pytest.mark.asyncio
async def test_holder_replies_use_the_clicker_language_en_us():
    w = world()
    grant = w.pool.erasure_grants.issue(OWNER, HOLDER)
    view = views.ErasureConfirmView(w.bot, w.pool.i18n.for_locale(None), HOLDER, grant=grant)
    inter = click(HOLDER, locale=disnake.Locale.en_US)

    await view.confirmed(inter)

    assert final_text(inter) == w.pool.i18n.for_locale("en-US")("erasure.done")
    assert final_text(inter) != w.tr("erasure.done")


@pytest.mark.parametrize("locale", sorted(ERASE_COMMAND_NAMES))
def test_holder_dm_fits_discord_limit(locale):
    assert len(get_i18n().for_locale(locale)("erasure.owner.dm")) < 2000


@pytest.mark.asyncio
async def test_listener_to_view_carries_the_typed_id_the_owner_and_the_target():
    w, cog, message = listener_world()

    await cog.erasure_request_dm(message)

    w.bot.fetch_user.assert_awaited_once_with(123456789012345678)
    view = message.channel.send.await_args.kwargs["view"]
    assert view.target.id == 123456789012345678 and view.owner_id == OWNER
    assert view.sent_message is message.channel.send.return_value


@pytest.mark.asyncio
async def test_listener_stays_silent_on_reserve_bots():
    w, cog, message = listener_world()
    w.bot.is_reserve = True

    await cog.erasure_request_dm(message)

    message.channel.send.assert_not_awaited()
    w.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dm_messages", "reserve", "warns"), [(False, False, True), (True, False, False), (False, True, False)]
)
async def test_setup_warns_when_the_dm_intent_is_off(monkeypatch, caplog, dm_messages, reserve, warns):
    from empiremuse.cogs import legacy_cmds

    monkeypatch.setattr(legacy_cmds, "PanelView", MagicMock())
    w = world()
    w.bot.intents = SimpleNamespace(dm_messages=dm_messages)
    w.bot.is_reserve = reserve
    cog = Owner.__new__(Owner)
    cog.bot = w.bot

    with caplog.at_level(logging.WARNING):
        await cog.cog_load()

    assert ("DM_MESSAGES_INTENT" in caplog.text) is warns


# --------------------------------------------------------------------- ViewStore real (custom_id único e botão expirado)


def real_store(bot):
    store = disnake.ui.view.ViewStore(MagicMock())
    bot._connection = SimpleNamespace(_view_store=store)
    return store


def button_interaction(user_id, custom_id, message_id=None):
    inter = click(user_id)
    inter.data = SimpleNamespace(component_type=disnake.ComponentType.button, custom_id=custom_id)
    inter.message = SimpleNamespace(id=message_id) if message_id else None
    return inter


async def settle():
    import asyncio

    await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name().startswith("disnake-ui-view-dispatch")])


@pytest.mark.asyncio
async def test_two_members_slash_views_do_not_share_custom_ids_in_the_real_view_store():
    w = world()
    store = real_store(w.bot)
    mine = views.ErasureConfirmView(w.bot, w.tr, HOLDER)
    theirs = views.ErasureConfirmView(w.bot, w.tr, OTHER)
    store.add_view(mine)
    store.add_view(theirs)  # ainda sem ``message_id``, como o slash efêmero

    assert len({item.custom_id for item in mine.children} | {item.custom_id for item in theirs.children}) == 4
    store.dispatch(button_interaction(OTHER, theirs.children[0].custom_id))
    await settle()

    w.pool.erasure.erase_user.assert_awaited_once_with(
        OTHER, source="slash"
    )  # o clique de OTHER apaga OTHER, nunca HOLDER


@pytest.mark.asyncio
async def test_owner_view_cancel_and_pending_request_send_nothing():
    w = world()
    target = SimpleNamespace(id=HOLDER, send=AsyncMock())
    cancel = click(OWNER)
    await views.OwnerRequestView(w.bot, w.tr, OWNER, target).cancelled(cancel)
    w.pool.erasure_grants.issue(OWNER, HOLDER)  # já há um pendente
    pending = click(OWNER)
    await views.OwnerRequestView(w.bot, w.tr, OWNER, target).send_request(pending)

    target.send.assert_not_awaited()
    assert pending.response.edit_message.await_args.kwargs["content"] == w.tr("erasure.owner.pending")
    assert cancel.response.edit_message.await_args.kwargs["content"] == w.tr("erasure.owner.cancelled")


@pytest.mark.asyncio
async def test_failed_progress_ack_still_runs_the_service_and_delivers_the_outcome():
    w = world()
    grant = w.pool.erasure_grants.issue(OWNER, HOLDER)
    view = views.ErasureConfirmView(w.bot, w.tr, HOLDER, grant=grant)
    inter = click(HOLDER)
    inter.response.edit_message.side_effect = RuntimeError("ack")

    await view.confirmed(inter)

    w.pool.erasure.erase_user.assert_awaited_once_with(HOLDER, source="owner", owner_id=OWNER, grant_id=grant.grant_id)
    assert final_text(inter) == w.tr("erasure.done")


def test_grant_log_ref_is_a_short_stable_hash_not_the_code():
    ref = grant_log_ref("codigo-secreto-123456")

    assert ref == grant_log_ref("codigo-secreto-123456") and len(ref) == 12 and "secreto" not in ref
