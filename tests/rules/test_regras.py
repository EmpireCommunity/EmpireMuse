"""F3-5b — as regras centralizadas em ``empiremuse.rules``, chamadas DIRETAMENTE (funções puras, sem dublês).

Complementam a caracterização por comportamento dos comandos e do painel: se uma regra mudar aqui sem querer, os dois
lados mudam juntos, então cada função e cada limite têm o valor atual pinado com números literais (que não mudam
junto com a constante). Quando uma etapa seguinte alterar uma regra de propósito, o teste que a pina é invertido nela.
"""

import json
from types import SimpleNamespace

import pytest

from empiremuse.panel.services.appearance import reload_skin
from empiremuse.rules import limits, quota, settings, text, urls
from tests.support.drivers_de_regras import BotDoPainel, painel_favoritos

pytestmark = pytest.mark.critical

URL = "https://exemplo.invalid/a"


class TestTexto:
    @pytest.mark.parametrize("valor", [None, {"a": 1}])
    def test_clean_title_so_aceita_texto(self, valor):
        assert text.clean_title(valor, 1, 10) is None

    @pytest.mark.smoke
    @pytest.mark.parametrize(
        ("entrada", "baixo", "alto", "esperado"),
        [
            ("  ab  ", 2, 5, "ab"),
            ("a b", 2, 5, "a b"),
        ],
    )
    def test_clean_title(self, entrada, baixo, alto, esperado):
        assert text.clean_title(entrada, baixo, alto) == esperado

    @pytest.mark.regression
    @pytest.mark.parametrize(
        ("escapado", "recusado"), [("\\ud800", True), ("\\u2028", True), ("\\u2029", True), ("\\ud83c\\udfb5", False)]
    )
    def test_surrogate_isolado_e_separadores_de_linha_sao_recusados_e_o_par_do_emoji_passa(self, escapado, recusado):
        """V-158-1 na regra compartilhada: ``Cs``, ``Zl`` e ``Zp`` chegam por escape JSON; o par de surrogates (🎵) é um caractere só."""
        assert text.has_control_or_format(json.loads(f'"Ro{escapado}ck"')) is recusado

    @pytest.mark.regression
    def test_titulo_de_faixa_perde_surrogate_isolado_e_separadores_de_linha_e_mantem_o_emoji(self):
        """V-158-1 no título externo das integrações: ``Cs``, ``Zl`` e ``Zp`` saem como ``Cc``/``Cf``; o par do 🎵 fica."""
        bruto = json.loads('"Ro\\ud800c\\u2028k \\u2029\\ud83c\\udfb5"')
        assert text.clean_track_name(bruto, 1, 90) == "Rock 🎵"

    def test_clean_import_name_recusa_repetido_sem_diferenciar_a_caixa(self):
        assert text.clean_import_name("Rock", 2, 25, {}) == "Rock"
        assert text.clean_import_name("Rock", 2, 25, {"rock": 1}) is None
        assert text.clean_import_name("rock", 2, 25, {"ROCK": 1}) is None
        assert text.clean_import_name("Pop", 2, 25, {"Rock": 1}) == "Pop"
        assert text.clean_import_name(" Rock ", 2, 25, {}) == "Rock"
        assert text.clean_import_name("x", 2, 25, {}) is None

    def test_drop_same_names_tira_as_chaves_iguais_sem_diferenciar_a_caixa(self):
        entradas = {"Rock": 1, "pop": 2, "Jazz": 3}
        text.drop_same_names(entradas, ["rock", "POP"])
        assert entradas == {"Jazz": 3}
        intactas = {"Rock": 1}
        text.drop_same_names(intactas, ["Pop"])
        assert intactas == {"Rock": 1}

    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("a\nb", "a b"),
            ("", ""),
        ],
    )
    def test_normalize_status(self, entrada, esperado):
        assert text.normalize_status(entrada) == esperado


class TestUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "http://exemplo.invalid/a",
            "https://exemplo.invalid/a_b-c.d",
        ],
    )
    def test_url_inteira_e_limpa(self, url):
        assert urls.URL_REG.fullmatch(url) and urls.is_clean_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://exemplo.invalid/a",
            "https://exemplo.invalid/a texto",
            "https://exemplo.invalid/a]b",
            "https://exemplo.invalid/a<b>",
            "https://exemplo.invalid/a#z",
            "",
        ],
    )
    def test_url_que_nao_e_inteira_e_limpa(self, url):
        assert not urls.is_clean_url(url)

    @pytest.mark.parametrize(
        ("avatar", "esperado"),
        [
            (None, ""),
            (URL, URL),
            (123, None),
            ("   ", None),
        ],
    )
    def test_clean_avatar(self, avatar, esperado):
        assert urls.clean_avatar(avatar) == esperado

    def test_clean_avatar_limite_de_250_depois_do_strip(self):
        base = "https://exemplo.invalid/"
        no_limite = base + "x" * (250 - len(base))
        assert urls.clean_avatar(no_limite) == no_limite
        assert urls.clean_avatar(f"  {no_limite}  ") == no_limite
        assert urls.clean_avatar(no_limite + "x") is None


class TestCota:
    @pytest.mark.parametrize(
        ("maximo", "dono", "esperado"),
        [(10, False, 10), (-5, True, 0)],
    )
    def test_quota_limit(self, maximo, dono, esperado):
        assert quota.quota_limit(maximo, dono) == esperado


class TestSkinsPermitidas:
    def test_allowed_names(self):
        assert settings.allowed_names(["a", "b", "c"], "") == ("a", "b", "c")
        assert settings.allowed_names(["a", "b", "c"], "b") == ("a", "c")
        assert settings.allowed_names(["a", "b", "c"], "a  c") == ("b",)
        assert settings.allowed_names(["a", "b"], "a b") == ()
        assert settings.allowed_names({"x": 1, "y": 2}, "x") == ("y",)
        assert settings.allowed_names(["ab"], "a") == ("ab",)


class TestPontosDeUsoDasRegras:
    @pytest.mark.asyncio
    async def test_painel_aceita_url_de_favorito_de_exatamente_10_caracteres(self):
        curta = "http://a.b"
        assert len(curta) == limits.FAV_URL_MIN
        aceito = await painel_favoritos(BotDoPainel(), "favorito_adicionar", {}, nome="novo", url=curta)
        recusado = await painel_favoritos(BotDoPainel(), "favorito_adicionar", {}, nome="novo", url="http://a.")
        assert aceito.errors == {} and aceito.new["fav_links"]["novo"] == curta
        assert recusado.errors.get("url")

    @pytest.mark.asyncio
    async def test_painel_nao_recarrega_skin_personalizada_e_recarrega_a_normal(self):
        chamadas = []

        async def destruir():
            chamadas.append("destruir")

        async def invocar(**kwargs):
            chamadas.append("invocar")

        def nada(*args, **kwargs):
            chamadas.append("ajuste")

        player = SimpleNamespace(
            static=False,
            skin="classic",
            skin_static="simple",
            destroy_message=destruir,
            invoke_np=invocar,
            setup_features=nada,
            setup_hints=nada,
            process_hint=nada,
            set_command_log=nada,
        )
        await reload_skin(player, "> custom_skin: minha", "simple", "texto")
        assert chamadas == [] and player.skin == "classic"
        await reload_skin(player, "modern", "simple", "texto")
        assert player.skin == "modern" and "destruir" in chamadas and "invocar" in chamadas


@pytest.mark.regression
@pytest.mark.parametrize(("escapado", "aceito"), [("\\ud800", False), ("\\u2028", False), ("\\ud83c\\udfb5", True)])
def test_prefixo_com_surrogate_isolado_e_recusado_e_o_emoji_passa(escapado, aceito):
    """V-158-1 no prefixo (Sentinela, PR 255): o painel recebe o prefixo por JSON, então o escape de um surrogate isolado chegava."""
    from empiremuse.rules.errors import RuleError
    from empiremuse.rules.settings import validate_prefix

    prefixo = json.loads(f'"!{escapado}a"')
    assert (validate_prefix(prefixo) == prefixo) if aceito else isinstance(validate_prefix(prefixo), RuleError)


@pytest.mark.regression
def test_descricao_gravada_antes_da_regra_reenviada_sem_mudanca_perde_o_caractere_recusado():
    """V-158-1 na descrição do link do servidor (Sentinela, PR 255): reenviar a gravada não é troca, mas ela também passa pela regra.
    O caractere recusado sai (como nos filtros) em vez de recusar o formulário inteiro; sem sobrar o mínimo, a descrição fica vazia."""
    from empiremuse.rules.guild_links import check_description

    gravada = json.loads('"Rock \\ud800 classico"')
    assert check_description(gravada, gravada) == "Rock  classico"
    assert check_description("ok ", "ok ") == ""  # sobra menos que o mínimo (3)
    assert check_description("Rock", "Rock") == "Rock"
