"""S-32 — o ``ytoauth`` nunca põe o refresh token (nem os dados da conta) numa mensagem de canal de servidor.

Opção A do contrato: a confirmação da conta (e-mail, nome, avatar e os botões Sim/Não) e o token vão só por DM ao dono que invocou, as duas DMs
com ``delete_after=600`` e o prazo dito em uma linha; o canal só manda olhar a DM. Se a DM de confirmação falhar (``Forbidden``), o fluxo aborta
antes de tocar no ``application.yml``, no nó LOCAL e no MongoDB. O token entra no redator ANTES de qualquer envio feito depois de obtido e não
aparece em registro de log. Tudo com fakes; os valores de token são montados em tempo de execução e não imitam um token real.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import disnake
import pytest

from empiremuse.cogs import ll_yt_oauth
from empiremuse.utils.music.errors import GenericError

pytestmark = pytest.mark.critical

REFRESH = "".join(["refresh-", "dm-teste-", "Zk4p7"])
ACCESS = "".join(["access-", "dm-teste-", "Wq9m2"])
EMAIL = "conta.descartavel@exemplo.invalido"
NOME_DA_CONTA = "Conta Descartavel"
AVATAR = "https://ok.exemplo/foto.png"
DADOS_DA_CONTA = (EMAIL, NOME_DA_CONTA, AVATAR)
YML_ORIGINAL = "plugins:\n  youtube:\n    oauth:\n      enabled: false\n"


class ViewFalsa:
    def __init__(self, **kwargs):
        self.interaction = None
        self.exception_txt = None
        self.data = {"refresh_token": REFRESH, "access_token": ACCESS}

    async def wait(self):
        return None


def registrar(eventos: list[str], nome: str, retorno=None, falha_em: set[int] | None = None, erro=None) -> AsyncMock:
    """``AsyncMock`` que anota ``nome`` (a ORDEM dos envios); levanta ``erro`` nas chamadas de número ``falha_em`` (1-based)."""
    contador = {"n": 0}

    async def corpo(*args, **kwargs):
        contador["n"] += 1
        eventos.append(nome)
        if erro is not None and (falha_em is None or contador["n"] in falha_em):
            raise erro
        return retorno

    return AsyncMock(side_effect=corpo)


class ConfirmacaoFalsa:
    """``AskView`` já respondida com "sim" (o clique acontece na DM)."""

    eventos: list[str] = []
    selected = True

    def __init__(self, **kwargs):
        self.selected = type(self).selected
        self.interaction_resp = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), edit_message=registrar(self.eventos, "dm:interacao")),
            edit_original_message=registrar(self.eventos, "dm:interacao"),
            followup=SimpleNamespace(send=registrar(self.eventos, "dm:interacao")),
            send=registrar(self.eventos, "dm:interacao"),
        )

    async def wait(self):
        return None


class RespostaHttp:
    status = 200

    async def json(self):
        return {"name": NOME_DA_CONTA, "email": EMAIL, "picture": AVATAR}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class SessaoFalsa:
    def __init__(self):
        self.post = AsyncMock()

    def get(self, *args, **kwargs):
        return RespostaHttp()


def mostrar(chamada) -> str:
    """Texto de uma chamada, com o conteúdo dos embeds (o ``repr`` de um ``Embed`` não mostra a descrição)."""
    partes = []
    for valor in [*chamada.args, *chamada.kwargs.values()]:
        embeds = valor if isinstance(valor, (list, tuple)) else [valor]
        partes += [str(e.to_dict()) if isinstance(e, disnake.Embed) else str(e) for e in embeds]
    return " ".join(partes)


class Cenario:
    def __init__(self, monkeypatch, tmp_path, *, dm_falha_em: set[int] | None = None, com_efeitos: bool = False):
        self.eventos: list[str] = []
        ConfirmacaoFalsa.eventos = self.eventos
        ConfirmacaoFalsa.selected = True
        monkeypatch.setattr(ll_yt_oauth, "YtOauthView", ViewFalsa)
        monkeypatch.setattr(ll_yt_oauth, "AskView", ConfirmacaoFalsa)
        monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
        monkeypatch.setattr(ll_yt_oauth, "register_secret", lambda valor: self.eventos.append(f"registro:{valor}"))
        self.yml = tmp_path / "application.yml"
        self.mongo = SimpleNamespace(update_data=AsyncMock())
        self.sessao = SessaoFalsa()
        self.no_local = SimpleNamespace(
            host="127.0.0.1",
            port=8090,
            identifier="LOCAL",
            info={"plugins": ["youtube-plugin"]},
            rest_uri="http://127.0.0.1:8090",
            _websocket=SimpleNamespace(headers={}),
        )
        if com_efeitos:
            self.yml.write_text(YML_ORIGINAL, encoding="utf-8")
        self.cog = ll_yt_oauth.YtOauthLL.__new__(ll_yt_oauth.YtOauthLL)
        self.cog.bot = SimpleNamespace(
            get_color=lambda *a: 0,
            session=self.sessao,
            pool=SimpleNamespace(mongo_database=self.mongo if com_efeitos else None),
            music=SimpleNamespace(nodes={"LOCAL": self.no_local} if com_efeitos else {}),
        )
        self.mensagem = SimpleNamespace(edit=registrar(self.eventos, "canal:edicao"))
        erro_dm = disnake.Forbidden(SimpleNamespace(status=403, reason="x"), "Cannot send messages to this user")
        self.dm = registrar(self.eventos, "dm", erro=erro_dm if dm_falha_em else None, falha_em=dm_falha_em)
        self.ctx = SimpleNamespace(
            guild=SimpleNamespace(me=None),
            author=SimpleNamespace(id=1, send=self.dm, mention="<@1>"),
            send=registrar(self.eventos, "canal:envio", retorno=self.mensagem),
            channel=SimpleNamespace(send=registrar(self.eventos, "canal:envio")),
        )
        self.erro: BaseException | None = None

    def rodar(self) -> None:
        try:
            asyncio.run(self.cog.oauth_command(self.ctx))
        except GenericError as erro:
            self.erro = erro

    def canal(self) -> str:
        """Tudo o que o CANAL pode ter recebido: envios, edições da mensagem e o texto de um erro genérico (as interações são da DM)."""
        chamadas = [self.ctx.send, self.mensagem.edit, self.ctx.channel.send]
        partes = [mostrar(c) for chamada in chamadas for c in chamada.call_args_list]
        if self.erro:
            partes += [str(self.erro), repr(getattr(self.erro, "args", ""))]
        return "\n".join(partes)

    def dm_texto(self) -> str:
        return "\n".join(mostrar(c) for c in self.dm.call_args_list)


@pytest.fixture
def cenario(monkeypatch, tmp_path) -> Cenario:
    return Cenario(monkeypatch, tmp_path)


@pytest.fixture
def cenario_dm_fechada(monkeypatch, tmp_path) -> Cenario:
    """A primeira DM (a de confirmação) falha."""
    return Cenario(monkeypatch, tmp_path, dm_falha_em={1, 2})


class TestConfirmacaoPorDm:
    """Opção A: a confirmação da conta é a primeira DM, com os botões; o canal não vê nada da conta."""

    def test_the_confirmation_dm_has_no_token(self, cenario):
        cenario.rodar()

        assert REFRESH not in mostrar(cenario.dm.call_args_list[0])


class TestNadaDoCanal:
    """Y1: NENHUMA mensagem do canal (confirmação ou final) tem token, e-mail, nome ou URL do avatar."""

    @pytest.mark.parametrize(
        "dado", [REFRESH, ACCESS, *DADOS_DA_CONTA], ids=["refresh", "access", "email", "nome", "avatar"]
    )
    def test_no_channel_message_has_it(self, cenario, dado):
        cenario.rodar()

        assert dado not in cenario.canal()


class TestRegistroAntesDoEnvio:
    """Y4: o token entra no redator ANTES de qualquer envio feito depois de obtido (o convite inicial do ``ctx.send`` é anterior ao token)."""

    def test_the_token_is_registered_before_the_first_dm(self, cenario):
        cenario.rodar()

        assert cenario.eventos.index(f"registro:{REFRESH}") < cenario.eventos.index("dm")


class TestDmDoTokenFechada:
    """A confirmação passou e a DM com o token falha: o canal continua sem o token e sem os dados da conta."""

    def test_the_channel_never_gets_the_token_or_the_account_data(self, monkeypatch, tmp_path):
        c = Cenario(monkeypatch, tmp_path, dm_falha_em={2})

        c.rodar()

        canal = c.canal()
        assert not any(dado in canal for dado in (REFRESH, *DADOS_DA_CONTA))


class TestTokenForaDosLogs:
    def test_the_token_never_appears_in_a_log_record(self, cenario, caplog):
        with caplog.at_level(logging.DEBUG):
            cenario.rodar()

        assert all(REFRESH not in r.getMessage() + (r.exc_text or "") for r in caplog.records)
