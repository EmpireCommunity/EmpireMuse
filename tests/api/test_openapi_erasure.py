"""Contrato das rotas de apagar dados (B5, ``docs/api/openapi.yaml``): as respostas reais validam contra o arquivo e todo par
rota+status documentado é observado."""

from unittest.mock import patch

import pytest

from empiremuse.api.handlers_erasure import ErasureHandler
from empiremuse.rules.erasure import ErasureReport, ErasureStatus
from empiremuse.utils.erasure import ErasureBusy
from tests.api.support_data import USER
from tests.api.test_erasure_routes import ERASE, PREPARE, WORD, ErasureCase
from tests.api.test_openapi_contract import SpecChecker, documented, load_spec

pytestmark = pytest.mark.critical

BASE = "/api/v1"
FAILURE = RuntimeError("falha inesperada")
OK = ErasureReport(ErasureStatus.OK, {}, ())


class OpenApiErasureContractTest(ErasureCase):
    spec = load_spec()
    seen: set[tuple[str, str, str]] = set()
    ran: set[str] = set()

    def setUp(self):
        super().setUp()
        self.ran.add(self._testMethodName)

    def expect(self, path, response, status, code=None):
        assert response.code == status, response.body
        self.seen.add(SpecChecker(self.spec).check("POST", BASE + path, response))

    def cover_common(self, path, body, *, media_type=True):
        self.expect(path, self.api("POST", path, body=body), 401)
        if media_type:
            plain = self.call("POST", path, None, raw_body="x", headers={"Content-Type": "text/plain"})
            self.expect(path, plain, 415)
        with patch.object(ErasureHandler, "require_recent_login", side_effect=FAILURE):
            self.expect(path, self.call("POST", path, body), 500)
        service, self.pool.erasure = self.pool.erasure, None
        self.expect(path, self.call("POST", path, body), 503)
        self.pool.erasure = service
        while self.ctx.limits.user_write.allow(USER):
            pass
        self.expect(path, self.call("POST", path, body), 429)
        self.advance(61)

    def test_prepare(self):
        self.expect(PREPARE, self.call("POST", PREPARE, {}), 200)
        self.advance(1801)  # login velho: 401 ``reauth_required``, também documentado
        self.expect(PREPARE, self.call("POST", PREPARE, {}), 401)
        self.token = self.new_session(USER)
        self.cover_common(PREPARE, {}, media_type=False)  # o prepare não lê o corpo

    def test_erasure(self):
        self.use_stub(OK)
        token = self.prepare()
        self.expect(ERASE, self.erase(token, "errada"), 400)
        self.expect(ERASE, self.erase(token), 200)
        self.token = self.new_session(USER)
        self.use_stub(ErasureBusy())
        self.expect(ERASE, self.erase(self.prepare()), 200)  # ``BUSY`` também é 200
        self.cover_common(ERASE, {"confirm_token": "x", "typed": WORD})

    def test_zz_todo_status_documentado_foi_exercitado(self):
        casos = {name for name in dir(self) if name.startswith("test_") and not name.startswith("test_zz")}
        if not casos <= self.ran:
            pytest.skip("só vale com todos os casos da classe rodando (o ``-k`` deixa casos de fora)")

        mine = {item for item in documented(self.spec) if "/account/erasure" in item[1]}

        assert mine and mine <= self.seen, f"documentados e nunca observados: {sorted(mine - self.seen)}"
