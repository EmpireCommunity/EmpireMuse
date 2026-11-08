"""Regras puras dos comandos de owner (``empiremuse.rules.owner_shell``): ambiente mínimo e mascaramento de segredos."""

import pytest

from empiremuse.rules.owner_shell import MASK, build_shell_env, collect_secret_values, mask_secrets

pytestmark = pytest.mark.regression


def test_ambiente_do_shell_so_leva_a_allowlist():
    ambiente = {
        "PATH": "/bin",
        "HOME": "/h",
        "LANG": "C",
        "GIT_DIR": "/g/.git",
        "TOKEN": "segredo-123",
        "MONGO": "mongodb://u:p@h",
    }
    assert build_shell_env(ambiente) == {"PATH": "/bin", "HOME": "/h", "LANG": "C", "GIT_DIR": "/g/.git"}


def test_ambiente_do_windows_leva_systemroot_e_comspec_sem_diferenciar_maiusculas():
    ambiente = {"SYSTEMROOT": "C:\\Windows", "ComSpec": "cmd.exe", "PATH": "C:\\x", "TOKEN": "segredo-123"}
    assert build_shell_env(ambiente, windows=True) == {
        "SYSTEMROOT": "C:\\Windows",
        "ComSpec": "cmd.exe",
        "PATH": "C:\\x",
    }
    assert "SYSTEMROOT" not in build_shell_env(ambiente)


def test_mascara_so_os_valores_de_segredo_com_tamanho_minimo():
    configuracao = {
        "TOKEN": "tok-abcdef",
        "SPOTIFY_CLIENT_SECRET": "sp-123456",
        "DEFAULT_PREFIX": "!!!!!!!!",
        "MONGO": "ab",
        "RUN": True,
    }
    ambiente = {"TOKEN_RESERVE_2": "tok-abcdef", "LAVALINK_NODE_1": "senha-do-no", "EMBED_COLOR": "8B5CF6"}
    segredos = collect_secret_values(configuracao, ambiente)
    assert segredos == {"tok-abcdef", "sp-123456", "senha-do-no"}

    saida = mask_secrets("a tok-abcdef b senha-do-no c !!!!!!!! d", segredos)
    assert saida == f"a {MASK} b {MASK} c !!!!!!!! d"


def test_mascara_o_valor_mais_longo_primeiro():
    assert mask_secrets("abcdef-extra", {"abcdef", "abcdef-extra"}) == MASK


@pytest.mark.critical
@pytest.mark.regression
def test_shell_masks_normalized_and_rejected_tokens_without_changing_common_text():
    from empiremuse.rules.bot_tokens import load_bot_tokens
    from empiremuse.rules.owner_shell import mask_shell_output
    from tests.rules.test_bot_tokens import token

    loaded = load_bot_tokens({"TOKEN": '"' + token() + '"', "TOKEN_RESERVE_X": token("b") + " " + token("c")})
    assert loaded.issues[0].code == "FORMAT"
    secrets = frozenset(bot.reveal() for bot in loaded.bots)
    assert mask_shell_output(token(), secrets) == "***"
    # A reserva rejeitada e um token legado nunca admitido no pool são cobertos pela forma.
    assert mask_shell_output(token("b") + " " + token("d"), secrets) == "*** ***"
    assert mask_shell_output("texto comum key=valor token=exemplo", secrets) == "texto comum key=valor token=exemplo"
