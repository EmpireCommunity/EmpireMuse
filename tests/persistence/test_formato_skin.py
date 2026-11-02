"""As skins personalizadas do servidor: dublês e leitores (``load_skin``, ``change_skin`` e ``create_player``).

Uma skin é o dicionário do editor (``message_data``), guardado no documento global do servidor em ``custom_skins`` (modo normal)
e ``custom_skins_static`` (song-request). Os leitores entregam ao player o dicionário validado. O texto do formato antigo
(``base64`` de ``pickle``, ``texto_legado``) não é mais lido: fica no documento e a skin padrão assume (ver
``tests/persistence/test_skin_leitura.py``).
"""

import base64
import copy
import pickle
from types import SimpleNamespace

import pytest

from empiremuse.cogs import music_settings
from empiremuse.cogs.music import Music
from empiremuse.cogs.music_settings import MusicSettings
from empiremuse.i18n import get_i18n
from empiremuse.utils.music.interactions import skin_editor
from empiremuse.utils.music.interactions import SkinEditorMenu
from empiremuse.utils.music.interactions.skin_editor_data import base_skin_for
from tests.support.formatos_reais import SERVIDOR


def skin_padrao() -> dict:
    """A skin base do editor no idioma dos testes (pt-BR), como ``new_skin``/``load_skin`` a montam."""
    return base_skin_for(get_i18n().for_locale("pt-BR"))


def skin_realista(**mudancas) -> dict:
    """A skin padrão do editor mais um embed montado pelo editor (campos, autor, rodapé, imagem, miniatura, cor)."""
    skin = skin_padrao()
    skin["content"] = "Tocando agora: {track.title}"
    skin["controller_enabled"] = False
    skin["embeds"].append(
        {
            "title": "Título do embed",
            "description": "Descrição com **negrito** e `código`",
            "color": "ff0000",
            "fields": [
                {"name": "Campo 1", "value": "Valor 1", "inline": True},
                {"name": "Campo 2", "value": "Valor 2"},
            ],
            "author": {"name": "Autor", "icon_url": "https://exemplo.invalid/a.png"},
            "footer": {"text": "Rodapé", "icon_url": "https://exemplo.invalid/r.png"},
            "image": {"url": "{track.thumb}"},
            "thumbnail": {"url": "https://exemplo.invalid/t.png"},
        }
    )
    skin.update(mudancas)
    return skin


def texto_legado(skin: dict, protocolo: int = 4) -> str:
    """O que o gravador antigo punha no documento: ``base64`` de ``pickle`` (sem ``zlib``); o bot não o lê mais."""
    return base64.b64encode(pickle.dumps(skin, protocol=protocolo)).decode("utf-8")


class Jogador:
    """Só o que os leitores de skin pedem ao ``LavalinkPlayer`` depois de converter as skins."""

    def __init__(self, *, estatico=False, skin="default", skin_static="default"):
        self.static, self.skin, self.skin_static = estatico, skin, skin_static
        self.custom_skin_data, self.custom_skin_static_data = None, None
        self.controller_mode = False
        self.guild = SimpleNamespace(me=SimpleNamespace(voice=None))
        self.chamadas = []

    def setup_features(self):
        self.chamadas.append("setup_features")

    def setup_hints(self):
        self.chamadas.append("setup_hints")

    def process_hint(self):
        self.chamadas.append("process_hint")

    def set_command_log(self, **kwargs):
        self.chamadas.append("set_command_log")

    async def destroy_message(self):
        self.chamadas.append("destroy_message")

    async def invoke_np(self, **kwargs):
        self.chamadas.append("invoke_np")


class BotDeSkins:
    def __init__(self, global_data, jogadores=()):
        self.global_data = global_data  # o cache do banco: ``get_global_data`` devolve esta MESMA referência
        self.salvos = []  # uma cópia de cada documento gravado
        self.escritas = []  # ``(id_, db_name)`` de cada gravação
        self.banco = None  # o que foi persistido na última gravação (cópia): é o que sobrevive a um reinício
        self.music = SimpleNamespace(players={SERVIDOR: jogador for jogador in list(jogadores)[:1]})
        self.pool = SimpleNamespace(get_guild_bots=lambda guild_id: [self], guild_prefix_cache={}, i18n=get_i18n())
        self.config = {"DEFAULT_PREFIX": "!!", "IGNORE_SKINS": "", "IGNORE_STATIC_SKINS": ""}
        self.user = SimpleNamespace(
            id=1,
            display_avatar=SimpleNamespace(
                with_format=lambda formato: SimpleNamespace(url="https://exemplo.invalid/b.png")
            ),
        )

    async def get_global_data(self, id_, *, db_name):
        return self.global_data

    async def update_global_data(self, id_, data, *, db_name):
        self.escritas.append((id_, db_name))
        self.banco = copy.deepcopy(data)
        self.global_data = (
            data  # ``empiremuse/utils/db.py`` faz ``cache[chave] = data``: o cache passa a ser o objeto gravado
        )
        self.salvos.append(copy.deepcopy(data))

    def get_global_command_named(self, *args, **kwargs):
        return None

    def get_color(self, *args):
        return 0


def dados_globais(normais=None, estaticas=None):
    return {"custom_skins": dict(normais or {}), "custom_skins_static": dict(estaticas or {})}


# ============================================================================ o editor (gravador e leitor da skin escolhida)


class InteracaoDoEditor:
    author = SimpleNamespace(
        id=900
    )  # o editor guarda a última interação como ``ctx`` e o botão de salvar lê ``ctx.author``

    def __init__(self, **text_values):
        self.text_values = text_values
        self.custom_id = "skin_editor_save"
        self.guild_id = SERVIDOR
        self.enviados = []
        self.response = SimpleNamespace(defer=self._defer, is_done=lambda: True)

    async def _defer(self, **kwargs):
        return None

    async def send(self, content=None, **kwargs):
        self.enviados.append(content)

    async def edit_original_message(self, content=None, **kwargs):
        self.enviados.append(content)


class ContextoDoEditor(InteracaoDoEditor):
    author = SimpleNamespace(id=900)


class BotaoDeModo:
    """Substitui o ``SkinSettingsButton``: o dono escolhe o modo de imediato e sem passar pela tela."""

    modo = "custom_skins"

    def __init__(self, user, *args, **kwargs):
        self.mode, self.inter, self.controller_enabled = type(self).modo, None, True

    async def wait(self):
        return None


@pytest.fixture
def editor_sem_tela(monkeypatch):
    """Editor REAL (``SkinEditorMenu``) sem as partes de tela que não importam aqui (escolha de modo e reenvio)."""

    async def atualizar(self, inter):
        return None

    monkeypatch.setattr(skin_editor, "SkinSettingsButton", BotaoDeModo)
    monkeypatch.setattr(SkinEditorMenu, "update_message", atualizar)
    monkeypatch.setattr(SkinEditorMenu, "build_embeds", lambda self: {"embed": None})

    def montar(global_data, jogadores=()):
        bot = BotDeSkins(global_data, jogadores)
        contexto = ContextoDoEditor()
        servidor = SimpleNamespace(id=SERVIDOR, me=SimpleNamespace(color=SimpleNamespace(value=0)))
        return SkinEditorMenu(contexto, bot, servidor, global_data), bot, contexto

    return montar


# ============================================================================ os leitores


class TestLeitores:
    @pytest.mark.asyncio
    async def test_load_skin_aceita_o_dicionario_pronto(self, editor_sem_tela):
        skin = skin_realista()
        menu, _, _ = editor_sem_tela(dados_globais(normais={"Minha": skin}))
        await menu.load_skin(SimpleNamespace(values=["> cs: Minha"]))
        assert menu.message_data == skin

    @pytest.mark.asyncio
    async def test_load_skin_sem_prefixo_conhecido_comeca_da_skin_padrao(self, editor_sem_tela):
        menu, _, _ = editor_sem_tela(dados_globais())
        await menu.load_skin(SimpleNamespace(values=["default"]))
        assert menu.message_data == skin_padrao()

    @pytest.mark.asyncio
    async def test_change_skin_nao_mexe_no_player_que_ja_usa_a_skin_escolhida(self, monkeypatch):
        dados = {
            "custom_skins": {"N": skin_realista()},
            "custom_skins_static": {},
            "global_skin": False,
            "player_skin": "",
            "player_skin_static": "",
        }
        jogador = await chamar_change_skin(
            monkeypatch, dados, nova_skin="> custom_skin: N", skin_atual="> custom_skin: N"
        )
        assert jogador.custom_skin_data is None and jogador.chamadas == []


# ============================================================================ os dublês de create_player e change_skin


class Parou(Exception):
    def __init__(self, argumentos):
        self.argumentos = argumentos


async def chamar_create_player(global_data, escritas: list | None = None) -> dict:
    """Roda o ``Music.create_player`` REAL até o ``get_player`` e devolve o que ele recebeu.

    ``escritas`` (opcional) recebe ``(id_, cópia do documento, db_name)`` de cada ``update_global_data``.
    """

    def get_player(**kwargs):
        raise Parou(kwargs)

    guild_data = {
        "player_controller": {"skin": "default", "static_skin": "default", "channel": None, "message_id": None},
        "enable_restrict_mode": False,
        "autoplay": False,
        "default_player_volume": 100,
    }

    async def get_global_data(*args, **kwargs):
        return global_data

    async def update_global_data(id_=None, data=None, *, db_name=None, **kwargs):
        if escritas is not None:
            escritas.append((id_, copy.deepcopy(data), db_name))

    bot = SimpleNamespace(
        music=SimpleNamespace(players={}, get_player=get_player),
        pool=SimpleNamespace(check_skin=lambda skin: skin, check_static_skin=lambda skin: skin),
        get_global_data=get_global_data,
        update_global_data=update_global_data,
        default_prefix="!!",
    )
    cog = Music.__new__(Music)
    cog.bot, cog.extra_hints = bot, []
    inter = SimpleNamespace(
        guild_id=SERVIDOR, author=SimpleNamespace(id=900, voice=None), guild=SimpleNamespace(id=SERVIDOR)
    )
    guild = SimpleNamespace(id=SERVIDOR, me=SimpleNamespace())
    node = SimpleNamespace(identifier="no")
    with pytest.raises(Parou) as parada:
        await cog.create_player(inter, bot, guild, None, guild_data=guild_data, node=node)
    return parada.value.argumentos


async def chamar_change_skin(monkeypatch, global_data, *, nova_skin, skin_atual="default") -> Jogador:
    """Roda o ``MusicSettings.change_skin`` REAL com a escolha da tela já feita; devolve o player ativo.

    Tudo que o comando diz ao dono (texto e embeds, enviados ou editados) fica em ``jogador.mensagens``.
    """
    jogador = Jogador(skin=skin_atual)
    jogador.mensagens = mensagens = []

    async def registrar(conteudo=None, **kwargs):
        mensagens.append(str(conteudo or ""))
        embed = kwargs.get("embed")
        if embed is not None:
            mensagens.append(str(embed.description or ""))

    bot = BotDeSkins(global_data, [jogador])
    jogador.bot = bot
    bot.player_skins = {"default": SimpleNamespace(preview="https://exemplo.invalid/p")}
    bot.player_static_skins = {"default": SimpleNamespace(preview="https://exemplo.invalid/p")}
    bot.pool.default_skin = bot.pool.default_static_skin = "default"
    guild_data = {"player_controller": {"skin": skin_atual, "static_skin": "default"}}

    async def get_data(*args, **kwargs):
        return guild_data

    async def update_data(*args, **kwargs):
        return None

    async def patch(*args, **kwargs):
        return {}

    bot.get_data, bot.update_data = get_data, update_data
    bot.pool.servers = SimpleNamespace(patch=patch)
    bot.get_guild = lambda guild_id: SimpleNamespace(id=guild_id, me=SimpleNamespace())

    class TelaDeEscolha:
        def __init__(self, *args, **kwargs):
            self.skin_selected, self.static_skin_selected = nova_skin, "default"
            self.global_mode = False
            self.interaction = SimpleNamespace(
                guild_id=SERVIDOR,
                response=SimpleNamespace(defer=_aguardar, is_done=lambda: True),
                author=SimpleNamespace(mention="<@900>"),
                edit_original_message=registrar,
                send=registrar,
            )
            self.children = []

        async def wait(self):
            return None

    async def selecionar_bot(interaction, return_new=False):
        return interaction, bot

    monkeypatch.setattr(music_settings, "SkinSelector", TelaDeEscolha)
    monkeypatch.setattr(music_settings, "select_bot_pool", selecionar_bot)
    monkeypatch.setattr(music_settings, "update_inter", lambda *args, **kwargs: None)
    monkeypatch.setattr(music_settings, "asyncio", SimpleNamespace(sleep=_aguardar))
    cog = MusicSettings.__new__(MusicSettings)
    cog.bot = bot
    interaction = SimpleNamespace(
        guild_id=SERVIDOR,
        guild=SimpleNamespace(id=SERVIDOR),
        author=SimpleNamespace(mention="<@900>"),
        response=SimpleNamespace(defer=_aguardar, is_done=lambda: True),
        edit_original_message=registrar,
        send=registrar,
    )
    await MusicSettings.change_skin.callback(cog, interaction)
    return jogador


async def _aguardar(*args, **kwargs):
    return None
