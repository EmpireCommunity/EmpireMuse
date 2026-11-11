"""J3 · telas do Discord das favoritas de rádio: modo rádio do ``/fav_manager`` (só Remover e Limpar, sempre efêmero)
e a gerência das favoritas do servidor (``/radio favoritas``: gate de admin, Limpar com confirmação).

Backend REAL (``RadioHub`` sobre banco em memória); só o Discord, o diretório e a árvore de local são dublês.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import disnake
import pytest

from empiremuse.utils.erasure import ErasureTombstones

from empiremuse.i18n import get_i18n
from empiremuse.rules.radio_stations import Station, check_station
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.interactions import FavMenuView, ViewMode
from empiremuse.utils.music.interactions import fav_menu_server
from empiremuse.utils.music.interactions.fav_menu_radio import fit_lines
from empiremuse.utils.music.interactions.fav_menu_server import ServerRadioFavoritesView, open_server_favorites
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.favorites import FavoriteForbidden
from empiremuse.utils.radio.premium import RadioUnavailable
from tests.persistence.test_radio_favoritas import station, uuid
from tests.persistence.test_radio_store import FakeDb
from tests.support.modais import ContextoDePrefixo

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

GUILD, MEMBER, OTHER_MEMBER, ADMIN = 500, 7, 8, 9
HOSTILE = "@everyone https://evil.example [x](https://evil.example) Rádio"
RADIO_MODE_VALUE = f"fav_view_mode_{ViewMode.radio_favorites}"


class World:
    """Pool com permissões por membro, hub real e a lista de quem pode gerenciar o servidor."""

    def __init__(self):
        self.db = FakeDb()
        self.managers = {ADMIN}
        self.tr = get_i18n().for_locale("pt-BR")
        guild = SimpleNamespace(
            owner_id=1,
            get_member=lambda uid: SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_guild=uid in self.managers, administrator=False)
            ),
        )
        bot = SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD else None)
        self.pool = SimpleNamespace(
            database=self.db, get_all_bots=lambda: [bot], i18n=get_i18n(), erasure_tombstones=ErasureTombstones()
        )
        self.hub = RadioHub(self.pool)
        self.pool.radio = self.hub
        lookup = AsyncMock(side_effect=lambda v: station(int(v[-12:]), HOSTILE))
        self.hub.directory = SimpleNamespace(by_uuid=lookup, by_uuid_strict=lookup)
        self.hub.locations = SimpleNamespace(
            node=lambda n: n if str(n).startswith("node:") else None,
            path=lambda n: [f"raiz-{n}", n],
            label=lambda node, locale: str(node).upper(),
        )
        self.bot = SimpleNamespace(
            pool=self.pool,
            loop=SimpleNamespace(create_task=self._drop_task),
            get_global_command_named=lambda *a, **k: SimpleNamespace(id=1),
        )

    @staticmethod
    def _drop_task(coro):
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    async def seed(self, user=MEMBER, stations=(1, 2), filters=("node:a",)):
        for n in stations:
            await self.hub.user_favorites.toggle_station(user, uuid(n))
        for node in filters:
            await self.hub.user_favorites.toggle_filter(user, node)


class FakeInter:
    def __init__(self, user_id, *, ephemeral=True, values=(), guild_id=GUILD):
        self.author = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.guild, self.guild_id = SimpleNamespace(preferred_locale="pt-BR"), guild_id
        self.values = list(values)
        self.message = SimpleNamespace(flags=SimpleNamespace(ephemeral=ephemeral))
        self.calls, self.sent = [], []
        self.response = SimpleNamespace(defer=self._defer, edit_message=self._edit_message)
        self.followup = SimpleNamespace(send=self._followup_send)

    async def _defer(self, **kwargs):
        self.calls.append(("defer", kwargs))

    async def _edit_message(self, **kwargs):
        self.calls.append(("edit_message", kwargs))

    async def edit_original_message(self, **kwargs):
        self.calls.append(("edit_original_message", kwargs))

    async def send(self, content=None, **kwargs):
        self.calls.append(("send", {"content": content, **kwargs}))
        self.sent.append({"content": content, **kwargs})

    async def _followup_send(self, content=None, **kwargs):
        self.calls.append(("followup", {"content": content, **kwargs}))
        self.sent.append({"content": content, **kwargs})

    def calls_named(self, name):
        return [kwargs for kind, kwargs in self.calls if kind == name]


@pytest.fixture
def world():
    return World()


def menu(world, ctx=None, **kwargs):
    ctx = ctx or FakeInter(MEMBER)
    return FavMenuView(bot=world.bot, ctx=ctx, data={"fav_links": {}, "integration_links": {}}, tr=world.tr, **kwargs)


def labels(view):
    return [c.label for c in view.children if isinstance(c, disnake.ui.Button)]


async def open_radio(world, user=MEMBER):
    """O menu já numa mensagem efêmera, no modo rádio, aberto pelo select de modos."""
    view = menu(world, FakeInter(user))
    inter = FakeInter(user, ephemeral=True, values=[RADIO_MODE_VALUE])
    await view.mode_callback(inter)
    return view, inter


async def test_radio_mode_in_an_ephemeral_message_lists_and_offers_only_remove_and_clear(world):
    await world.seed()
    view, inter = await open_radio(world)

    (content,) = inter.calls_named("edit_original_message")
    text = content["content"]
    assert content["allowed_mentions"].everyone is False
    assert text.count("📻") >= 3 and "📌" in text  # o título, as duas estações e o filtro
    assert "@everyone" not in text and "https://" not in text  # nome externo pelo sanitizador único
    tr = world.tr
    assert labels(view) == [
        tr("radio.favorites.remove_station.label"),
        tr("radio.favorites.clear_stations.label"),
        tr("radio.favorites.remove_filter.label"),
        tr("radio.favorites.clear_filters.label"),
        tr("radio.favorites.refs.label"),
        tr("fav.close.label"),
    ]
    assert view.mode == ViewMode.radio_favorites
    assert all(getattr(c, "custom_id", "") != "favmanager_play_button" for c in view.children)  # sem Tocar (J2a)


async def test_radio_mode_in_a_public_message_opens_a_new_ephemeral_one_and_leaves_the_public_one_alone(world):
    await world.seed()
    public_ctx = FakeInter(MEMBER, ephemeral=False)
    public_view = menu(world, public_ctx)
    inter = FakeInter(MEMBER, ephemeral=False, values=[RADIO_MODE_VALUE])

    await public_view.mode_callback(inter)

    assert inter.calls_named("defer") == [{"with_message": True, "ephemeral": True}]
    assert inter.calls_named("edit_message") == []  # a mensagem pública não é editada
    assert public_view.mode == ViewMode.fav_manager and public_ctx.calls == []
    (shown,) = inter.calls_named("edit_original_message")  # só a efêmera recebe a lista
    assert "📻" in shown["content"] and isinstance(shown["view"], FavMenuView)
    assert shown["view"].radio_private and shown["view"].mode == ViewMode.radio_favorites


async def test_the_list_is_never_built_without_the_private_gate(world):
    await world.seed()
    view = menu(world, mode=ViewMode.radio_favorites)  # radio_private=False: poderia estar numa mensagem pública
    await view.load_radio()

    with pytest.raises(GenericError) as raised:
        view.build_txt()

    assert "evil" not in str(raised.value) and "NODE" not in str(raised.value)  # nenhum dado da lista


async def test_prefix_command_has_no_radio_mode_because_it_cannot_be_ephemeral(world):
    ctx = ContextoDePrefixo()
    ctx.guild, ctx.guild_id, ctx.author = SimpleNamespace(preferred_locale="pt-BR"), GUILD, SimpleNamespace(id=MEMBER)
    view = menu(world, ctx)
    inter = FakeInter(MEMBER, values=[RADIO_MODE_VALUE])

    await view.mode_callback(inter)

    assert not view.radio_available() and inter.sent and view.mode == ViewMode.fav_manager
    view.update_components()
    offered = [o.value for c in view.children if isinstance(c, disnake.ui.Select) for o in c.options]
    assert RADIO_MODE_VALUE not in offered


async def test_remove_and_clear_act_on_the_clicker_and_report_nothing_changed_when_repeated(world):
    await world.seed()
    await world.seed(user=OTHER_MEMBER, stations=(3,), filters=("node:z",))
    view, _ = await open_radio(world)
    view.current_station = view.radio_stations[0].uuid
    favorites = world.hub.user_favorites

    await view.radio_remove_station_callback(FakeInter(MEMBER))
    assert [f.uuid for f in await favorites.stations(MEMBER)] == [uuid(2)]

    await view.radio_clear_stations_callback(FakeInter(MEMBER))
    await view.radio_clear_stations_callback(FakeInter(MEMBER))  # idempotente: não levanta
    assert view.log == world.tr("radio.favorites.nothing_changed")
    await view.radio_clear_filters_callback(FakeInter(MEMBER))
    assert await favorites.stations(MEMBER) == [] and await favorites.filters(MEMBER) == []
    assert len(await favorites.stations(OTHER_MEMBER)) == 1  # o documento de outro membro nunca é tocado
    assert await favorites.filters(OTHER_MEMBER) == ["node:z"]


async def test_another_member_cannot_drive_the_menu(world):
    await world.seed()
    view, _ = await open_radio(world)

    assert not await view.interaction_check(FakeInter(OTHER_MEMBER))  # a view do menu devolve None ao recusar

    assert len(await world.hub.user_favorites.stations(MEMBER)) == 2


async def test_database_down_shows_retry_and_never_the_list_then_recovers(world):
    await world.seed()
    world.db.fail = True
    view, inter = await open_radio(world)
    (content,) = inter.calls_named("edit_original_message")

    assert view.radio_unavailable and world.tr("radio.owner.unavailable") in content["content"]
    assert "📻 " not in content["content"].split("\n", 1)[1]  # nenhuma linha de estação
    assert labels(view) == [world.tr("radio.favorites.retry.label"), world.tr("fav.close.label")]

    world.db.fail = False
    await view.radio_retry_callback(FakeInter(MEMBER))
    assert not view.radio_unavailable and len(view.radio_stations) == 2


async def test_write_failure_is_reported_as_retry_and_changes_nothing(world):
    await world.seed()
    view, _ = await open_radio(world)
    world.db.fail = True

    await view.radio_clear_stations_callback(FakeInter(MEMBER))

    assert view.log == world.tr("radio.owner.unavailable") or view.radio_unavailable
    world.db.fail = False
    assert len(await world.hub.user_favorites.stations(MEMBER)) == 2


async def test_filters_are_hidden_while_the_location_tree_is_off(world):
    await world.seed()
    world.hub.locations = None
    view, _ = await open_radio(world)

    assert view.radio_filters == [] and len(view.radio_stations) == 2


async def test_gate_is_checked_again_on_click(world):
    await world.seed()
    view, _ = await open_radio(world)
    view.radio_private = False  # estado inconsistente: nada é removido nem redesenhado
    click = FakeInter(MEMBER)

    await view.radio_clear_stations_callback(click)

    assert click.sent and len(await world.hub.user_favorites.stations(MEMBER)) == 2


# ---------------------------------------------------------------------------------------------- servidor


async def open_server(world, user=ADMIN):
    inter = FakeInter(user)
    await open_server_favorites(inter, world.tr, world.pool)
    return inter


async def test_server_favorites_refuse_a_member_without_manage_guild_and_show_nothing(world):
    await world.hub.favorites.add(GUILD, ADMIN, station(1, HOSTILE))

    inter = await open_server(world, user=MEMBER)

    (only,) = inter.sent
    assert only["content"] == world.tr("radio.refused.no_manage_guild") and only["ephemeral"] is True
    assert "view" not in only


async def test_server_favorites_list_only_the_server_list_in_an_ephemeral_message(world):
    await world.hub.favorites.add(GUILD, ADMIN, station(1, HOSTILE))
    await world.seed(user=ADMIN, stations=(5,), filters=("node:secret",))

    inter = await open_server(world, user=ADMIN)

    (sent,) = inter.sent
    assert sent["ephemeral"] is True and sent["allowed_mentions"].everyone is False
    assert "⭐" in sent["content"] and "@everyone" not in sent["content"] and "https://" not in sent["content"]
    assert "NODE:SECRET" not in sent["content"] and uuid(5) not in sent["content"]  # nada pessoal do admin


async def test_server_remove_uses_the_clicker_as_actor_and_rechecks_the_gate_at_click(world):
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    await world.hub.favorites.add(GUILD, ADMIN, station(2))
    view = (await open_server(world)).sent[0]["view"]
    view.current = uuid(1)

    world.managers.discard(ADMIN)  # perdeu a permissão depois de abrir a tela
    await view.remove_callback(FakeInter(ADMIN))
    assert len(await world.hub.favorites.list(GUILD)) == 2  # recusado no clique

    world.managers.add(ADMIN)
    await view.remove_callback(FakeInter(ADMIN))
    assert [f.uuid for f in await world.hub.favorites.list(GUILD)] == [uuid(2)]


async def test_server_clear_needs_a_confirmation_and_cancel_keeps_the_list(world):
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    await world.hub.favorites.add(GUILD, ADMIN, station(2))
    view = (await open_server(world)).sent[0]["view"]

    await view.ask_clear_callback(FakeInter(ADMIN))
    assert view.confirming and len(await world.hub.favorites.list(GUILD)) == 2  # pedir não apaga
    assert labels(view) == [
        world.tr("radio.favorites.server.clear_confirm_button"),
        world.tr("radio.favorites.server.cancel.label"),
    ]

    await view.cancel_clear_callback(FakeInter(ADMIN))
    assert not view.confirming and len(await world.hub.favorites.list(GUILD)) == 2

    await view.ask_clear_callback(FakeInter(ADMIN))
    await view.confirm_clear_callback(FakeInter(ADMIN))
    assert await world.hub.favorites.list(GUILD) == [] and view.stations == []


async def test_server_clear_confirmation_by_someone_who_lost_the_permission_changes_nothing(world):
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    view = (await open_server(world)).sent[0]["view"]
    await view.ask_clear_callback(FakeInter(ADMIN))

    world.managers.discard(ADMIN)
    click = FakeInter(ADMIN)
    await view.confirm_clear_callback(click)

    assert len(await world.hub.favorites.list(GUILD)) == 1
    assert click.sent[0]["content"] == world.tr("radio.refused.no_manage_guild")


async def test_server_store_refusal_is_shown_as_forbidden(world):
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    view = (await open_server(world)).sent[0]["view"]
    view.current = uuid(1)
    refusing = SimpleNamespace(remove=AsyncMock(side_effect=FavoriteForbidden), list=AsyncMock(return_value=[]))
    view.pool = SimpleNamespace(radio=SimpleNamespace(favorites=refusing), get_all_bots=world.pool.get_all_bots)

    await view.remove_callback(FakeInter(ADMIN))

    assert view.log == world.tr("radio.refused.no_manage_guild")


async def test_server_database_down_is_reported_and_the_list_is_kept(world):
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    view = (await open_server(world)).sent[0]["view"]
    view.current = uuid(1)
    world.db.fail = True

    await view.remove_callback(FakeInter(ADMIN))

    assert view.log == world.tr("radio.owner.unavailable")
    world.db.fail = False
    assert len(await world.hub.favorites.list(GUILD)) == 1


async def test_server_view_only_answers_to_its_author(world):
    view = (await open_server(world)).sent[0]["view"]

    assert isinstance(view, ServerRadioFavoritesView)
    assert await view.interaction_check(FakeInter(ADMIN)) is True
    assert await view.interaction_check(FakeInter(OTHER_MEMBER)) is False


def production_station(n, name):
    """A estação como o diretório a entrega: pelo ``check_station``, que já escapa o markdown do nome."""
    raw = {"stationuuid": uuid(n), "name": name, "url_resolved": "http://stream.example:8000/live", "codec": "MP3"}
    built = check_station(raw)
    assert isinstance(built, Station), built
    return built


async def test_stored_names_are_shown_with_a_single_backslash_per_character(world):
    """O nome já sai escapado do ``check_station`` e é guardado assim; as telas o mostram com ``markdown=False``."""
    world.hub.directory.by_uuid_strict.side_effect = lambda v: production_station(int(v[-12:]), "Rock_FM **x**")
    await world.seed(stations=(1,), filters=())
    await world.hub.favorites.add(GUILD, ADMIN, production_station(1, "Rock_FM **x**"))

    _, inter = await open_radio(world)
    (content,) = inter.calls_named("edit_original_message")
    server = (await open_server(world)).sent[0]["content"]

    for text in (content["content"], server):
        assert "Rock\\_FM \\*\\*x\\*\\*" in text  # uma barra por caractere
        assert "\\\\" not in text  # nunca duas


async def test_headings_and_confirmation_show_the_translated_text_never_the_raw_key(world):
    await world.seed()
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    _, inter = await open_radio(world)
    (content,) = inter.calls_named("edit_original_message")
    server_view = (await open_server(world)).sent[0]["view"]
    server_view.confirming = True
    texts = [content["content"], server_view.build_txt()]

    assert "(2/24)" in texts[0] and "(1/10)" in texts[0] and "(1/24)" in texts[1]
    assert "1 favoritas do servidor?" in texts[1]
    assert all("radio.favorites." not in text for text in texts)


async def test_a_long_station_list_is_never_cut_without_saying_how_many_are_hidden(world):
    """O cabeçalho conta 24; a tela mostra N linhas e avisa ``+M``; N + M é sempre o total (nada de corte calado)."""
    await world.seed(stations=range(1, 25), filters=())
    _, inter = await open_radio(world)
    (content,) = inter.calls_named("edit_original_message")
    text = content["content"]

    shown = text.count("📻 ") - 1  # o título do modo também leva 📻
    hidden = int(text.split("> … +")[1].split("\n")[0])
    assert "(24/24)" in text and hidden > 0 and shown + hidden == 24
    assert len(text) <= 2000


async def test_a_short_list_has_no_hidden_marker():
    lines = [f"> ` {n:02} ` 📻 Rádio {n}" for n in range(1, 4)]
    assert "…" not in fit_lines(lines, 800)


async def test_the_server_gate_answers_the_click_before_the_slow_member_lookup(world, monkeypatch):
    """O gate pode levar até 5 s por bot e o Discord dá 3 s: o clique é adiado antes dele, recusa e confirmação vão por follow-up."""
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    view = (await open_server(world)).sent[0]["view"]
    view.current = uuid(1)
    order = []
    real = fav_menu_server.can_manage_guild

    async def slow_gate(pool, guild_id, user_id):
        order.append(("gate", [kind for kind, _ in click.calls]))
        return await real(pool, guild_id, user_id)

    monkeypatch.setattr(fav_menu_server, "can_manage_guild", slow_gate)
    for callback in (view.ask_clear_callback, view.remove_callback):
        order.clear()
        click = FakeInter(ADMIN)
        await callback(click)
        assert order and order[0] == ("gate", ["defer"])  # já tinha respondido (adiado) quando o gate rodou

    world.managers.discard(ADMIN)
    for callback in (view.ask_clear_callback, view.remove_callback):
        click = FakeInter(ADMIN)
        await callback(click)
        assert click.calls_named("followup")[0]["ephemeral"] is True  # a recusa vai por follow-up, nunca por inter.send
        assert click.calls_named("followup")[0]["content"] == world.tr("radio.refused.no_manage_guild")
        assert not click.calls_named("send")


# ---------------------------------------------------------------------- J2a: custom e gênero nas duas listas

ROCK = {"name": "Queen Rock", "kind": "artists", "source": {"artists": ["Queen"]}}


def tr_label(world: World, key: str) -> str:
    return world.tr(key)


async def make_custom(world: World, doc: dict[str, Any] = ROCK) -> str:
    return (await world.hub.stations.create(GUILD, doc)).id


async def test_server_menu_lists_custom_and_genre_and_removes_each_through_the_gate(world: World) -> None:
    custom = await make_custom(world)
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    await world.hub.favorites.add_ref(GUILD, ADMIN, custom, exists=AsyncMock(return_value=True))
    await world.hub.favorites.add_ref(GUILD, ADMIN, "g:rock", exists=AsyncMock(return_value=True))

    view = (await open_server(world)).sent[0]["view"]
    text = view.build_txt()
    assert "(3/" in text and "🎼" in text and "🎛️" in text and world.tr("radio.genre.rock") in text
    assert "Queen Rock" in text
    select = next(c for c in view.children if isinstance(c, disnake.ui.Select))
    assert [o.value for o in select.options] == [uuid(1), custom, "g:rock"]

    world.managers.discard(ADMIN)
    view.current = custom
    await view.remove_callback(FakeInter(ADMIN))  # recusado no clique: nada muda
    assert (await world.hub.favorites.list_all(GUILD))[1] == [custom, "g:rock"]

    world.managers.add(ADMIN)
    await view.remove_callback(FakeInter(ADMIN))
    assert (await world.hub.favorites.list_all(GUILD))[1] == ["g:rock"]
    view.current = "g:rock"
    await view.remove_callback(FakeInter(ADMIN))
    stations, refs = await world.hub.favorites.list_all(GUILD)
    assert [f.uuid for f in stations] == [uuid(1)] and refs == []


async def test_server_clear_removes_the_refs_too_and_a_deleted_custom_stays_listed_as_removed(world: World) -> None:
    custom = await make_custom(world)
    await world.hub.favorites.add_ref(GUILD, ADMIN, custom, exists=AsyncMock(return_value=True))
    await world.hub.stations.delete(
        GUILD, custom, expected_rev=(await world.hub.stations.get(GUILD, custom)).rev
    )  # a estação sumiu: segue listada e removível
    view = (await open_server(world)).sent[0]["view"]
    assert "removida" in view.build_txt()

    await view.ask_clear_callback(FakeInter(ADMIN))
    await view.confirm_clear_callback(FakeInter(ADMIN))

    assert await world.hub.favorites.list_all(GUILD) == ([], [])


async def test_member_menu_lists_own_custom_and_genre_removes_and_clears_without_touching_others(world: World) -> None:
    custom = await make_custom(world)
    custom_favorites = world.hub.custom_favorites
    for ref in (custom, "g:rock"):
        await custom_favorites.toggle(GUILD, MEMBER, ref)
    await custom_favorites.toggle(GUILD, OTHER_MEMBER, custom)
    await world.seed()

    view, inter = await open_radio(world)
    assert (
        "Queen Rock" not in inter.calls_named("edit_original_message")[0]["content"]
    )  # a página principal não leva as refs
    await view.radio_toggle_refs_callback(FakeInter(MEMBER))  # a página própria cabe nas 5 linhas do Discord
    assert view.radio_refs_page and tr_label(world, "radio.favorites.remove_ref.label") in labels(view)
    text = view.radio_manager_txt()
    assert "(2/24)" in text and world.tr("radio.genre.rock") in text and "Queen Rock" in text

    view.current_ref = custom
    await view.radio_remove_ref_callback(FakeInter(MEMBER))
    assert await custom_favorites.list(GUILD, MEMBER) == ["g:rock"]
    await view.radio_clear_refs_callback(FakeInter(MEMBER))
    assert await custom_favorites.list(GUILD, MEMBER) == []
    assert await custom_favorites.list(GUILD, OTHER_MEMBER) == [custom]  # o documento de outro membro nunca é tocado
    await view.radio_clear_refs_callback(FakeInter(MEMBER))
    assert view.log == world.tr("radio.favorites.nothing_changed")


async def test_member_ref_writes_respect_the_erasure_tombstone(world: World) -> None:
    await make_custom(world)
    view, _ = await open_radio(world)
    await world.hub.custom_favorites.toggle(GUILD, MEMBER, "g:rock")
    await view.load_radio()
    real_find = world.hub.custom_favorites._find

    async def erased_meanwhile(key: str) -> dict[str, Any] | None:
        doc = await real_find(key)
        world.pool.erasure_tombstones.mark(MEMBER)  # apagar dados no meio da operação
        return doc

    world.hub.custom_favorites._find = erased_meanwhile
    view.current_ref = "g:rock"
    await view.radio_remove_ref_callback(FakeInter(MEMBER))
    assert view.log == world.tr("radio.owner.unavailable")


def all_texts(view: FavMenuView) -> tuple[str, str]:
    """O texto da página principal e o da página das ``custom``/``genre``."""
    view.radio_refs_page = False
    main = view.radio_manager_txt()
    view.radio_refs_page = True
    refs = view.radio_manager_txt()
    view.radio_refs_page = False
    return main, refs


async def full_world(world: World) -> None:
    """As três listas cheias: 24 estações, 10 filtros e 24 entradas ``custom``/``genre`` do membro."""
    from empiremuse.rules.radio_genres import shipped_catalog

    await world.seed(stations=range(1, 25), filters=[f"node:{n}" for n in range(10)])
    custom = await make_custom(world)
    for ref in [custom, *(f"g:{g.id}" for g in shipped_catalog())][:24]:
        await world.hub.custom_favorites.toggle(GUILD, MEMBER, ref)


async def test_every_member_page_fits_the_2000_character_limit_with_the_three_lists_full(world: World) -> None:
    await full_world(world)
    view, _ = await open_radio(world)
    view.log = "x" * 100

    for text in all_texts(view):
        assert len(text) + len(view.log) <= 2000
        assert "\\n" not in text  # nenhum "\n" literal (barra e n) vindo dos textos do idioma
    assert all("\\n" not in world.tr(key) for key in ("radio.favorites.refs.heading", "radio.favorites.refs.empty"))


async def test_a_genre_gone_from_the_catalog_shows_as_removed_and_stays_removable(world: World) -> None:
    await world.hub.custom_favorites.toggle(GUILD, MEMBER, "g:ghost_genre")
    view, _ = await open_radio(world)
    await view.radio_toggle_refs_callback(FakeInter(MEMBER))
    assert "removida" in view.radio_manager_txt()

    view.current_ref = "g:ghost_genre"
    await view.radio_remove_ref_callback(FakeInter(MEMBER))
    assert await world.hub.custom_favorites.list(GUILD, MEMBER) == []
    assert view.log == world.tr("radio.favorites.log.ref_gone")  # sem "(removida) removida"


async def test_refs_page_escapes_markdown_once_in_the_member_and_the_server_lists(world: World) -> None:
    custom = await make_custom(world, {**ROCK, "name": "Rock_FM [x]"})
    await world.hub.custom_favorites.toggle(GUILD, MEMBER, custom)
    await world.hub.favorites.add_ref(GUILD, ADMIN, custom, exists=AsyncMock(return_value=True))
    view, _ = await open_radio(world)
    await view.radio_toggle_refs_callback(FakeInter(MEMBER))
    server = (await open_server(world)).sent[0]["content"]

    for text in (view.radio_manager_txt(), server):
        assert "Rock\\_FM" in text and "\\\\" not in text  # uma barra por caractere, nunca duas


async def test_member_ref_load_failure_shows_retry_on_the_member_page(world: World) -> None:
    view, _ = await open_radio(world)

    async def down(user: int, *args: Any) -> None:
        raise RadioUnavailable

    world.hub.custom_favorites.list = down
    await view.load_radio()
    assert not view.radio_unavailable and view.radio_refs_unavailable
    view.radio_refs_page = True
    assert world.tr("radio.favorites.refs.unavailable") in view.radio_manager_txt()


async def test_server_clear_keeps_the_member_custom_favorites(world: World) -> None:
    custom = await make_custom(world)
    await world.hub.custom_favorites.toggle(GUILD, MEMBER, custom)
    await world.hub.favorites.add_ref(GUILD, ADMIN, custom, exists=AsyncMock(return_value=True))
    view = (await open_server(world)).sent[0]["view"]

    await view.ask_clear_callback(FakeInter(ADMIN))
    await view.confirm_clear_callback(FakeInter(ADMIN))

    assert await world.hub.favorites.list_all(GUILD) == ([], [])
    assert await world.hub.custom_favorites.list(GUILD, MEMBER) == [custom]  # a lista pessoal é outra


async def test_member_clear_respects_the_erasure_tombstone(world: World) -> None:
    await world.hub.custom_favorites.toggle(GUILD, MEMBER, "g:rock")
    real_find = world.hub.custom_favorites._find

    async def erased_meanwhile(key: str) -> dict[str, Any] | None:
        doc = await real_find(key)
        world.pool.erasure_tombstones.mark(MEMBER)
        return doc

    world.hub.custom_favorites._find = erased_meanwhile
    with pytest.raises(RadioUnavailable):
        await world.hub.custom_favorites.clear(GUILD, MEMBER)
    world.hub.custom_favorites._find = real_find
    assert await world.hub.custom_favorites.list(GUILD, MEMBER) == ["g:rock"]  # nada foi gravado


async def test_server_reload_failure_keeps_the_log_of_the_change_that_happened(world: World) -> None:
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    view = (await open_server(world)).sent[0]["view"]
    view.current = uuid(1)

    async def down(pool: Any, tr: Any, guild_id: int) -> None:
        raise RadioUnavailable

    original = fav_menu_server.load_server_list
    fav_menu_server.load_server_list = down
    try:
        await view.remove_callback(FakeInter(ADMIN))
    finally:
        fav_menu_server.load_server_list = original
    assert (await world.hub.favorites.list_all(GUILD))[0] == []  # a remoção aconteceu
    assert "Rádio 1" in view.log  # e o aviso dela segue na tela
    assert world.tr("radio.owner.unavailable") in view.log


async def test_server_ref_label_failure_preserves_live_management_and_retry(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = await make_custom(world)
    await world.hub.favorites.add(GUILD, ADMIN, station(1))
    await world.hub.favorites.add_ref(GUILD, ADMIN, custom, exists=AsyncMock(return_value=True))
    original = world.hub.stations.get
    monkeypatch.setattr(world.hub.stations, "get", AsyncMock(side_effect=RadioUnavailable))
    opened = await open_server(world)
    view = opened.sent[0]["view"]
    assert view.refs_unavailable and [s.uuid for s in view.stations] == [uuid(1)]
    assert world.tr("radio.favorites.refs.unavailable") in view.build_txt()
    assert world.tr("radio.favorites.server.empty") not in view.build_txt()
    assert next(
        c for c in view.children if getattr(c, "label", "") == world.tr("radio.favorites.server.clear.label")
    ).disabled
    view.current = uuid(1)
    await view.remove_callback(FakeInter(ADMIN))
    assert await world.hub.favorites.list_all(GUILD) == ([], [custom])
    await view.confirm_clear_callback(FakeInter(ADMIN))  # nem callback antigo apaga refs invisíveis
    assert (await world.hub.favorites.list_all(GUILD))[1] == [custom]
    monkeypatch.setattr(world.hub.stations, "get", original)
    world.managers.discard(ADMIN)
    refused = FakeInter(ADMIN)
    await view.retry_callback(refused)
    assert refused.calls_named("followup")[0]["content"] == world.tr("radio.refused.no_manage_guild")
    assert view.refs_unavailable  # a releitura também exige a permissão atual
    world.managers.add(ADMIN)
    await view.retry_callback(FakeInter(ADMIN))
    assert not view.refs_unavailable and view.refs == [(custom, "Queen Rock")]


async def test_member_ref_label_failure_preserves_live_filters_and_retry(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = await make_custom(world)
    await world.hub.custom_favorites.toggle(GUILD, MEMBER, custom)
    await world.seed()
    original = world.hub.stations.get
    monkeypatch.setattr(world.hub.stations, "get", AsyncMock(side_effect=RadioUnavailable))
    view, _ = await open_radio(world)
    assert not view.radio_unavailable and view.radio_refs_unavailable
    assert len(view.radio_stations) == 2 and len(view.radio_filters) == 1
    view.current_station = uuid(1)
    await view.radio_remove_station_callback(FakeInter(MEMBER))
    assert [s.uuid for s in await world.hub.user_favorites.stations(MEMBER)] == [uuid(2)]
    await view.radio_toggle_refs_callback(FakeInter(MEMBER))
    assert world.tr("radio.favorites.refs.unavailable") in view.radio_manager_txt()
    assert world.tr("radio.favorites.retry.label") in labels(view)
    await view.radio_clear_refs_callback(FakeInter(MEMBER))
    assert await world.hub.custom_favorites.list(GUILD, MEMBER) == [custom]
    monkeypatch.setattr(world.hub.stations, "get", original)
    await view.radio_retry_callback(FakeInter(MEMBER))
    assert not view.radio_refs_unavailable and view.radio_refs == [(custom, "Queen Rock")]
