"""Dublês compartilhados dos testes de modais, importadores e comandos de prefixo (F3-5a e F3-5b).

Só montam o que ``interactions/`` e ``legacy_cmds.py`` pedem ao bot e à interação: sem Discord real, sem rede e sem
``.env``. Os dados são o MESMO dict que o banco devolveria (como no ``BaseDB``) e cada gravação fica registrada.
"""

import copy
import json
from types import SimpleNamespace

from empiremuse.i18n import get_i18n
from empiremuse.utils.erasure import ErasureTombstones
from empiremuse.utils.music import interactions
from empiremuse.utils.music.interactions import FavMenuView, FavModalAdd, FavModalImport, ViewMode
from empiremuse.utils.music.interactions import fav_menu, fav_modals
from empiremuse.utils.others import CustomContext


SERVIDOR = 5000
URL_BASE = "https://exemplo.invalid"


def trocar_process_idle_embed(monkeypatch, falso):
    """Troca ``process_idle_embed`` em todo módulo que o lê pelo nome: os que o importam (``fav_modals`` e ``fav_menu``)
    e o pacote ``interactions`` (o painel chama ``interactions.process_idle_embed``). Trocar só o pacote não afeta os
    modais: o nome importado fica preso ao módulo onde o código mora."""
    for modulo in (interactions, fav_modals, fav_menu):
        if hasattr(modulo, "process_idle_embed"):  # as playlists do servidor atualizam o embed pelo store, não por aqui
            monkeypatch.setattr(modulo, "process_idle_embed", falso)


class ModalBot:
    """Só o que os modais de ``interactions.py`` pedem ao bot; os dados são o MESMO dict (como no BaseDB)."""

    loop = None  # o modal de integração usa ``bot.loop or asyncio.get_event_loop()`` para o yt-dlp

    def __init__(
        self,
        *,
        favs=10,
        integracoes=10,
        dono=False,
        fav_links=None,
        integration_links=None,
        nome_max=25,
        url_max=200,
        retry_after=None,
    ):
        self.config = {
            "MAX_USER_FAVS": favs,
            "MAX_USER_INTEGRATIONS": integracoes,
            "USER_FAV_MAX_URL_LENGTH": url_max,
            "USER_FAV_MAX_NAME_LENGTH": nome_max,
        }
        self.dono = dono
        self.retry_after = retry_after
        self.user_data = {"fav_links": dict(fav_links or {}), "integration_links": dict(integration_links or {})}
        self.guild_data = {"player_controller": {"channel": "22", "message_id": "555", "fav_links": {}}}
        self.salvos_usuario, self.salvos_servidor = [], []
        self.spotify = None
        self.deezer = SimpleNamespace(get_user_info=self._deezer_user)
        self.pool = SimpleNamespace(
            i18n=get_i18n(), erasure_tombstones=ErasureTombstones()
        )  # o tradutor da guilda (pt-BR nos testes)

    async def _deezer_user(self, user_id):
        return {"name": "Ana", "link": f"https://www.deezer.com/br/profile/{user_id}", "picture": f"{URL_BASE}/ana.png"}

    def get_cog(self, nome):
        return SimpleNamespace(
            fav_import_export_cd=SimpleNamespace(
                get_bucket=lambda inter: SimpleNamespace(update_rate_limit=lambda: self.retry_after)
            ),
            fav_manager=SimpleNamespace(name="fav_manager"),
        )

    async def is_owner(self, user):
        return self.dono

    async def get_global_data(self, id_, *, db_name):
        return self.user_data

    async def update_global_data(self, id_, data, *, db_name):
        self.salvos_usuario.append(copy.deepcopy(data))

    async def get_data(self, id_, *, db_name):
        return self.guild_data

    async def update_data(self, id_, data, *, db_name):
        self.salvos_servidor.append(copy.deepcopy(data))

    def get_channel(self, cid):
        return object()

    def get_guild(self, gid):
        return SimpleNamespace(id=gid, me=SimpleNamespace())

    def get_color(self, *args):
        return 0

    def get_global_command_named(self, name, cmd_type=None):
        return SimpleNamespace(id=1)


class ModalContext:
    def __init__(self):
        self.atualizacoes = []

    async def edit_original_message(self, **kwargs):
        self.atualizacoes.append(kwargs)


class ContextoDePrefixo(CustomContext):
    """``isinstance(ctx, CustomContext)`` verdadeiro, sem a montagem pesada do contexto real."""

    def __init__(self):
        self.atualizacoes = []


class MensagemDoMenu:
    def __init__(self):
        self.atualizacoes = []

    async def edit(self, **kwargs):
        self.atualizacoes.append(kwargs)


class ViewDoMenu(SimpleNamespace):
    """Só expõe os métodos que a ``FavMenuView`` REAL tem: o resto levanta ``AttributeError`` como em produção."""

    def __getattr__(self, nome):
        if not nome.startswith("__") and callable(getattr(FavMenuView, nome, None)):
            return lambda *args, **kwargs: ""
        raise AttributeError(f"a FavMenuView real não tem {nome!r}")


def modal_view(bot, mode, *, real=True, prefixo=False):
    ctx = ContextoDePrefixo() if prefixo else ModalContext()
    campos = dict(mode=mode, bot=bot, data=None, guild_data=None, ctx=ctx, log="", tr=get_i18n().for_locale("pt-BR"))
    campos["message"] = MensagemDoMenu() if prefixo else None
    if real:
        return ViewDoMenu(**campos)
    return SimpleNamespace(**campos, build_txt=lambda: "", build_embed=lambda: None)


class ModalInteraction:
    def __init__(self, *, estrito=False, **text_values):
        self.estrito, self.respondida, self.arquivos, self.nomes_de_arquivo = estrito, False, [], []
        self.text_values = text_values
        self.resolved_values = {}  # ``FileUpload`` do modal de favoritos: {custom_id: [Attachment]}; vazio = sem anexo
        self.author = SimpleNamespace(id=900)
        self.guild = None
        self.guild_id = SERVIDOR
        self.enviados = []
        self.response = SimpleNamespace(defer=self._defer)

    async def _defer(self, **kwargs):
        self.respondida = True

    async def send(self, content=None, **kwargs):
        self.respondida = True
        if kwargs.get("file") is not None:
            self.arquivos.append(kwargs["file"].fp.read().decode())
            self.nomes_de_arquivo.append(kwargs["file"].filename)
        self.enviados.append(self._texto(content, kwargs))

    async def edit_original_message(self, content=None, **kwargs):
        if self.estrito and not self.respondida:
            raise RuntimeError("edit_original_message antes de a interação ser respondida (o Discord recusa)")
        self.enviados.append(self._texto(content, kwargs))

    @staticmethod
    def _texto(content, kwargs):
        embed = kwargs.get("embed")
        return f"{content or ''}{embed.description if embed is not None else ''}"

    @property
    def texto(self):
        return "\n".join(self.enviados)


async def adicionar_integracao(bot, url="https://www.deezer.com/br/profile/123456"):
    view = modal_view(bot, ViewMode.integrations_manager)
    inter = ModalInteraction(user_integration_url=url)
    await FavModalAdd(None, None, view).callback(inter)
    return inter


async def importar(bot, modo, conteudo, *, estrito=False, view=None):
    view = view or modal_view(bot, modo)
    texto = conteudo if isinstance(conteudo, str) else json.dumps(conteudo, indent=4)
    inter = ModalInteraction(json_data=texto, estrito=estrito)
    await FavModalImport(view).callback(inter)
    return inter


SERVIDOR_DO_COMANDO = 7001
SERVIDOR_ALVO = 123456789012345678  # 18 dígitos: passa a checagem de tamanho do comando


def gravados(bot, modo):
    """O último dicionário de entradas gravado no banco pelo importador do modo (``{}`` se não gravou)."""
    if modo == ViewMode.fav_manager:
        return bot.salvos_usuario[-1]["fav_links"] if bot.salvos_usuario else {}
    if modo == ViewMode.integrations_manager:
        return bot.salvos_usuario[-1]["integration_links"] if bot.salvos_usuario else {}
    return bot.salvos_servidor[-1]["player_controller"]["fav_links"] if bot.salvos_servidor else {}
