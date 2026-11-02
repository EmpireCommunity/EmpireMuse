"""O uso dos comandos de prefixo vem do catálogo (``cmd.<id>.usage``) no idioma do servidor, no ``help`` e no erro de argumento."""

from types import SimpleNamespace

import pytest
from disnake.ext import commands

from empiremuse.i18n.service import get_i18n
from empiremuse.utils.music.command_usage import command_usage
from empiremuse.utils.music.errors import parse_error


def _seek():
    return SimpleNamespace(name="seek", qualified_name="seek", full_parent_name="", usage=None)


@pytest.mark.parametrize(
    ("locale", "esperado"),
    [("pt-BR", "[tempo]"), ("en-US", "[time]")],
)
def test_o_uso_do_seek_segue_o_idioma(locale, esperado):
    texto = command_usage(get_i18n().for_locale(locale), _seek(), "!")

    assert texto.startswith(f"!seek {esperado}")
    assert "{prefix}" not in texto


def test_skip_mostra_os_sinais_de_opcional():
    comando = SimpleNamespace(name="skip", qualified_name="skip", full_parent_name="", usage=None)

    assert "<termo>" in command_usage(get_i18n().for_locale("pt-BR"), comando, "!")
    assert "<term>" in command_usage(get_i18n().for_locale("en-US"), comando, "!")


def test_comando_sem_chave_cai_no_usage_do_decorador_e_sem_nada_devolve_none():
    com = SimpleNamespace(name="x", qualified_name="x", full_parent_name="", usage="{prefix}{cmd} abc")
    sem = SimpleNamespace(name="y", qualified_name="y", full_parent_name="", usage=None)
    tr = get_i18n().for_locale("pt-BR")

    assert command_usage(tr, com, "!") == "!x abc"
    assert command_usage(tr, sem, "!") is None


def test_o_erro_de_argumento_explica_o_uso_no_idioma_do_servidor():
    ctx = SimpleNamespace(
        command=_seek(), prefix="!", me=SimpleNamespace(id=1, display_name="Bot"), guild=None, author=None
    )

    texto, *_ = parse_error(ctx, commands.BadArgument("x"), no_log=True, tr=get_i18n().for_locale("en-US"))

    assert "How to use" in texto and "[time]" in texto
