"""O ``/skins`` (2.3, S6): o que o Salvar grava, quem pode gravar e o que é recusado sem escrita.

Superfícies de risco (contrato S6 §5 e §10 item 6; ameaça SK-12): valor forjado (``../x``, id fora do registro, rádio de
outro servidor) recusado sem escrita; sem a permissão das rádios o par não aparece nem grava; Cancelar não grava; conflito
de CAS numa rádio não desfaz as outras; o servidor é sempre o da interação. Só o Discord, o banco e a store são dublês.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

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
from empiremuse.utils.radio.store import RadioConflict, RadioNotFound

pytestmark = pytest.mark.critical

GUILD = 4242
BOT_A, BOT_B, STRANGER = 11, 12, 99
TR = get_i18n().for_locale("pt-BR")
RADIOS = (RadioRow(BOT_A, "#a · A", None, 3), RadioRow(BOT_B, "#b · B", "white", 7))
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


# ============================================================================ gravação


class RadioStore:
    def __init__(self, conflicts=()):
        self.conflicts = set(conflicts)
        self.calls = []

    async def set_look(self, guild_id, radio_id, look, *, expected_rev, updated_by):
        self.calls.append((guild_id, radio_id, look, expected_rev))
        if radio_id in self.conflicts:
            raise RadioConflict
        if not radio_id.endswith(f":{guild_id}"):
            raise RadioNotFound

    async def list_for_guild(self, guild_id):
        return []


class Bot:
    """O bot, o pool e o banco de mentira: ``get_global_data`` devolve a mesma referência do cache, como o banco real."""

    def __init__(self, global_data, store=None):
        self.global_data = {"custom_skins": {}, "custom_skins_static": {}, **global_data}  # o modelo padrão do banco
        self.global_writes = []
        self.server_writes = []
        self.settings = {"skin": "default", "static_skin": "default"}
        self.store = store or RadioStore()
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
            default_skin="muse_expanded_black",
            default_static_skin="muse_expanded_black",
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
        return SimpleNamespace(id=guild_id, me=SimpleNamespace(), get_channel=lambda cid: None)

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


# ============================================================================ o comando de ponta a ponta (tela de mentira)


def _member(*, manage_guild=True):
    perms = SimpleNamespace(manage_guild=manage_guild, administrator=False)
    return SimpleNamespace(id=900, mention="<@900>", guild_permissions=perms)


async def _noop(*args, **kwargs):
    return None


async def run_skins(monkeypatch, bot, *, change, result="save", saver=None, radio_allowed=True):
    """Roda o ``MusicSettings.change_skin`` REAL; a tela devolve o rascunho que ``change(initial)`` monta."""
    replies = []

    async def edit(*args, **kwargs):
        replies.append(kwargs)

    member = saver or _member()

    class Screen:
        def __init__(self, *, initial, choices, **kwargs):
            self.initial, self.choices = initial, choices
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
    interaction = SimpleNamespace(
        guild_id=GUILD,
        guild=bot.get_guild(GUILD),
        author=_member(),
        response=SimpleNamespace(defer=_noop),
        edit_original_message=edit,
    )
    await MusicSettings.change_skin.callback(cog, interaction)
    return replies


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
        replies = await run_skins(monkeypatch, bot, change=lambda d: _server_skin(d, "muse_text"), saver=lost)
        assert bot.global_writes == [] and bot.server_writes == []
        assert replies[-1]["embeds"][0].description == TR("skin.no_permission")

    @pytest.mark.asyncio
    async def test_forged_value_is_refused_without_writing(self, monkeypatch):
        bot = Bot({"global_skin": False})
        forged = lambda d: _replace(_server_skin(d, "../x"), disc_motion=False)  # noqa: E731
        replies = await run_skins(monkeypatch, bot, change=forged)
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
        await run_skins(monkeypatch, bot, change=lambda d: _server_skin(d, "muse_text"))
        assert bot.server_writes == [(GUILD, "settings", {"skin": "muse_text"})] and bot.global_writes == []


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
