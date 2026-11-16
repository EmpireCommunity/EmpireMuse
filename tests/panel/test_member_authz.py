"""T-660: ``MemberAuthorizer`` (contrato r10 §2 c; threat model T1, T2, T4, T6, T7, T10, T11; CC2 e CC3). Relógio falso, sem espera real."""

import asyncio
import dataclasses
import logging

import aiohttp
import disnake
import pytest

from empiremuse.panel.authz import GuildAccessDenied, GuildAuthorizer
from empiremuse.panel.member_authz import (
    MemberAccessDenied,
    MemberAuthorizer,
    MemberBudget,
    MemberLookupUnavailable,
)
from tests.panel.helpers_login import FakeClock
from tests.panel.helpers_servers import (
    USER_ID,
    FakeBot,
    FakeGuild,
    FakeMember,
    FakePool,
    _response,
    forbidden,
    make_session,
    not_found,
    run,
)

pytestmark = pytest.mark.critical

GID = "42"
UID = int(USER_ID)


def member_session(guild_id=GID):
    """Sessão de quem é só membro de ``guild_id`` (fora de ``guilds``, dentro de ``member_guild_ids``)."""
    return dataclasses.replace(make_session(guild_ids=()), member_guild_ids=frozenset({guild_id}))


def setup(*, member=True, bot_ready=True, pending=False, cached=True, budget=None):
    guild = FakeGuild(GID, name="Comunidade", icon_hash="abc")
    person = FakeMember()
    person.pending = pending
    if member:
        guild.fetchable[UID] = person
        if cached:
            guild.members[UID] = person
    pool = FakePool([FakeBot(1, "Bot", ready=bot_ready, guilds=[guild])])
    clock = FakeClock()
    return guild, pool, clock, MemberAuthorizer(pool, clock=clock, budget=budget)


def authorize(authorizer, session=None, guild_id=GID, *, fresh=False):
    return run(authorizer.authorize(session or member_session(), guild_id, fresh=fresh)).access


# --------------------------------------------------------------------------- T2, T4: a ordem das conferências


def test_a_member_is_allowed_with_the_name_and_the_cdn_icon_only():
    _, _, _, authorizer = setup()

    access = authorize(authorizer)

    assert (access.guild_id, access.name) == (GID, "Comunidade")
    assert access.icon_url == f"https://cdn.discordapp.com/icons/{GID}/abc.png?size=64"


@pytest.mark.parametrize("guild_id", ["", "abc", "1" * 21, "42 ", 42])
def test_a_malformed_id_is_denied_without_any_discord_call(guild_id):
    guild, _, _, authorizer = setup()
    with pytest.raises(MemberAccessDenied):
        authorize(authorizer, guild_id=guild_id)
    assert guild.calls == {}


def test_an_id_outside_the_session_lists_is_denied_without_any_discord_call():
    """T4: o pré-filtro vem antes de qualquer consulta; só itens da própria lista podem tocar o Discord."""
    guild, _, _, authorizer = setup()
    with pytest.raises(MemberAccessDenied):
        authorize(authorizer, session=member_session("777"))
    assert guild.calls == {}


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"bot_ready": False}, "sem bot pronto"),
        ({"member": False}, "não é membro"),
        ({"pending": True}, "na triagem de membro"),
    ],
)
def test_every_definitive_denial_is_the_same_exception(kwargs, reason):
    _, _, _, authorizer = setup(**kwargs)
    with pytest.raises(MemberAccessDenied):
        authorize(authorizer)


def test_a_manager_listed_in_guilds_may_use_the_member_view_too():
    _, _, _, authorizer = setup()
    assert authorize(authorizer, session=make_session(guild_ids=(GID,))).guild_id == GID


# --------------------------------------------------------------------------- T2: escrita sempre fresca


def test_a_write_always_asks_discord_even_with_the_member_in_the_gateway_cache():
    guild, _, _, authorizer = setup()
    authorize(authorizer)  # leitura: o gateway basta
    assert guild.calls["fetch_member"] == 0

    authorize(authorizer, fresh=True)

    assert guild.calls["fetch_member"] == 1


def test_who_left_is_denied_on_write_at_once_and_the_denied_write_drops_the_cached_read():
    guild, _, clock, authorizer = setup()
    authorize(authorizer)
    guild.fetchable[UID] = not_found()  # saiu; o evento ainda não chegou ao gateway (a escrita confia só na API)

    with pytest.raises(MemberAccessDenied):
        authorize(authorizer, fresh=True)
    del guild.members[UID]  # o gateway também registrou a saída
    with pytest.raises(MemberAccessDenied):
        authorize(
            authorizer
        )  # Crivo CS1: sem a decisão guardada (a escrita negada a descartou), nada de 30 s de leitura velha


# --------------------------------------------------------------------------- CC2: só o definitivo é negativo; o resto é 503 sem cache


@pytest.mark.parametrize(
    "failure",
    [
        asyncio.TimeoutError(),
        aiohttp.ClientError("GET /guilds/42/members/1001"),
        disnake.HTTPException(_response(503, "Service Unavailable"), "down"),
        disnake.HTTPException(_response(429, "Too Many Requests"), "slow down"),
    ],
)
def test_a_transient_discord_failure_is_unavailable_and_never_cached(failure, caplog):
    guild, _, _, authorizer = setup(cached=False)
    guild.fetchable[UID] = failure

    with caplog.at_level(logging.WARNING), pytest.raises(MemberLookupUnavailable):
        authorize(authorizer)

    guild.fetchable[UID] = FakeMember()  # o Discord voltou: nada ficou guardado
    assert authorize(authorizer).guild_id == GID
    assert USER_ID not in caplog.text and "members/" not in caplog.text  # T11: só o tipo da exceção


@pytest.mark.parametrize("definitive", [not_found, forbidden])
def test_only_not_found_and_forbidden_become_the_cached_denial(definitive):
    guild, _, clock, authorizer = setup(cached=False)
    guild.fetchable[UID] = definitive()
    with pytest.raises(MemberAccessDenied):
        authorize(authorizer)

    guild.fetchable[UID] = FakeMember()
    with pytest.raises(MemberAccessDenied):
        authorize(authorizer)  # a negação vale os 30 s
    assert guild.calls["fetch_member"] == 1
    clock.advance(31)
    assert authorize(authorizer).guild_id == GID


def test_the_budget_is_global_per_bot_and_exhausted_means_unavailable():
    """T6: o orçamento de consultas ao Discord é do BOT, entre todos os usuários; esgotado = 503, sem negativo guardado."""
    clock = FakeClock()
    budget = MemberBudget(clock, burst=2, per_second=1.0, concurrency=4, user_burst=10)
    guild, _, _, authorizer = setup(cached=False, budget=budget)
    authorizer._clock = clock
    for user in ("2001", "2002", "2003"):
        guild.fetchable[int(user)] = FakeMember()
    sessions = [
        dataclasses.replace(member_session(), user=dataclasses.replace(member_session().user, id=u))
        for u in ("2001", "2002", "2003")
    ]

    authorize(authorizer, session=sessions[0])
    authorize(authorizer, session=sessions[1])
    with pytest.raises(MemberLookupUnavailable):
        authorize(authorizer, session=sessions[2])
    clock.advance(1)
    assert authorize(authorizer, session=sessions[2]).guild_id == GID


def test_the_budget_caps_concurrent_lookups_and_releases_them():
    budget = MemberBudget(FakeClock(), burst=10, per_second=1.0, concurrency=1, user_burst=10)
    assert budget.acquire("1", "u1") and not budget.acquire("1", "u2")
    budget.release("1")
    assert budget.acquire("1", "u1") and budget.acquire("2", "u1")


# --------------------------------------------------------------------------- T1, T7: caches separados e chaveados pela sessão


def test_a_member_decision_never_authorizes_the_manager_path():
    guild, pool, clock, authorizer = setup()
    authorize(authorizer)
    manager = GuildAuthorizer(pool, clock=clock)

    with pytest.raises(GuildAccessDenied):
        run(manager.authorize(member_session(), GID))  # sem o id em ``session.guilds`` o gerente nega


def test_a_cached_entry_of_another_user_is_never_served():
    guild, _, _, authorizer = setup()
    authorize(authorizer)
    other = dataclasses.replace(member_session(), user=dataclasses.replace(member_session().user, id="2002"))

    with pytest.raises(MemberAccessDenied):
        authorize(authorizer, session=other)  # 2002 não é membro: nada vem da entrada de 1001


def test_a_cache_hit_for_an_id_no_longer_in_the_session_lists_is_denied():
    _, _, _, authorizer = setup()
    authorize(authorizer)
    with pytest.raises(MemberAccessDenied):
        authorize(authorizer, session=member_session("777"))
    with pytest.raises(MemberAccessDenied):
        authorize(authorizer, session=dataclasses.replace(member_session(), member_guild_ids=frozenset()))


# --------------------------------------------------------------------------- T10, CC3: apagamento e banimento


def test_purge_drops_the_users_entries_only():
    guild, _, _, authorizer = setup()
    authorize(authorizer)
    other = dataclasses.replace(member_session(), user=dataclasses.replace(member_session().user, id="2002"))
    guild.members[2002] = FakeMember()
    authorize(authorizer, session=other)

    authorizer.purge_user(USER_ID)

    assert (USER_ID, GID) not in authorizer._cache and ("2002", GID) in authorizer._cache


def test_an_erasure_during_a_lookup_in_progress_leaves_nothing_in_the_cache():
    """CC3: a consulta em andamento termina depois do apagamento e não recria a entrada (usuário, servidor)."""
    guild, _, _, authorizer = setup(cached=False)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_fetch(user_id):
        started.set()
        await release.wait()
        return FakeMember()

    guild.fetch_member = slow_fetch

    async def scenario():
        lookup = asyncio.create_task(authorizer.authorize(member_session(), GID, fresh=False))
        await started.wait()
        authorizer.purge_user(USER_ID)  # o apagamento (ou o banimento) chega no meio
        release.set()
        await lookup

    run(scenario())

    assert authorizer._cache == {}


# --------------------------------------------------------------------------- revisão do PR 264


def test_cm3_the_cache_is_lru_a_hit_moves_the_entry_to_the_end(monkeypatch):
    from empiremuse.panel import member_authz

    monkeypatch.setattr(member_authz, "MAX_CACHE_ENTRIES", 2)
    clock = FakeClock()
    authorizer = MemberAuthorizer(FakePool([]), clock=clock)
    access = member_authz.MemberAccess(GID, "S", None)
    for user in ("a", "b"):
        authorizer._remember((user, GID), access, 0)

    authorizer._cached(("a", GID))  # "a" foi usada agora
    authorizer._remember(("c", GID), access, 0)

    assert list(authorizer._cache) == [("a", GID), ("c", GID)]  # saiu "b", a menos usada


def test_cs2_no_generation_is_kept_for_a_user_without_a_lookup_in_progress():
    _, _, _, authorizer = setup()
    authorize(authorizer)
    authorizer.purge_user(USER_ID)
    authorizer.purge_user("9999")

    assert authorizer._generation == {} and authorizer._inflight == {}


def test_sf1_one_user_draining_their_own_bucket_does_not_take_the_others_lookups():
    clock = FakeClock()
    budget = MemberBudget(clock, burst=10, per_second=1.0, user_burst=2, user_per_second=0.5)
    guild, _, _, authorizer = setup(cached=False, budget=budget)
    authorizer._clock = clock
    guild.fetchable[2002] = FakeMember()
    other = dataclasses.replace(member_session(), user=dataclasses.replace(member_session().user, id="2002"))

    authorize(authorizer, fresh=True)
    authorize(authorizer, fresh=True)
    with pytest.raises(MemberLookupUnavailable):
        authorize(authorizer, fresh=True)  # o balde de 1001 neste bot acabou
    assert authorize(authorizer, session=other, fresh=True).guild_id == GID  # o de 2002 e o global seguem


def test_sf1_a_read_in_a_chunked_guild_trusts_the_gateway_and_spends_no_rest():
    guild, _, _, authorizer = setup(cached=False)
    guild.chunked = True  # o bot liga o intent de membros: servidor carregado = o cache do gateway é completo

    with pytest.raises(MemberAccessDenied):
        authorize(authorizer)  # fora do cache do gateway = não está

    assert guild.calls["fetch_member"] == 0
    assert authorize(authorizer, fresh=True).guild_id == GID  # a escrita segue perguntando à API
