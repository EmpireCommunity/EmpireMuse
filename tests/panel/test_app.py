"""Aplicação do painel: rotas, 404 genérico, cabeçalhos, cookie _xsrf e arquivos estáticos."""

import json

import tornado.web
from tornado.testing import AsyncHTTPTestCase

from tests.panel.helpers import (
    EXPECTED_CSP,
    ROOT,
    fake_pool,
    https_config,
    load_settings,
    local_config,
    require,
)
import pytest

pytestmark = pytest.mark.critical


class _Main(tornado.web.RequestHandler):
    def get(self, *_):
        self.write("main-app")


class PanelTestBase(AsyncHTTPTestCase):
    config_factory = staticmethod(local_config)

    def setUp(self):
        require("empiremuse.panel")
        super().setUp()

    def get_app(self):
        panel = require("empiremuse.panel")
        self.settings = load_settings(self.config_factory())
        self.panel_app = panel.build_panel_app(fake_pool(), self.settings)
        return panel.make_router(tornado.web.Application([(r"/", _Main)]), self.panel_app)

    def get_page(self, path="/painel/"):
        return self.fetch(path, follow_redirects=False)

    @staticmethod
    def assert_all(cases, check):
        """Executa `check(case)` para todos os casos e reporta TODAS as falhas (AsyncHTTPTestCase é unittest:
        parametrize do pytest não injeta argumentos em métodos de TestCase)."""
        failures = []
        for case in cases:
            try:
                check(case)
            except AssertionError as exc:
                failures.append(f"{case!r}: {exc}")
        assert not failures, f"{len(failures)} caso(s) falharam:\n  " + "\n  ".join(failures)


class PanelRoutesTest(PanelTestBase):
    def test_painel_without_slash_redirects_permanently(self):
        response = self.get_page("/painel")
        assert response.code == 301
        assert response.headers["Location"].endswith("/painel/")

    @pytest.mark.smoke
    def test_health_returns_ok_json(self):
        response = self.get_page("/painel/saude")
        assert response.code == 200
        assert response.headers["Content-Type"].startswith("application/json")
        assert json.loads(response.body) == {"ok": True}

    @pytest.mark.smoke
    def test_unknown_route_returns_generic_html_404(self):
        response = self.get_page("/painel/nao-existe")
        body = response.body.decode()
        assert response.code == 404
        assert response.headers["Content-Type"].startswith("text/html")
        assert "404" in body
        for leak in ("Traceback", "tornado", 'File "', ROOT.as_posix()):
            assert leak not in body, f"a página de erro vaza detalhes internos: {leak!r}"

    def test_vendored_static_files_are_served(self):
        def check(case):
            path, kind = case
            response = self.get_page(path)
            assert response.code == 200, f"status {response.code}"
            assert kind in response.headers["Content-Type"], response.headers["Content-Type"]

        self.assert_all(
            [("/painel/static/vendor/htmx.min.js", "javascript"), ("/painel/static/vendor/pico.min.css", "css")], check
        )


class PanelSecurityHeadersTest(PanelTestBase):
    def test_csp_has_no_unsafe_directives(self):
        csp = self.get_page().headers["Content-Security-Policy"]
        assert csp == EXPECTED_CSP
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp

    def test_hsts_is_absent_without_https(self):
        assert "Strict-Transport-Security" not in self.get_page().headers


class PanelHttpsTest(PanelTestBase):
    config_factory = staticmethod(https_config)

    def test_hsts_is_sent_when_base_url_is_https(self):
        assert "max-age=31536000" in self.get_page().headers.get("Strict-Transport-Security", "")


def _xsrf_cookie(response) -> str:
    cookies = [c for c in response.headers.get_list("Set-Cookie") if c.startswith("_xsrf=")]
    assert cookies, "a página inicial deve definir o cookie _xsrf"
    return cookies[0]


class PanelXsrfTest(PanelTestBase):
    def test_xsrf_cookie_flags_for_local_development(self):
        flags = [part.strip().lower() for part in _xsrf_cookie(self.get_page()).split(";")]
        assert "httponly" in flags
        assert "samesite=lax" in flags
        assert "secure" not in flags, "PANEL_INSECURE_COOKIES=true em localhost não deve marcar Secure"

    def test_post_without_token_is_forbidden(self):
        def check(path):
            code = self.fetch(path, method="POST", body="", follow_redirects=False).code
            assert code == 403, f"status {code}"

        self.assert_all(["/painel/", "/painel/saude"], check)


class PanelStaticTest(PanelTestBase):
    STATIC_BLOCKED_PATHS = [
        "/painel/static/../../empiremuse/config_loader.py",
        "/painel/static/%2e%2e/%2e%2e/LICENSE",
        "/painel/static/..%2f..%2fempiremuse%2fconfig_loader.py",
    ]

    def test_static_route_blocks_path_traversal(self):
        def check(path):
            response = self.get_page(path)
            assert response.code in (403, 404), f"status {response.code}"
            assert b"GNU GENERAL PUBLIC LICENSE" not in response.body
            assert b"DEFAULT_CONFIG" not in response.body

        self.assert_all(self.STATIC_BLOCKED_PATHS, check)
