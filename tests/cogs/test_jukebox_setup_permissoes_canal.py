"""`/jukebox setup` num canal existente: "gerenciar permissões" do canal é opcional; o que o setup precisa de verdade é ver, enviar, embutir e apagar mensagens."""

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.utils.music import jukebox_channel
from empiremuse.utils.music.errors import GenericError
from tests.cogs.test_jukebox_setup_reset import Scenario
from tests.support.canal_da_jukebox import FULL_TEXT, perms


def text_without(*names):
    return perms(**{k: v for k, v in FULL_TEXT.items() if k not in names})


@pytest.fixture
def world(monkeypatch):
    scenario = Scenario(monkeypatch)
    scenario.bot.pool.i18n = get_i18n()
    return scenario


def test_the_existing_channel_without_manage_permissions_is_set_up_without_editing_it_and_warns_what_is_missing(world):
    world.text._perms = text_without("manage_permissions", "attach_files")

    world.setup(world.text)

    assert world.saved["channel"] == "22"  # configurou
    assert world.j.of("channel.edit") == []  # sem editar o canal
    text = world.final_embed_text()
    assert "Gerenciar Permissões" in text or "Manage Permissions" in text


def test_with_manage_permissions_the_channel_is_edited_and_there_is_no_warning(world):
    world.setup(world.text)

    assert len(world.j.of("channel.edit")) == 1
    assert "Permissions" not in world.final_embed_text() and "Permissões" not in world.final_embed_text()


def test_the_validation_no_longer_refuses_a_channel_only_because_manage_permissions_is_missing(world):
    world.text._perms = text_without("manage_permissions")

    jukebox_channel.validate_jukebox_target(world.bot, world.guild, world.text, existing_channel=True)


def test_purging_needs_manage_messages_so_its_absence_is_refused_up_front(world):
    world.text._perms = text_without("manage_messages")

    with pytest.raises(GenericError):
        jukebox_channel.validate_jukebox_target(world.bot, world.guild, world.text, existing_channel=True)
