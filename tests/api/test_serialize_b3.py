"""B3 sobre a serialização (``rules/panel_serialize``): campo numérico, acesso ao servidor, convites e exportação."""

import pytest

from empiremuse.panel.authz import BotRef, GuildAccess
from empiremuse.panel.authz import InviteLink
from empiremuse.panel.services.base import Formulario
from empiremuse.rules import panel_serialize as ps

pytestmark = pytest.mark.critical


def player_form(volume):
    values = {
        "default_player_volume": volume,
        "autoplay": False,
        "check_other_bots_in_vc": False,
        "enable_restrict_mode": False,
    }
    return Formulario(key="player", form_id="f", template="t", acao="/x", versao="0" * 16, valores=values)


@pytest.mark.parametrize("value", ["60", 60, 60.0, "60.0", "60.00"])
def test_o_volume_sai_como_inteiro_do_texto_do_formulario_do_inteiro_e_do_inteiral_em_float_ou_texto(value):
    volume = ps.serialize_form(player_form(value), ps.GUILD)["values"]["default_player_volume"]

    assert volume == 60 and type(volume) is int


@pytest.mark.parametrize(
    "bad",
    [
        "sessenta",
        "6.5",
        "60.5",
        "-1",
        "",
        "5e1",
        " 60",
        True,
        6.5,
        float("inf"),
        float("nan"),
        None,
        ["6"],
        "9" * 30,
        1e30,
    ],
)
def test_valor_que_nao_e_inteiro_no_campo_numerico_falha_fechado(bad):
    with pytest.raises(ps.SerializationError):
        ps.serialize_form(player_form(bad), ps.GUILD)


def test_acesso_ao_servidor_so_leva_a_lista_de_permissao():
    access = GuildAccess("100", "Alfa", None, (BotRef("1", "Bot", "https://cdn/a.png"),))

    assert ps.serialize_access(access) == {
        "id": "100",
        "name": "Alfa",
        "icon_url": None,
        "bots": [{"id": "1", "name": "Bot", "avatar_url": "https://cdn/a.png"}],
    }


def test_convites_e_exportacao_dos_favoritos():
    assert ps.serialize_invites([InviteLink("Bot", "https://cdn/a.png", "https://discord.com/x")]) == [
        {"bot_name": "Bot", "bot_avatar_url": "https://cdn/a.png", "url": "https://discord.com/x"}
    ]
    assert ps.serialize_favorites_export({"Rock": "https://e.com/r", 7: 8}) == {"Rock": "https://e.com/r", "7": "8"}
    with pytest.raises(ps.SerializationError):
        ps.serialize_favorites_export(["Rock"])
    with pytest.raises(ps.SerializationError):
        ps.serialize_access("não é um acesso")
