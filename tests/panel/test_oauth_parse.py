"""Ressalva do AppSec (E3): parse_managed_guilds descarta itens com `id` inválido sem quebrar a lista."""

from tests.panel.helpers import require
import pytest

pytestmark = pytest.mark.critical


def item(guild_id, **extra):
    return {"id": guild_id, "name": "Srv", "icon": None, "owner": True, "permissions": "8", **extra}


class TestParseManagedGuilds:
    def test_non_numeric_or_missing_ids_are_dropped_and_valid_ones_kept(self):
        parse = require("empiremuse.panel.oauth").parse_managed_guilds
        raw = [
            item("100"),
            item("12a"),
            item(""),
            item(None),
            item(" 12"),
            item("-5"),
            item("1.5"),
            item("٥٠٠"),
            item(123),
            {"name": "sem id", "permissions": "8", "owner": True},
            item("200"),
        ]
        assert [g.id for g in parse(raw)] == ["100", "200"], "só ids numéricos (str de dígitos ASCII) sobrevivem"

    def test_garbage_items_do_not_break_the_list(self):
        parse = require("empiremuse.panel.oauth").parse_managed_guilds
        raw = [item("100"), None, "texto", 5, [], item("300")]
        assert [g.id for g in parse(raw)] == ["100", "300"]


# --------------------------------------------------------------------------- T-660: servidores em que a pessoa é só membro


@pytest.mark.critical
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([{"id": "10"}, {"id": "20"}], {"10", "20"}),
        ([{"id": "10"}, {"id": "10"}], {"10"}),  # duplicado
        ([{"id": "1" * 21}, {"id": "1a"}, {"id": 30}, {"id": ""}, {"name": "x"}, "texto", None], set()),  # formato
        ([{"id": "99"}], set()),  # gerenciável: fica em ``guilds``, nunca aqui
        ([{"id": "77"}], set()),  # sem bot pronto do pool
    ],
)
def test_member_guild_ids_keep_only_well_formed_unmanaged_ids_with_a_ready_bot(raw, expected):
    from empiremuse.panel.oauth import parse_member_guild_ids

    found = parse_member_guild_ids(raw, managed_ids={"99"}, has_ready_bot=lambda gid: gid != "77")

    assert found == frozenset(expected) and isinstance(found, frozenset)


@pytest.mark.critical
def test_member_guild_ids_of_a_non_list_is_empty():
    from empiremuse.panel.oauth import parse_member_guild_ids

    assert parse_member_guild_ids({"id": "10"}, managed_ids=set(), has_ready_bot=lambda gid: True) == frozenset()
