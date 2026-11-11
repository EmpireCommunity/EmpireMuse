"""``ExternalPanelSettings`` fail-closed (contrato T-456 §2, §6, §14 [M3, M4, N1, m1, m9, n3, n4])."""

import pytest

from empiremuse.api.settings import ExternalPanelConfigError, external_panel_requested, load_external_panel_settings
from tests.api.helpers import (
    API_PORT,
    COOKIE_SECRET,
    NOW,
    OAUTH_SECRET,
    PANEL_URL,
    PREVIOUS_KEY,
    SERVICE_KEY,
    SIGNING_SECRET,
    WEB_PORT,
    external_config,
)

pytestmark = pytest.mark.critical


def load(config: dict, **kwargs):
    return load_external_panel_settings(config, web_port=WEB_PORT, now=NOW, **kwargs)


def test_sem_nenhuma_variavel_o_modo_externo_fica_desligado():
    assert load({}) is None
    assert load({"EXTERNAL_PANEL_URL": "  ", "EXTERNAL_PANEL_API_PORT": ""}) is None
    assert external_panel_requested({"EXTERNAL_PANEL_API_BIND": "x"}) is True
    assert external_panel_requested({}) is False


def test_configuracao_valida_funciona_com_o_painel_generico_desligado():
    settings = load(external_config(PANEL_ENABLED=False, PANEL_COOKIE_SECRET=""))

    assert settings is not None
    assert (settings.base_url, settings.port, settings.bind) == (PANEL_URL, API_PORT, "127.0.0.1")
    assert settings.redirect_uri == PANEL_URL + "/auth/callback"
    assert settings.signing_secret == SIGNING_SECRET
    assert settings.signing_secret != ""
    assert SERVICE_KEY not in repr(settings) and SIGNING_SECRET not in repr(settings)


@pytest.mark.parametrize(
    "faltando",
    [
        "EXTERNAL_PANEL_URL",
        "EXTERNAL_PANEL_API_SECRET",
        "EXTERNAL_PANEL_SIGNING_SECRET",
        "EXTERNAL_PANEL_API_PORT",
        "EXTERNAL_PANEL_API_BIND",
        "PANEL_OAUTH_CLIENT_SECRET",
    ],
)
def test_falta_de_qualquer_configuracao_recusa_subir(faltando):
    config = external_config()
    del config[faltando]
    with pytest.raises(ExternalPanelConfigError):
        load(config)


@pytest.mark.parametrize(
    "url",
    [
        "lixo",
        "ftp://painel.example.com",
        "http://painel.example.com",  # http fora de localhost
        "https://usuario:senha@painel.example.com",
        "https://usuario@painel.example.com",
        "https://painel.example.com\\@evil.example",
        "https://painel.example.com/caminho",
        "https://painel.example.com?x=1",
        "https://pai\nnel.example.com",
        "https://painel.example.com:99999",
    ],
)
def test_url_do_painel_invalida_recusa(url):
    with pytest.raises(ExternalPanelConfigError):
        load(external_config(EXTERNAL_PANEL_URL=url))


def test_url_com_barra_final_e_normalizada():
    assert load(external_config(EXTERNAL_PANEL_URL=PANEL_URL + "/")).base_url == PANEL_URL


@pytest.mark.parametrize("porta", ["", "0", "65536", "abc", "6000", " 6000 "])
def test_porta_ausente_invalida_ou_igual_a_do_bot_recusa(porta):
    with pytest.raises(ExternalPanelConfigError):
        load(external_config(EXTERNAL_PANEL_API_PORT=porta))


@pytest.mark.parametrize("bind", ["", "localhost", "painel.interno", "fe80::1%eth0", "300.1.1.1"])
def test_bind_ausente_ou_que_nao_e_ip_recusa(bind):
    with pytest.raises(ExternalPanelConfigError):
        load(external_config(EXTERNAL_PANEL_API_BIND=bind))


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "172.20.0.2", "10.1.2.3", "2001:db8::2"])
def test_n1_endereco_fora_do_loopback_exige_cidrs(bind):
    with pytest.raises(ExternalPanelConfigError):
        load(external_config(EXTERNAL_PANEL_API_BIND=bind))
    settings = load(
        external_config(EXTERNAL_PANEL_API_BIND=bind, EXTERNAL_PANEL_API_CIDRS="172.20.0.0/24, 2001:db8::/64")
    )
    assert settings is not None and len(settings.cidrs) == 2


@pytest.mark.parametrize("cidrs", ["", "lixo", "172.20.0.0/24, lixo", "172.20.0.5/24", "0.0.0.0/0", "::/0"])
def test_cidrs_invalidos_ou_largos_demais_recusam(cidrs):
    with pytest.raises(ExternalPanelConfigError):
        load(external_config(EXTERNAL_PANEL_API_BIND="172.20.0.2", EXTERNAL_PANEL_API_CIDRS=cidrs))


def test_loopback_aceita_ipv6_sem_cidrs():
    assert load(external_config(EXTERNAL_PANEL_API_BIND="::1")) is not None


@pytest.mark.parametrize(
    "chave",
    [
        "EXTERNAL_PANEL_API_SECRET",
        "EXTERNAL_PANEL_SIGNING_SECRET",
    ],
)
def test_chave_curta_recusa_o_boot(chave):
    with pytest.raises(ExternalPanelConfigError):
        load(external_config(**{chave: "x" * 31}))
    assert load(external_config(**{chave: "x" * 32 if chave.endswith("API_SECRET") else "y" * 32})) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"EXTERNAL_PANEL_SIGNING_SECRET": SERVICE_KEY},
        {"PANEL_COOKIE_SECRET": SERVICE_KEY},
        {"PANEL_OAUTH_CLIENT_SECRET": SERVICE_KEY},
        {"EXTERNAL_PANEL_SIGNING_SECRET": OAUTH_SECRET},
        {"PANEL_COOKIE_SECRET": SIGNING_SECRET},
        {"RPC_SERVER_SECRET": SERVICE_KEY},
        {"RPC_SERVER_SECRET": SIGNING_SECRET},
    ],
)
def test_chaves_repetidas_entre_si_recusam_o_boot(overrides):
    with pytest.raises(ExternalPanelConfigError):
        load(external_config(**overrides))


def test_rpc_server_secret_curto_nao_e_recusado_e_so_a_igualdade_conta():
    assert load(external_config(RPC_SERVER_SECRET="curto")) is not None
    assert load(external_config(RPC_SERVER_SECRET="")) is not None


def previous(**extra):
    return external_config(
        **{
            "EXTERNAL_PANEL_API_SECRET_PREVIOUS": PREVIOUS_KEY,
            "EXTERNAL_PANEL_API_PREVIOUS_UNTIL": "2026-11-15",
            **extra,
        }
    )


def test_n4_chave_anterior_valida_vale_ate_o_fim_do_dia_da_data():
    settings = load(previous())

    assert settings.previous_key == PREVIOUS_KEY
    assert settings.previous_expires_at == pytest.approx(NOW + (4 * 86400) + 12 * 3600)  # 2026-11-16 00:00 UTC


@pytest.mark.parametrize(
    "until",
    [
        "",
        "amanha",
        "2026-13-40",
        "2026-11-10",
        "2026-11-19",
        "2026-11-18T10:00",
    ],  # sem data, inválida, vencida, > 7 dias
)
def test_n4_chave_anterior_sem_data_valida_e_ignorada_sem_recusar_o_boot(until, caplog):
    settings = load(
        external_config(EXTERNAL_PANEL_API_SECRET_PREVIOUS=PREVIOUS_KEY, EXTERNAL_PANEL_API_PREVIOUS_UNTIL=until)
    )

    assert settings.previous_key == ""
    assert PREVIOUS_KEY not in caplog.text


def test_chave_anterior_curta_ou_igual_a_atual_ou_aos_outros_segredos_recusa():
    for bad in ("curta", SERVICE_KEY, COOKIE_SECRET, OAUTH_SECRET, SIGNING_SECRET):
        with pytest.raises(ExternalPanelConfigError):
            load(previous(EXTERNAL_PANEL_API_SECRET_PREVIOUS=bad, PANEL_COOKIE_SECRET=COOKIE_SECRET))


def test_mensagens_de_erro_nunca_trazem_valores_de_segredos():
    with pytest.raises(ExternalPanelConfigError) as erro:
        load(external_config(EXTERNAL_PANEL_SIGNING_SECRET=SERVICE_KEY))
    assert SERVICE_KEY not in str(erro.value)
