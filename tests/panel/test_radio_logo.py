"""Item 8 (R4, Vigil F-5..F-7) · Rádios: o logo da estação servido pela própria origem do painel.

Caminhos de risco: só com sessão (anônimo: 404 sem redirecionar e sem cookie); a chave é só o uuid canônico da rota
(maiúsculas, curto, traversal ou qualquer parâmetro: 404 ou ignorado); os bytes vêm só de ``directory.logo_png`` e
qualquer outra coisa (None, falha, não-PNG) vira o ícone padrão, com os cabeçalhos exatos e o ``max-age`` certo
(86400 logo, 300 padrão, 60 sem ficha); a URL do favicon nunca aparece na página nem nos fragmentos; o ``<img>`` é
montado no servidor a partir do uuid, com ``referrerpolicy="no-referrer"`` e tamanho fixo.
"""

import re
from typing import NamedTuple

from empiremuse.panel.handlers.radio_logo import DEFAULT_ICON, LOGO_PATH
from tests.panel.helpers_radio import PNG_1PX, LogoResult
from tests.panel.test_radio_live import A, B, C, SECRET, LiveBase
from tests.panel.test_settings_http import Each

FAVICON = "https://logo.example/alfa/logo.png"
EXPECTED = {
    "Content-Type": "image/png",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class StationWithLogo(NamedTuple):
    """A ``Station`` do diretório com o campo ``favicon`` (Turbine): o painel nunca o lê."""

    uuid: str
    name: str
    url_resolved: str
    codec: str
    bitrate: int
    country: str
    favicon: str


def logo_url(uuid):
    return f"{LOGO_PATH}/{uuid}.png"


class LogoBase(LiveBase):
    def get_logo(self, browser, uuid, query=""):
        return browser.get(logo_url(uuid) + query)


class LogoRouteTest(LogoBase):
    def test_logo_png_default_icon_and_transient_have_the_exact_headers_and_cache_lifetimes(self):
        self.seed_live()
        self.radio.directory.logos = {B: LogoResult(PNG_1PX, False), C: LogoResult(None, True)}
        browser = self.logged_in()
        default = DEFAULT_ICON.read_bytes()

        def check(case):
            uuid, body, max_age = case
            response = self.get_logo(browser, uuid)
            assert response.code == 200 and response.body == body, uuid
            for name, value in EXPECTED.items():
                assert response.headers.get(name) == value, f"{uuid}: {name}={response.headers.get(name)!r}"
            assert response.headers.get("Cache-Control") == f"private, max-age={max_age}", uuid
            assert response.headers.get("Content-Length") == str(len(body)), uuid
            assert not response.headers.get_list("Set-Cookie"), uuid  # a sessão é só lida

        Each.run([(B, PNG_1PX, 86400), (A, default, 300), (C, default, 60)], check)
        assert self.radio.directory.logo_calls == [B, A, C]

    def test_bad_bytes_or_a_failing_directory_fall_back_to_the_default_icon(self):
        self.seed_live()
        browser = self.logged_in()
        self.radio.directory.logos = {B: LogoResult(b"<svg/>", False)}
        bad = self.get_logo(browser, B)
        assert bad.code == 200 and bad.body == DEFAULT_ICON.read_bytes()

        async def boom(uuid):
            raise RuntimeError("secret.example")

        self.radio.directory.logo_png = boom
        failed = self.get_logo(browser, B)
        assert failed.code == 200 and failed.body == DEFAULT_ICON.read_bytes()
        assert failed.headers.get("Cache-Control") == "private, max-age=300"
        assert "secret.example" not in self.all_log_text() and "RuntimeError" in self.all_log_text()

    def test_only_a_canonical_uuid_in_the_path_is_accepted_and_query_parameters_are_ignored(self):
        self.seed_live()
        self.radio.directory.logos = {B: LogoResult(PNG_1PX, False)}
        browser = self.logged_in()
        for raw in (B.upper(), B[:-1], "zzz", "../" + B, B + "%00", B.replace("-", "")):
            response = browser.get(f"{LOGO_PATH}/{raw}.png")
            assert response.code == 404, raw
        with_query = self.get_logo(browser, B, "?url=https://evil.example/x.png&uuid=" + C)
        assert with_query.code == 200 and with_query.body == PNG_1PX
        assert self.radio.directory.logo_calls == [B]

    def test_anonymous_requests_are_refused_without_redirect_or_cookie(self):
        self.seed_live()
        self.radio.directory.logos = {B: LogoResult(PNG_1PX, False)}
        anonymous = self.browser()
        response = self.get_logo(anonymous, B)
        assert response.code == 404 and "Location" not in response.headers
        assert not response.headers.get_list("Set-Cookie") and self.radio.directory.logo_calls == []


class LogoPageTest(LogoBase):
    def test_pages_and_fragments_point_images_to_our_origin_and_never_leak_the_favicon_url(self):
        radio = self.seed_live(reserve=B)
        self.radio.directory.stations[A] = StationWithLogo(A, "Rádio Alfa", SECRET, "MP3", 128, "Brasil", FAVICON)
        self.radio.directory.results = [StationWithLogo(C, "Gama Rock", SECRET, "MP3", 128, "Brasil", FAVICON)]
        browser = self.logged_in()
        page = self.page(browser)
        results = self.search(browser, radio, "fm").body.decode()
        for body in (page, results):
            assert "logo.example" not in body and FAVICON not in body
            assert not re.search(r'data-[a-z-]+="[^"]*(logo|https?:)', body)  # nem em atributos data-*
        imgs = re.findall(r'<img class="estacao-logo" src="([^"]+)"[^>]*>', page)
        assert imgs == [logo_url(A), logo_url(B)]  # principal e reserva, montados pelo servidor
        tag = re.search(r'<img class="estacao-logo"[^>]*>', results).group(0)
        assert f'src="{logo_url(C)}"' in tag and 'referrerpolicy="no-referrer"' in tag
        assert 'width="40" height="40"' in tag and 'loading="lazy"' in tag and "onerror" not in tag
        assert 'src="' + logo_url(A) not in results  # a principal fica fora da lista

    def test_station_without_a_known_name_still_gets_the_server_side_logo_src(self):
        self.seed_live()
        self.radio.directory.stations.pop(A)
        page = self.page(self.logged_in())
        assert logo_url(A) in page and "não encontrada no diretório" in page
