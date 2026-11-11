"""B6: /painel antigo é só um redirecionamento fixo no modo externo, sem transferir OAuth ou formulários."""

import pytest
import tornado.web
from tornado.testing import AsyncHTTPTestCase

from empiremuse.panel import make_router
from empiremuse.panel.redirect import build_redirect_app

pytestmark = pytest.mark.critical
DESTINATION = "https://muse.empirecommunity.org/panel"


class ExternalRedirectTest(AsyncHTTPTestCase):
    def get_app(self):
        return make_router(tornado.web.Application([]), build_redirect_app(DESTINATION))

    def test_get_e_head_redirecionam_302_sem_refletir_caminho_query_host_ou_cookies(self):
        for path in (
            "/painel",
            "/painel/",
            "/painel/callback?code=segredo&state=outro",
            "/painel/login?next=https://evil.example",
            "/painel/%2f%2fevil.example?x=%0d%0a",
        ):
            for method in ("GET", "HEAD"):
                response = self.fetch(
                    path,
                    method=method,
                    follow_redirects=False,
                    headers={"Host": "evil.example", "Cookie": "qualquer=valor"},
                )
                assert response.code == 302
                assert response.headers["Location"] == DESTINATION
                assert response.headers["Cache-Control"] == "no-store"
                assert response.headers["Referrer-Policy"] == "no-referrer"
                assert "Set-Cookie" not in response.headers
                assert b"segredo" not in response.body and b"evil.example" not in response.body

    def test_post_e_outros_metodos_nao_redirecionam_nem_executam_o_generico(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            response = self.fetch(
                "/painel/callback?code=segredo",
                method=method,
                body="code=corpo" if method in ("POST", "PUT", "PATCH") else None,
                follow_redirects=False,
            )
            assert response.code == 404
            assert "Location" not in response.headers and "Set-Cookie" not in response.headers
            assert b"segredo" not in response.body and b"corpo" not in response.body

    def test_prefixos_parecidos_nao_sao_redirecionados(self):
        for path in ("/painelx", "/outro/painel", "/PAINEL"):
            response = self.fetch(path, follow_redirects=False)
            assert response.code == 404 and "Location" not in response.headers

    def test_log_nao_contem_query_ou_erro_do_oauth(self):
        import logging
        from unittest.mock import patch

        with patch.object(logging.getLogger("empiremuse.panel.redirect"), "log") as logged:
            self.fetch("/painel/callback?code=segredo&state=outro", follow_redirects=False)
        assert logged.called
        assert "segredo" not in str(logged.call_args_list) and "outro" not in str(logged.call_args_list)
