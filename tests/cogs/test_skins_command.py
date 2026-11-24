"""O ``/skins`` (2.3, S6): o que o Salvar grava, quem pode gravar e o que é recusado sem escrita.

Superfícies de risco (contrato S6 §5 e §10 item 6; ameaça SK-12): valor forjado (``../x``, id fora do registro, rádio de
outro servidor) recusado sem escrita; sem a permissão das rádios o par não aparece nem grava; Cancelar não grava; conflito
de CAS numa rádio não desfaz as outras; o servidor é sempre o da interação. Só o Discord, o banco e a store são dublês.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import disnake
import pytest

from empiremuse.cogs import music_settings
from empiremuse.cogs.music_settings import MusicSettings
from empiremuse.i18n import get_i18n
from empiremuse.utils.music.interactions import skins_view
from empiremuse.utils.music.interactions.skins_draft import (
    INVALID,
    Choices,
    Draft,
    RadioLook,
    RadioRow,
    SavePlan,
    plan_is_valid,
    plan_save,
    player_value,
    radio_skin_value,
    target_value,
)
from empiremuse.utils.others import CustomContext
from empiremuse.utils.radio.store import RadioConflict, RadioNotFound

pytestmark = pytest.mark.critical

GUILD = 4242
BOT_A, BOT_B, STRANGER = 11, 12, 99
TR = get_i18n().for_locale("pt-BR")
RADIOS = (RadioRow(BOT_A, "#a · A", None, 3, "#a · A"), RadioRow(BOT_B, "#b · B", "white", 7, "#b · B"))
CHOICES = Choices(
    normal=("muse_expanded_black", "muse_text", "default", "classic"),
    static=("muse_expanded_black", "default"),
    custom_normal=("Minha",),
    custom_static=(),
    radios=RADIOS,
)
INITIAL = Draft(
    global_mode=False,
    server=("default", "default"),
    glob=("default", "default"),
    disc_motion=True,
    radio_all="portrait_black",
    radio_own=((BOT_A, None), (BOT_B, "white")),
)


def _replace(draft: Draft, **changes) -> Draft:
    return Draft(**{**draft.__dict__, **changes})


# ============================================================================ plano (puro)


class TestPlan:
    def test_saving_without_changes_writes_nothing(self):
        assert plan_save(INITIAL, INITIAL, RADIOS, radio_allowed=True).empty

    def test_only_the_changed_fields_go_to_their_documents(self):
        draft = _replace(INITIAL, server=("muse_text", "default"), disc_motion=False, radio_all="text")
        draft = _replace(draft, radio_own=((BOT_A, "portrait_white"), (BOT_B, "white")))

        plan = plan_save(INITIAL, draft, RADIOS, radio_allowed=True)

        assert plan.server_fields == {"skin": "muse_text"}
        assert plan.global_fields == {"disc_motion": False, "radio_skin": "text"}
        assert plan.radio_looks == (RadioLook(BOT_A, "portrait_white", 3),)

    def test_global_mode_without_changing_the_skins_keeps_the_absence_of_a_global_choice(self):
        plan = plan_save(INITIAL, _replace(INITIAL, global_mode=True), RADIOS, radio_allowed=True)
        assert plan.global_fields == {"global_skin": True} and plan.server_fields == {}

    def test_without_the_radio_permission_forged_radio_changes_are_dropped(self):
        draft = _replace(INITIAL, radio_all="text", radio_own=((BOT_A, "text"), (BOT_B, None)))
        assert plan_save(INITIAL, draft, RADIOS, radio_allowed=False).empty

    def test_a_radio_that_was_not_listed_never_enters_the_plan(self):
        draft = _replace(INITIAL, radio_own=((BOT_A, None), (BOT_B, "white"), (STRANGER, "text")))
        assert plan_save(INITIAL, draft, RADIOS, radio_allowed=True).radio_looks == ()

    @pytest.mark.parametrize(
        "plan",
        [
            SavePlan({}, {"skin": "../x"}, ()),
            SavePlan({}, {"skin": "__init__"}, ()),
            SavePlan({}, {"static_skin": "classic"}, ()),  # Clássica que não existe no modo fixo
            SavePlan({"player_skin": "> custom_skin: Outra"}, {}, ()),
            SavePlan({"radio_skin": "black;x"}, {}, ()),
            SavePlan({"disc_motion": "false"}, {}, ()),
            SavePlan({"owner_ids": [1]}, {}, ()),
            SavePlan({}, {}, (RadioLook(STRANGER, "text", 0),)),
            SavePlan({}, {}, (RadioLook(BOT_A, "../portrait", 3),)),
        ],
    )
    def test_any_value_outside_the_reread_lists_refuses_the_whole_plan(self, plan):
        assert not plan_is_valid(plan, CHOICES)

    def test_valid_plan_passes(self):
        plan = SavePlan(
            {"player_skin": "> custom_skin: Minha", "radio_skin": "text", "global_skin": False},
            {"skin": "muse_text"},
            (RadioLook(BOT_A, None, 3),),
        )
        assert plan_is_valid(plan, CHOICES)


class TestSelectValues:
    @pytest.mark.parametrize("raw", ["../x", "custom:Outra", "muse_slim_black", None, 5])
    def test_player_value_outside_the_lists_is_refused(self, raw):
        assert player_value(raw, CHOICES.normal, CHOICES.custom_normal) is None

    def test_player_value_maps_a_custom_to_the_stored_form(self):
        assert player_value("custom:Minha", CHOICES.normal, CHOICES.custom_normal) == "> custom_skin: Minha"

    @pytest.mark.parametrize("raw", [f"radio:{STRANGER}", "radio:-1", "radio:1e3", "radio:", "tudo", None])
    def test_target_outside_the_servers_radios_is_refused(self, raw):
        assert target_value(raw, RADIOS) is None

    def test_follow_only_exists_for_a_specific_radio(self):
        assert radio_skin_value("follow", "all") is INVALID
        assert radio_skin_value("follow", f"radio:{BOT_A}") is None
        assert radio_skin_value("portrait_x", "all") is INVALID


class TestPreviewPath:
    @pytest.mark.parametrize("relative", ["../../etc/passwd", "skins/nao_existe.png", "brand/../../pyproject.toml", ""])
    def test_paths_outside_assets_or_missing_have_no_image(self, relative):
        assert skins_view.preview_path(relative) is None

    def test_an_existing_png_outside_assets_has_no_image(self, monkeypatch, tmp_path):
        assets = tmp_path / "assets"
        (assets / "skins").mkdir(parents=True)
        (assets / "skins" / "ok.png").write_bytes(b"png")
        (tmp_path / "fora.png").write_bytes(b"png")
        monkeypatch.setattr(skins_view, "ASSETS_DIR", assets)

        assert skins_view.preview_path("skins/ok.png") == (assets / "skins" / "ok.png").resolve()
        assert skins_view.preview_path("../fora.png") is None


# ============================================================================ gravação


class RadioStore:
    def __init__(self, conflicts=(), radios=()):
        self.conflicts = set(conflicts)
        self.radios = list(radios)
        self.calls = []

    async def set_look(self, guild_id, radio_id, look, *, expected_rev, updated_by):
        self.calls.append((guild_id, radio_id, look, expected_rev))
        if radio_id in self.conflicts:
            raise RadioConflict
        if not radio_id.endswith(f":{guild_id}"):
            raise RadioNotFound

    async def list_for_guild(self, guild_id):
        return [radio for radio in self.radios if radio.guild_id == guild_id]


def _radio(bot_id=BOT_A, *, guild_id=GUILD, look=None, rev=3, channel_id=5):
    return SimpleNamespace(bot_id=bot_id, guild_id=guild_id, channel_id=channel_id, look=look, rev=rev)


class Bot:
    """O bot, o pool e o banco de mentira: ``get_global_data`` devolve a mesma referência do cache, como o banco real."""

    def __init__(self, global_data, store=None, channels=None):
        self.global_data = {"custom_skins": {}, "custom_skins_static": {}, **global_data}  # o modelo padrão do banco
        self.global_writes = []
        self.server_writes = []
        self.settings = {"skin": "default", "static_skin": "default"}
        self.store = store or RadioStore()
        self.channels = channels or {}
        self.music = SimpleNamespace(players={})
        self.player_skins = dict.fromkeys(CHOICES.normal)
        self.player_static_skins = dict.fromkeys(CHOICES.static)
        self.config = {"IGNORE_SKINS": "", "IGNORE_STATIC_SKINS": ""}
        self.user = SimpleNamespace(
            id=1,
            display_name="Muse",
            display_avatar=SimpleNamespace(with_format=lambda fmt: SimpleNamespace(url="https://exemplo.invalid/b")),
        )
        self.pool = SimpleNamespace(
            default_skin="muse_text",
            default_static_skin="muse_text",
            get_all_bots=lambda: [self],
            i18n=get_i18n(),
            servers=SimpleNamespace(patch=self._patch, settings=self._settings),
            radio=SimpleNamespace(store=self.store, manager=SimpleNamespace(runner=lambda rid: None)),
        )

    async def _patch(self, gid, section, changes):
        self.server_writes.append((gid, section, dict(changes)))
        self.settings = {**self.settings, **changes}

    async def _settings(self, gid):
        return self.settings

    async def get_global_data(self, id_, *, db_name):
        return self.global_data

    async def update_global_data(self, id_, data, *, db_name):
        self.global_writes.append((id_, copy.deepcopy(data)))
        self.global_data = data

    def get_guild(self, guild_id):
        return SimpleNamespace(id=guild_id, me=SimpleNamespace(), get_channel=self.channels.get)

    def get_color(self, *args):
        return 0


class TestSaveSkins:
    @pytest.mark.asyncio
    async def test_a_cas_conflict_on_one_radio_keeps_the_others_saved(self):
        store = RadioStore(conflicts={f"{BOT_A}:{GUILD}"})
        bot = Bot({"global_skin": False, "custom_skins": {"X": {}}}, store=store)
        plan = SavePlan({"radio_skin": "text"}, {}, (RadioLook(BOT_A, "text", 3), RadioLook(BOT_B, None, 7)))

        failed = await skins_view.save_skins(bot, GUILD, plan, updated_by="900")

        assert failed == (BOT_A,)
        assert [call[1] for call in store.calls] == [f"{BOT_A}:{GUILD}", f"{BOT_B}:{GUILD}"]
        assert bot.global_writes == [(GUILD, {**bot.global_data, "radio_skin": "text"})]
        assert bot.global_data["custom_skins"] == {"X": {}}  # o resto do documento relido fica

    @pytest.mark.asyncio
    async def test_the_radio_id_is_always_built_with_the_interactions_guild(self):
        store = RadioStore()
        plan = SavePlan({}, {}, (RadioLook(BOT_B, "text", 7),))
        await skins_view.save_skins(Bot({}, store=store), GUILD, plan, updated_by="9")
        assert store.calls == [(GUILD, f"{BOT_B}:{GUILD}", "text", 7)]

    def test_the_radio_panels_of_this_guild_are_asked_to_redraw(self):
        redrawn = []
        ports = {f"{BOT_A}:{GUILD}": SimpleNamespace(refresh_panel=lambda: redrawn.append(BOT_A))}
        runner = lambda rid: SimpleNamespace(ports=ports[rid]) if rid in ports else None  # noqa: E731
        pool = SimpleNamespace(radio=SimpleNamespace(manager=SimpleNamespace(runner=runner)))

        skins_view.redraw_radio_panels(pool, GUILD, [BOT_A, BOT_B])

        assert redrawn == [BOT_A]


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("global_mode", [False, True])
async def test_a_classic_default_saved_in_skins_survives_a_reopen_and_the_new_player(global_mode):
    from empiremuse.rules.skins import saved_player_skins

    bot = Bot({"global_skin": global_mode})
    bot.settings = {"skin": "mini", "static_skin": "mini"}
    initial = skins_view.initial_draft(bot.pool, bot.settings, bot.global_data, CHOICES)
    scope = "glob" if global_mode else "server"
    draft = _replace(initial, **{scope: ("default", "default")})

    await skins_view.save_skins(bot, GUILD, plan_save(initial, draft, RADIOS, radio_allowed=False), updated_by="9")
    reopened = skins_view.initial_draft(bot.pool, bot.settings, bot.global_data, CHOICES)

    assert getattr(reopened, scope) == ("default", "default")  # a Clássica escolhida, não o padrão Texto
    assert saved_player_skins(bot.settings, bot.global_data) == ("default", "default")  # a leitura da criação do player


# ============================================================================ o comando de ponta a ponta (tela de mentira)


def _member(*, manage_guild=True):
    perms = SimpleNamespace(manage_guild=manage_guild, administrator=False)
    return SimpleNamespace(id=900, mention="<@900>", guild_permissions=perms)


async def _noop(*args, **kwargs):
    return None


class Opened:
    """O que o comando montou: a tela (``None`` se recusou antes) e as respostas."""

    def __init__(self):
        self.screen = None
        self.replies = []


async def run_skins(
    monkeypatch,
    bot,
    *,
    change,
    result="save",
    saver=None,
    radio_allowed=True,
    opener=None,
    prefix=False,
    reply_error=None,
) -> Opened:
    """Roda o ``MusicSettings.change_skin`` REAL; a tela devolve o rascunho que ``change(initial)`` monta. ``prefix`` = pelo
    comando de prefixo (um ``CustomContext``); ``opener`` = quem abre; ``saver`` = quem aperta Salvar."""
    opened = Opened()

    async def edit(*args, **kwargs):
        opened.replies.append(kwargs)
        if reply_error is not None and len(opened.replies) > 1:  # a primeira edição é a da tela; a seguinte, a resposta
            raise reply_error

    member = saver or _member()

    class Screen:
        def __init__(self, *, initial, choices, **kwargs):
            opened.screen = self
            self.initial, self.choices, self.kwargs = initial, choices, kwargs
            self.draft = change(initial)
            self.result, self.message = result, None
            self.interaction = SimpleNamespace(
                guild_id=GUILD,
                author=member,
                response=SimpleNamespace(defer=_noop, edit_message=edit),
                edit_original_message=edit,
            )

        def payload(self):
            return {}

        async def wait(self):
            return None

    async def pick_bot(interaction, return_new=False):
        return interaction, bot

    monkeypatch.setattr(music_settings, "SkinsView", Screen)
    monkeypatch.setattr(music_settings, "select_bot_pool", pick_bot)
    monkeypatch.setattr(music_settings, "update_inter", lambda *args, **kwargs: None)
    monkeypatch.setattr(music_settings, "can_configure_radios", lambda member: radio_allowed)
    cog = MusicSettings.__new__(MusicSettings)
    cog.bot = bot
    author = opener or _member()
    if prefix:
        interaction = CustomContext.__new__(CustomContext)
        interaction.message = SimpleNamespace(author=author, guild=bot.get_guild(GUILD))
        interaction.guild_id, interaction.store_message = GUILD, None
        interaction.response = SimpleNamespace(defer=_noop)
        interaction.send = edit
    else:
        interaction = SimpleNamespace(
            guild_id=GUILD,
            guild=bot.get_guild(GUILD),
            author=author,
            response=SimpleNamespace(defer=_noop),
            edit_original_message=edit,
        )
    await MusicSettings.change_skin.callback(cog, interaction)
    return opened


def _server_skin(draft: Draft, skin: str) -> Draft:
    return _replace(draft, server=(skin, draft.server[1]))


class TestCommand:
    @pytest.mark.regression
    @pytest.mark.asyncio
    async def test_turning_global_mode_off_persists_global_skin_false(self, monkeypatch):
        """Bug de antes do 2.3: desligar o modo global só gravava as skins do servidor; ``global_skin`` ficava ``True``."""
        bot = Bot({"global_skin": True, "player_skin": "muse_text", "custom_skins": {}, "custom_skins_static": {}})

        await run_skins(monkeypatch, bot, change=lambda d: _replace(d, global_mode=False))

        assert bot.global_writes and bot.global_writes[-1][1]["global_skin"] is False
        assert bot.server_writes == []

    @pytest.mark.asyncio
    async def test_cancel_writes_nothing(self, monkeypatch):
        bot = Bot({"global_skin": False})
        await run_skins(monkeypatch, bot, change=lambda d: _replace(d, disc_motion=False), result="cancel")
        assert bot.global_writes == [] and bot.server_writes == []

    @pytest.mark.asyncio
    async def test_member_who_lost_manage_guild_before_saving_writes_nothing(self, monkeypatch):
        bot = Bot({"global_skin": False})
        lost = _member(manage_guild=False)
        replies = (
            await run_skins(monkeypatch, bot, change=lambda d: _server_skin(d, "muse_expanded_black"), saver=lost)
        ).replies
        assert bot.global_writes == [] and bot.server_writes == []
        assert replies[-1]["embeds"][0].description == TR("skin.no_permission")

    @pytest.mark.asyncio
    async def test_forged_value_is_refused_without_writing(self, monkeypatch):
        bot = Bot({"global_skin": False})
        forged = lambda d: _replace(_server_skin(d, "../x"), disc_motion=False)  # noqa: E731
        replies = (await run_skins(monkeypatch, bot, change=forged)).replies
        assert bot.global_writes == [] and bot.server_writes == []
        assert replies[-1]["embeds"][0].description == TR("skin.invalid")

    @pytest.mark.asyncio
    async def test_without_the_radio_permission_the_radio_pair_is_not_written(self, monkeypatch):
        store = RadioStore()
        bot = Bot({"global_skin": False}, store=store)
        forged = lambda d: _replace(d, radio_all="text", radio_own=((BOT_A, "text"),))  # noqa: E731
        await run_skins(monkeypatch, bot, change=forged, radio_allowed=False)
        assert store.calls == [] and bot.global_writes == []

    @pytest.mark.asyncio
    async def test_server_skin_change_writes_only_the_server_settings(self, monkeypatch):
        bot = Bot({"global_skin": False})
        await run_skins(monkeypatch, bot, change=lambda d: _server_skin(d, "muse_expanded_black"))
        assert (
            bot.server_writes == [(GUILD, "settings", {"skin": "muse_expanded_black", "skin_chosen": True})]
            and bot.global_writes == []
        )

    @pytest.mark.asyncio
    async def test_a_member_without_manage_guild_does_not_get_the_screen(self, monkeypatch):
        opened = await run_skins(monkeypatch, Bot({}), change=lambda d: d, opener=_member(manage_guild=False))
        assert opened.screen is None
        assert opened.replies[-1]["embed"].description == TR("skin.no_permission")

    @pytest.mark.parametrize(("radio_allowed", "listed"), [(True, (BOT_A,)), (False, ())])
    @pytest.mark.asyncio
    async def test_the_radio_pair_only_shows_with_the_radio_permission(self, monkeypatch, radio_allowed, listed):
        bot = Bot({}, store=RadioStore(radios=[_radio()]))
        opened = await run_skins(monkeypatch, bot, change=lambda d: d, result="cancel", radio_allowed=radio_allowed)
        assert tuple(row.bot_id for row in opened.screen.choices.radios) == listed

    @pytest.mark.asyncio
    async def test_the_prefix_command_neither_lists_nor_writes_radios(self, monkeypatch):
        """Pelo prefixo a resposta é pública: o nome dos canais das rádios (de call privada também) não pode sair ali."""
        store = RadioStore(radios=[_radio()])
        bot = Bot({"global_skin": False}, store=store)
        forged = lambda d: _replace(d, radio_all="text", radio_own=((BOT_A, "text"),))  # noqa: E731

        opened = await run_skins(monkeypatch, bot, change=forged, prefix=True)

        assert opened.screen.choices.radios == ()
        assert opened.screen.kwargs["note"] == TR("skin.ui.radios_by_slash")
        assert store.calls == [] and bot.global_writes == []

    @pytest.mark.asyncio
    async def test_a_cas_conflict_names_the_radio_that_was_not_saved(self, monkeypatch):
        store = RadioStore(conflicts={f"{BOT_A}:{GUILD}"}, radios=[_radio()])
        bot = Bot({"global_skin": False}, store=store, channels={5: SimpleNamespace(name="**x**")})
        change = lambda d: _replace(d, radio_own=((BOT_A, "text"),))  # noqa: E731

        opened = await run_skins(monkeypatch, bot, change=change)

        text = opened.replies[-1]["embeds"][0].description
        assert TR("skin.radio.changed", radio=f"#\\*\\*x\\*\\* · {BOT_A}") in text and TR("skin.unchanged") in text
        assert "#**x**" not in text  # o nome do canal sai escapado no embed

    @pytest.mark.parametrize(
        ("change", "reloaded"),
        [
            (
                lambda d: _replace(d, radio_all="portrait_black"),
                False,
            ),  # só a skin das rádios (a padrão é a Texto): os players ficam como estão
            (lambda d: _replace(d, disc_motion=False), True),
        ],
    )
    @pytest.mark.asyncio
    async def test_only_the_radio_skin_changing_does_not_reload_the_players(self, monkeypatch, change, reloaded):
        calls = []

        async def reload(self, *args, **kwargs):
            calls.append(args)

        monkeypatch.setattr(MusicSettings, "_reload_player_skins", reload)
        bot = Bot({"global_skin": False}, store=RadioStore(radios=[_radio()]))

        await run_skins(monkeypatch, bot, change=change)

        assert bot.global_writes and bool(calls) is reloaded

    @pytest.mark.asyncio
    async def test_a_database_failure_on_save_still_answers(self, monkeypatch):
        bot = Bot({"global_skin": False})

        async def down(*args, **kwargs):
            raise ConnectionError("banco fora")

        bot.pool.servers.patch = down
        opened = await run_skins(monkeypatch, bot, change=lambda d: _server_skin(d, "muse_expanded_black"))
        assert opened.replies[-1]["embeds"][0].description == TR("skin.save_failed")

    @pytest.mark.asyncio
    @pytest.mark.regression
    @pytest.mark.parametrize("global_mode", [False, True])
    async def test_a_saved_skin_in_ignore_skins_opens_the_real_screen_and_saves_unchanged_writes_nothing(
        self, monkeypatch, global_mode
    ):
        """O que o ``/skin`` antigo garantia (04aff891): a skin salva em ``IGNORE_SKINS`` não derruba a abertura e Salvar sem
        trocar não grava nada (nem escolha global)."""
        bot = Bot({"global_skin": global_mode})
        bot.settings = {"skin": "classic", "static_skin": "default"}
        bot.config["IGNORE_SKINS"] = "classic"

        opened = await run_skins(monkeypatch, bot, change=lambda d: d)
        screen = skins_view.SkinsView(
            bot=bot, author_id=900, tr=TR, choices=opened.screen.choices, initial=opened.screen.initial
        )

        assert "classic" not in opened.screen.choices.normal
        assert screen.payload()["embeds"]
        assert bot.global_writes == [] and bot.server_writes == []

    @pytest.mark.asyncio
    @pytest.mark.regression
    async def test_a_reply_that_fails_after_saving_still_reloads_the_players(self, monkeypatch):
        calls = []

        async def reload(self, *args, **kwargs):
            calls.append(args)

        monkeypatch.setattr(MusicSettings, "_reload_player_skins", reload)
        bot = Bot({"global_skin": False})
        error = disnake.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")

        await run_skins(monkeypatch, bot, change=lambda d: _replace(d, disc_motion=False), reply_error=error)

        assert bot.global_writes and len(calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.regression
    async def test_a_failure_after_the_global_write_still_reconciles_the_players_with_what_was_saved(self, monkeypatch):
        calls = []

        async def reload(self, *args, **kwargs):
            calls.append(args)

        async def down(*args, **kwargs):
            raise ConnectionError("banco fora")

        monkeypatch.setattr(MusicSettings, "_reload_player_skins", reload)
        bot = Bot({"global_skin": True})
        bot.pool.servers.patch = down

        def change(draft):
            return _server_skin(_replace(draft, global_mode=False), "muse_expanded_black")

        opened = await run_skins(monkeypatch, bot, change=change)

        assert bot.global_writes and bot.global_writes[-1][1]["global_skin"] is False
        assert opened.replies[-1]["embeds"][0].description == TR("skin.save_failed") and len(calls) == 1

    @pytest.mark.asyncio
    async def test_losing_the_radio_permission_before_saving_says_the_radio_edit_was_dropped(self, monkeypatch):
        bot = Bot({"global_skin": False}, store=RadioStore(radios=[_radio()]))

        def change(draft):
            monkeypatch.setattr(music_settings, "can_configure_radios", lambda member: False)
            return _replace(draft, radio_all="portrait_black")

        opened = await run_skins(monkeypatch, bot, change=change)

        text = opened.replies[-1]["embeds"][0].description
        assert bot.global_writes == [] and TR("skin.no_permission") in text and TR("skin.unchanged") in text

    @pytest.mark.asyncio
    async def test_the_global_radio_skin_redraws_the_radios_listed_at_save_time(self, monkeypatch):
        redrawn = []
        monkeypatch.setattr(music_settings, "redraw_radio_panels", lambda pool, gid, ids: redrawn.append(list(ids)))
        store = RadioStore(radios=[_radio()])
        bot = Bot({"global_skin": False}, store=store)

        def change(draft):
            store.radios.append(_radio(BOT_B, channel_id=6))  # criada depois de abrir o /skins
            return _replace(draft, radio_all="portrait_black")

        await run_skins(monkeypatch, bot, change=change)

        assert redrawn == [[BOT_A, BOT_B]]

    @pytest.mark.asyncio
    async def test_a_custom_skin_in_the_legacy_text_format_is_not_listed(self):
        bot = Bot({"custom_skins": {"Velha": "texto antigo"}, "custom_skins_static": {}})

        choices = await skins_view.load_choices(bot, bot.get_guild(GUILD), bot.global_data, radio_allowed=False)

        assert choices.custom_normal == () and choices.custom_static == ()

    @pytest.mark.asyncio
    @pytest.mark.regression
    @pytest.mark.parametrize("static", [False, True])
    async def test_a_reload_that_keeps_the_active_skin_still_stores_the_other_mode_too(self, static):
        player = SimpleNamespace(
            static=static, skin="muse_text", skin_static="muse_text", custom_skin_data={}, custom_skin_static_data={}
        )
        bot = Bot({"global_skin": False})
        changed = "skin" if static else "static_skin"  # o campo do modo que NÃO está ativo
        bot.settings = {"skin": "muse_text", "static_skin": "muse_text", changed: "muse_expanded_black"}
        bot.settings[f"{changed}_chosen"] = True
        bot.pool.get_all_bots = lambda: [SimpleNamespace(music=SimpleNamespace(players={GUILD: player}))]
        cog = SimpleNamespace(bot=bot)
        skins = SimpleNamespace(normal={"n": 1}, static={"s": 2})

        await MusicSettings._reload_player_skins(cog, bot, GUILD, bot.global_data, skins, "<@9>", TR)

        assert getattr(player, "skin" if static else "skin_static") == "muse_expanded_black"
        assert player.custom_skin_static_data == {"s": 2}


# ============================================================================ os padrões (Jukebox Texto e Rádio Texto)


class TestDefaults:
    @pytest.mark.parametrize("saved", [{}, {"skin": "default", "static_skin": "default"}])
    def test_a_server_that_never_chose_opens_on_the_text_skins(self, saved):
        # sem escolha (ou só com o "default" que o /skin antigo gravava; Q1 = não): Jukebox Texto nos dois modos e Rádio Texto
        draft = skins_view.initial_draft(Bot({}).pool, saved, {}, CHOICES)

        assert draft.server == draft.glob == ("muse_text", "muse_text")
        assert draft.radio_all == "text"

    @pytest.mark.asyncio
    async def test_the_default_marker_and_the_first_preview_are_the_text_skin(self):
        bot = Bot({})
        draft = skins_view.initial_draft(bot.pool, {}, {}, CHOICES)
        options = skins_view.player_options(bot, TR, CHOICES.normal, draft.server[0], bot.pool.default_skin)
        view = skins_view.SkinsView(bot=bot, author_id=900, tr=TR, choices=CHOICES, initial=draft)

        assert [o.value for o in options if o.description.startswith(TR("skin.default"))] == ["muse_text"]
        assert view.payload()["embeds"][1].title == TR("skin.preview.title", skin=TR("skin.name.muse_text"))


# ============================================================================ a view


def _view(choices: Choices = CHOICES) -> skins_view.SkinsView:
    return skins_view.SkinsView(bot=Bot({}), author_id=900, tr=TR, choices=choices, initial=INITIAL)


class TestView:
    @pytest.mark.asyncio
    async def test_someone_else_cannot_use_the_screen(self):
        view, sent = _view(), []

        async def send(*args, **kwargs):
            sent.append(kwargs)

        allowed = await view.interaction_check(SimpleNamespace(author=SimpleNamespace(id=1), send=send))
        assert allowed is False and sent == [{"ephemeral": True}]

    @pytest.mark.asyncio
    async def test_a_forged_select_value_is_refused_and_the_draft_does_not_change(self):
        view, sent = _view(), []

        async def send(*args, **kwargs):
            sent.append(args)

        await view._on_player("normal")(SimpleNamespace(values=["../x"], send=send))
        await view._on_target(SimpleNamespace(values=[f"radio:{STRANGER}"], send=send))
        assert view.draft == INITIAL and len(sent) == 2

    @pytest.mark.asyncio
    async def test_the_radio_pair_only_shows_with_radios(self):
        def rows(view):
            return sorted({item.row for item in view.children})

        assert rows(_view()) == [0, 1, 2, 3, 4]
        assert rows(_view(CHOICES._replace(radios=()))) == [0, 1, 4]

    @pytest.mark.asyncio
    async def test_after_the_timeout_a_custom_pick_is_refused_and_the_screen_stays_off(self):
        view, edits, sent = _view(), [], []

        async def edit(**kwargs):
            edits.append(kwargs)

        view.message = SimpleNamespace(edit=edit)
        custom = skins_view.CustomSkinsView(view)
        view.stop()  # o que o disnake faz antes do ``on_timeout``
        await view.on_timeout()
        await custom.on_timeout()

        async def send(*args, **kwargs):
            sent.append(args)

        await custom._on_pick("normal")(SimpleNamespace(values=["custom:Minha"], send=send))
        await view.refresh()  # nenhuma edição religa a tela encerrada

        assert all(item.disabled for item in view.children) and all(item.disabled for item in custom.children)
        assert view.draft == INITIAL and len(edits) == 1 and sent == [(TR("skin.invalid"),)]

    @pytest.mark.asyncio
    async def test_after_the_custom_screens_own_timeout_a_pick_is_refused_with_the_main_screen_open(self):
        view, sent = _view(), []
        custom = skins_view.CustomSkinsView(view)
        custom.stop()  # só a efêmera acabou: o clique já agendado ainda chega ao callback

        async def send(*args, **kwargs):
            sent.append(args)

        await custom._on_pick("normal")(SimpleNamespace(values=["custom:Minha"], send=send))

        assert not view.is_finished() and view.draft == INITIAL and sent == [(TR("skin.invalid"),)]

    @pytest.mark.asyncio
    @pytest.mark.regression
    @pytest.mark.parametrize("first, second", [("_on_cancel", "_on_save"), ("_on_save", "_on_cancel")])
    async def test_a_second_click_already_scheduled_does_not_overwrite_the_result(self, first, second):
        view, sent = _view(), []

        async def send(*args, **kwargs):
            sent.append(args)

        inter = SimpleNamespace(send=send)
        await getattr(view, first)(inter)
        kept = view.result
        await getattr(view, second)(inter)

        assert view.result == kept and sent == [(TR("skin.invalid"),)]

    @pytest.mark.asyncio
    async def test_outside_names_are_escaped_in_embeds_and_raw_only_in_selects(self):
        bot = Bot({}, store=RadioStore(radios=[_radio()]), channels={5: SimpleNamespace(name="**x**")})
        (row,) = await skins_view.radio_rows(bot, bot.get_guild(GUILD))
        assert row.label == f"#**x** · {BOT_A}" and row.text == f"#\\*\\*x\\*\\* · {BOT_A}"

        plan = SavePlan({"player_skin": "> custom_skin: **c**"}, {}, (RadioLook(BOT_A, "text", 3),))
        summary = "\n".join(skins_view.summary_lines(TR, plan, (row,)))
        assert "\\*\\*c\\*\\*" in summary and row.text in summary and "#**x**" not in summary

        view = _view(CHOICES._replace(radios=(row,)))
        view.draft = _replace(view.draft, target=f"radio:{BOT_A}", focus="radio", radio_own=((BOT_A, None),))
        view.rebuild()
        assert row.text in view.payload()["embeds"][1].description
        assert any(row.label in (item.placeholder or "") for item in view.children if hasattr(item, "placeholder"))
