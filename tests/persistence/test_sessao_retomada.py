"""F3-6b, etapa 5 (RED, commit 3) — o endurecimento do ``PlayerSession.resume_player``.

Condição 1 do 5.1: depois de CADA ``get_channel``/``fetch_channel`` (``voice_channel``, ``last_voice_channel_id``,
``text_channel_id`` e o canal do ``player_controller``) o canal tem de ser do MESMO servidor da sessão (``canal.guild.id ==
guild.id``; canal sem ``guild`` é recusado). Falhou: a sessão não conecta nem envia mensagem em outro servidor; o log leva o
``guild_id`` e o tipo da falha, NUNCA o id do canal alheio. A mensagem (``message_id``) só é buscada no canal já validado.

Também: a sessão decodificada (sem ``voice_state`` nem ``extra_info``, ``dj`` e log em lista, defaults nos campos ausentes) retoma
direito; o ``time`` antigo continua apagando (as duas contas de 172 800 s ficam); o ramo ``Thread`` de ``text_channel_id`` saiu.
"""

import ast
from datetime import timedelta
from types import SimpleNamespace

import disnake
import pytest

from empiremuse.cogs import player_resume
from tests.support.formatos_reais import CANAL_DE_TEXTO, CANAL_DE_VOZ, INSTANTE, MENSAGEM, SERVIDOR, cog_de_sessoes
from tests.support.sessoes import codificar, session
from tests.support.formato_sessao import gravar, jogador_completo
from tests.persistence.test_sessao_retomada_sem_voice_state import JogadorRetomado, No
from tests.support.raiz import RAIZ

OUTRO_SERVIDOR = 1100000000000000555
CANAL_ALHEIO = 1100000000000000777


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setattr(disnake.utils, "utcnow", lambda: INSTANTE)
    monkeypatch.setattr(player_resume, "can_send_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(player_resume, "can_connect", lambda *args, **kwargs: None)


class CanalEspiao:
    """Canal de mentira que anota tudo o que o ``resume_player`` faz com ele (enviar, buscar mensagem, ler o histórico)."""

    def __init__(self, canal_id, guild=..., *, conectar=True):
        self.id, self.name, self.mention = canal_id, f"canal-{canal_id}", f"<#{canal_id}>"
        if guild is not ...:
            self.guild = guild  # ``...`` = canal SEM o atributo ``guild`` (como um DM)
        self.conectar, self.chamadas = conectar, []
        membro = SimpleNamespace(bot=False, voice=SimpleNamespace(deaf=False, self_deaf=False))
        self.members = [membro]

    def permissions_for(self, quem):
        return SimpleNamespace(
            connect=self.conectar,
            read_message_history=True,
            send_messages=True,
            read_messages=True,
            manage_threads=True,
        )

    async def send(self, *args, **kwargs):
        self.chamadas.append("send")

    async def fetch_message(self, message_id):
        self.chamadas.append(("fetch_message", message_id))
        return SimpleNamespace(id=message_id)

    def history(self, **kwargs):
        self.chamadas.append("history")

        async def vazio():
            return
            yield  # pragma: no cover - gerador assíncrono vazio

        return vazio()


def montar(*, canais, controller=None, buscaveis=None, servidor_existe=True):
    """Cog real + bot mínimo; ``canais`` responde ao ``get_channel`` e ``buscaveis`` ao ``fetch_channel``."""
    cog = cog_de_sessoes()
    servidor = SimpleNamespace(id=SERVIDOR, name="servidor", me=SimpleNamespace(voice=object(), id=1))
    no, criados, jogadores = No(4), [], {}

    def get_player(**kwargs):
        jogador = JogadorRetomado(no, **kwargs)
        criados.append(jogador)
        jogadores[kwargs["guild_id"]] = jogador
        return jogador

    async def get_data(*args, **kwargs):
        return {"player_controller": controller or {"channel": None, "message_id": None}}

    async def fetch_channel(canal_id):
        try:
            return (buscaveis or {})[canal_id]
        except KeyError:
            raise disnake.NotFound(SimpleNamespace(status=404, reason="n/a"), "não existe") from None

    cog.bot.user = SimpleNamespace(id=1)
    cog.bot.get_channel = canais.get
    cog.bot.fetch_channel = fetch_channel
    cog.bot.get_guild = lambda guild_id: servidor if servidor_existe else None

    async def get_global_data(*args, **kwargs):
        return {}

    cog.bot.get_data = get_data
    cog.bot.get_global_data = get_global_data
    cog.bot.get_color = lambda *args: 0
    cog.bot.music = SimpleNamespace(players=jogadores, get_best_node=lambda: no, get_player=get_player)
    cog.bot.players_resumed = {}
    cog.apagados = []

    async def delete_data(player):
        cog.apagados.append(player)

    cog.delete_data = delete_data
    return cog, servidor, criados


def proprio(canal_id, **kwargs):
    return CanalEspiao(canal_id, SimpleNamespace(id=SERVIDOR), **kwargs)


def alheio(canal_id=CANAL_ALHEIO, **kwargs):
    return CanalEspiao(canal_id, SimpleNamespace(id=OUTRO_SERVIDOR), **kwargs)


async def retomar(cog, dados):
    await cog.resume_player(dados, hints=[])


async def sessao() -> dict:
    return await gravar(cog_de_sessoes(), jogador_completo())


def sem_rastro_do_canal_alheio(canal: CanalEspiao, criados) -> bool:
    return canal.chamadas == [] and not any(CANAL_ALHEIO in j.conexoes for j in criados)


# ============================================================================ condição 1: servidor == canal


class TestCanalDeOutroServidor:
    @pytest.mark.asyncio
    async def test_ultimo_canal_de_voz_de_outro_servidor_nao_e_usado_como_reserva(self):
        estranho = alheio()
        dados = await sessao()
        dados["last_voice_channel_id"] = CANAL_ALHEIO
        canais = {
            CANAL_DE_VOZ: proprio(CANAL_DE_VOZ, conectar=False),  # sem permissão: força o ramo do canal reserva
            CANAL_ALHEIO: estranho,
            CANAL_DE_TEXTO: proprio(CANAL_DE_TEXTO),
        }
        cog, _, criados = montar(canais=canais)
        await retomar(cog, dados)
        assert sem_rastro_do_canal_alheio(estranho, criados) and criados == []

    @pytest.mark.asyncio
    async def test_canal_do_player_controller_de_outro_servidor_e_ignorado(self):
        estranho = alheio()
        dados = await sessao()
        voz, texto = proprio(CANAL_DE_VOZ), proprio(CANAL_DE_TEXTO)
        cog, _, criados = montar(
            canais={CANAL_DE_VOZ: voz, CANAL_DE_TEXTO: texto, CANAL_ALHEIO: estranho},
            controller={"channel": str(CANAL_ALHEIO), "message_id": str(MENSAGEM)},
        )
        await retomar(cog, dados)
        assert len(criados) == 1 and sem_rastro_do_canal_alheio(estranho, criados)
        assert criados[0].kwargs["channel"] is not estranho

    @pytest.mark.asyncio
    async def test_texto_buscado_por_fetch_channel_de_outro_servidor_e_recusado_e_do_proprio_vale(self):
        """Sem o ramo ``Thread`` de ``text_channel_id``: o id é inteiro e a busca passa pela mesma conferência de servidor."""
        estranho = alheio()
        dados = await sessao()
        dados["text_channel_id"] = CANAL_ALHEIO
        cog, _, criados = montar(canais={CANAL_DE_VOZ: proprio(CANAL_DE_VOZ)}, buscaveis={CANAL_ALHEIO: estranho})
        await retomar(cog, dados)
        assert len(criados) == 1 and sem_rastro_do_canal_alheio(estranho, criados)

        meu = proprio(CANAL_DE_TEXTO)
        dados = await sessao()  # o resume_player altera o dicionário que recebe
        dados["text_channel_id"] = CANAL_DE_TEXTO
        cog, _, criados = montar(canais={CANAL_DE_VOZ: proprio(CANAL_DE_VOZ)}, buscaveis={CANAL_DE_TEXTO: meu})
        await retomar(cog, dados)
        assert criados[0].kwargs["channel"] is meu and ("fetch_message", MENSAGEM) in meu.chamadas


# ============================================================================ a sessão decodificada retoma direito


# ============================================================================ time: as contas de 48 h continuam (caracterização)


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize(
    ("marks", "skin"), [({}, None), ({"skin_chosen": True, "skin_static_chosen": True}, "default")]
)
async def test_a_resumed_session_reads_the_choice_mark_and_never_the_pool_default(marks, skin):
    """Sessão de antes das marcas: o ``default`` vira ``None`` (o padrão novo, que o próprio player aplica); com a marca, fica."""
    dados = {k: v for k, v in (await sessao()).items() if k not in ("skin_chosen", "skin_static_chosen")} | marks
    cog, _, criados = montar(canais={CANAL_DE_VOZ: proprio(CANAL_DE_VOZ), CANAL_DE_TEXTO: proprio(CANAL_DE_TEXTO)})
    if (pool := getattr(cog.bot, "pool", None)) is not None:
        for name in ("default_skin", "default_static_skin"):  # sem o padrão no pool: a retomada não pode lê-lo
            vars(pool).pop(name, None)

    await retomar(cog, dados)

    assert len(criados) == 1 and criados[0].kwargs["skin"] == skin and criados[0].kwargs["skin_static"] == skin


class TestIdadeDaSessao:
    @pytest.mark.asyncio
    async def test_servidor_inexistente_e_sessao_com_mais_de_48_horas_e_apagada(self):
        dados = session.decode_session(codificar(time=INSTANTE - timedelta(hours=49)))
        cog, _, criados = montar(canais={}, servidor_existe=False)
        await retomar(cog, dados)
        assert cog.apagados == [SERVIDOR] and criados == []

    @pytest.mark.asyncio
    async def test_canal_de_voz_inexistente_e_sessao_com_mais_de_48_horas_e_apagada(self):
        dados = session.decode_session(codificar(time=INSTANTE - timedelta(hours=49)))
        cog, _, criados = montar(canais={CANAL_DE_TEXTO: proprio(CANAL_DE_TEXTO)})
        await retomar(cog, dados)
        assert cog.apagados == [SERVIDOR] and criados == []


# ============================================================================ por ast


def funcao(nome: str, no_arquivo="cogs/player_resume.py") -> ast.AsyncFunctionDef:
    arvore = ast.parse((RAIZ / "empiremuse" / no_arquivo).read_text(encoding="utf-8"))
    for classe in ast.walk(arvore):
        if isinstance(classe, ast.ClassDef) and classe.name == "PlayerSession":
            for no in classe.body:
                if isinstance(no, (ast.AsyncFunctionDef, ast.FunctionDef)) and no.name == nome:
                    return no
    raise AssertionError(nome)
