"""panel/authz.py — GuildAuthorizer: matriz de autorização, cache de decisões e revogação.

Interpretações (contrato BRIEFING-PAINEL-E3.md): ver o relatório. Tudo com dublês; relógio injetado; sem rede.
"""

import aiohttp
import pytest

from tests.panel.helpers_servers import USER_ID, FakeBot, FakeGuild, FakePool, make_session, not_found, run
from tests.panel.helpers_login import FakeClock
from tests.panel.helpers import require

pytestmark = pytest.mark.critical

GID = "42"
UID = int(USER_ID)


def authz():
    return require("empiremuse.panel.authz")


def setup(*, owner=False, admin=False, manage=False, member=True, bot_ready=True, extra_bots=0, **kw):
    """Servidor 42 com um usuário (1001) e o(s) bot(s) do pool."""
    guild = FakeGuild(GID, name="Meu Servidor", owner_id=UID if owner else 999, **kw)
    if member:
        guild.add_member(UID, administrator=admin, manage_guild=manage)
    bots = [FakeBot(1, "Bot Um", ready=bot_ready, guilds=[guild])]
    bots += [FakeBot(10 + i, f"Bot {i}", guilds=[guild]) for i in range(extra_bots)]
    pool = FakePool(bots)
    clock = FakeClock()
    return guild, pool, clock, authz().GuildAuthorizer(pool, clock=clock)


def authorize(authorizer, session=None, guild_id=GID, **kwargs):
    return run(authorizer.authorize(session or make_session(guild_ids=(GID,)), guild_id, **kwargs))


# --------------------------------------------------------------------------- modelos


def test_guild_access_denied_is_an_exception():
    assert issubclass(authz().GuildAccessDenied, Exception)


def test_icon_url_is_none_when_guild_has_no_icon():
    assert authorize(setup(admin=True)[3]).icon_url is None


def test_only_ready_bots_are_listed():
    guild, pool, _, authorizer = setup(admin=True)
    pool.bots.append(FakeBot(77, "Bot Offline", ready=False, guilds=[guild]))
    assert [b.id for b in authorize(authorizer).bots] == ["1"]


def test_guild_whose_only_bot_is_not_ready_is_denied():
    _, _, _, authorizer = setup(admin=True, bot_ready=False)
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer)


# --------------------------------------------------------------------------- matriz de permissão


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # membro comum
        {"member": False},  # não é membro (get_member None e fetch NotFound)
    ],
)
def test_plain_member_or_non_member_is_denied(kwargs):
    with pytest.raises(authz().GuildAccessDenied):
        authorize(setup(**kwargs)[3])


def test_non_member_owner_is_denied():
    """Dono que não é mais membro atual: a regra (b) exige membro."""
    _, _, _, authorizer = setup(owner=True, member=False)
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer)


def test_empty_pool_is_denied():
    guild, pool, _, authorizer = setup(admin=True)
    pool.bots.clear()
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer)


@pytest.mark.smoke
def test_fetches_member_when_not_cached_and_accepts_admin():
    from tests.panel.helpers_servers import FakeMember

    guild, _, _, authorizer = setup(member=False)
    guild.fetchable[UID] = FakeMember(administrator=True)
    assert authorize(authorizer).guild_id == GID
    assert guild.calls["fetch_member"] == 1


@pytest.mark.parametrize(
    "failure",
    [
        not_found,
        lambda: aiohttp.ClientError("rede"),
    ],
    ids=["NotFound", "aiohttp"],
)
def test_fetch_member_failures_deny_access(failure):
    guild, _, _, authorizer = setup(member=False)
    guild.fetchable[UID] = failure()
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer)


# --------------------------------------------------------------------------- entrada: id inválido / fora da sessão


@pytest.mark.parametrize("bad_id", ["", None])
def test_invalid_guild_id_is_denied_without_any_discord_call(bad_id):
    guild, pool, _, authorizer = setup(admin=True)
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer, make_session(guild_ids=(GID, "")), bad_id)
    assert pool.api_calls() == 0, "id inválido não pode gerar chamadas ao bot/Discord"


def test_guild_outside_session_guilds_is_denied_without_any_discord_call():
    guild, pool, _, authorizer = setup(admin=True)
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer, make_session(guild_ids=("7", "8")), GID)  # o servidor 42 existe e o usuário é admin
    assert pool.api_calls() == 0


def test_bot_get_guild_receives_int_ids():
    """disnake só encontra servidores por int; passar str faria todo servidor parecer sem bot."""
    guild, _, _, authorizer = setup(admin=True)
    assert authorize(authorizer).guild_id == GID


# --------------------------------------------------------------------------- cache


def test_cached_read_still_allows_within_30_seconds_after_role_removal():
    guild, _, clock, authorizer = setup(admin=True)
    authorize(authorizer)
    guild.members[UID].guild_permissions.administrator = False
    clock.advance(29)
    assert authorize(authorizer).guild_id == GID  # janela de cache aceita pelo contrato (só leituras)


def test_cache_expires_after_30_seconds():
    guild, pool, clock, authorizer = setup(admin=True)
    authorize(authorizer)
    guild.members[UID].guild_permissions.administrator = False
    clock.advance(31)
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer)


def test_fresh_hits_the_gateway_even_when_cached():
    guild, _, _, authorizer = setup(admin=True)
    authorize(authorizer)
    authorize(authorizer, fresh=True)
    assert guild.calls["get_member"] == 2


def test_fresh_denial_invalidates_the_cached_positive_decision():
    guild, _, _, authorizer = setup(admin=True)
    authorize(authorizer)
    guild.members[UID].guild_permissions.administrator = False
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer, fresh=True)
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer)  # leitura normal não pode ressuscitar a decisão antiga


def test_cache_is_per_user_and_per_guild():
    guild, pool, _, authorizer = setup(admin=True)
    other_guild = FakeGuild("43", "Outro", owner_id=999)
    other_guild.add_member(UID, administrator=True)
    pool.bots[0].guilds[other_guild.id] = other_guild
    authorize(authorizer)
    assert authorize(authorizer, make_session(guild_ids=("43",)), "43").guild_id == "43"
    assert other_guild.calls["get_member"] == 1, "outro servidor não pode usar a decisão em cache do servidor 42"
    guild.add_member(2002)  # outro usuário, sem permissão
    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer, make_session(user_id="2002", guild_ids=(GID,)))


def test_cache_seconds_zero_disables_the_cache():
    guild, pool, clock, _ = setup(admin=True)
    authorizer = authz().GuildAuthorizer(pool, clock=clock, cache_seconds=0)
    authorize(authorizer)
    authorize(authorizer)
    assert guild.calls["get_member"] == 2


def test_cache_is_bounded(monkeypatch):
    """Limite de entradas (reduzido por monkeypatch; em produção é MAX_CACHE_ENTRIES = 10.000)."""
    monkeypatch.setattr(authz(), "MAX_CACHE_ENTRIES", 5)
    guild, _, _, authorizer = setup(admin=True)
    for uid in range(5000, 5000 + 8):
        guild.add_member(uid, administrator=True)
    first_user = make_session(user_id="5000", guild_ids=(GID,))
    authorize(authorizer, first_user)
    for uid in range(5001, 5000 + 8):
        authorize(authorizer, make_session(user_id=str(uid), guild_ids=(GID,)))
    before = guild.calls["get_member"]
    authorize(authorizer, first_user)
    assert guild.calls["get_member"] == before + 1, "a decisão mais antiga deveria ter sido descartada pelo limite"


@pytest.mark.regression
def test_erasure_or_ban_drops_the_cached_decisions_of_that_user_only():
    """Sentinela T-660 (achado anterior): o apagamento e o banimento (``destroy_user``) descartam as decisões em cache do usuário."""
    guild, _, clock, authorizer = setup(admin=True)
    authorize(authorizer)
    other = make_session(guild_ids=(GID,))
    other.user = type(other.user)(id="2002", username="b", global_name=None, avatar=None)
    guild.add_member(2002, administrator=True)
    authorize(authorizer, session=other)
    guild.members[UID].guild_permissions.administrator = False  # dentro da janela, o cache ainda aceitaria

    authorizer.purge_user(USER_ID)

    with pytest.raises(authz().GuildAccessDenied):
        authorize(authorizer)
    guild.members[2002].guild_permissions.administrator = False
    assert authorize(authorizer, session=other).guild_id == GID  # a decisão do OUTRO usuário segue em cache
