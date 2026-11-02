"""Drivers de COMPORTAMENTO das regras de comandos e painel (rede de segurança da F3-5b, etapa 0).

Nada aqui lê o texto do código-fonte: cada driver chama o ``callback`` real do comando, do modal ou da view com
dublês de disnake e devolve o que o usuário veria e o que foi gravado. Sem Discord real, sem rede e sem ``.env``.
Os valores falsos são montados em tempo de execução.
"""

import copy
from types import SimpleNamespace

import disnake

import empiremuse.cogs.legacy_cmds as legacy_cmds
from empiremuse.i18n import get_i18n
from empiremuse.cogs.music import Music
from empiremuse.utils.others import PlayerControls
from empiremuse.panel.services.account_base import AccountContext
from empiremuse.panel.services.account_links import FavoritesSection
from empiremuse.utils.db import db_models, global_db_models
from empiremuse.utils.music.errors import GenericError
from tests.support.modais import SERVIDOR_DO_COMANDO

AUTOR = 900


class BotDeComandos:
    """Bot mínimo dos comandos de prefixo, cargos de DJ, skins, volume, status de voz e RPC.

    Os documentos nascem dos modelos REAIS (``db_models``/``global_db_models``) e são o mesmo dict a cada leitura,
    como no ``BaseDB``; cada gravação fica em ``gravacoes`` como ``(escopo, id, cópia)``.
    """

    def __init__(self, *, config=None, dono=False):
        self.config = {
            "EXTRA_HINTS": "",
            "IGNORE_SKINS": "",
            "IGNORE_STATIC_SKINS": "",
            "ENABLE_RPC_COMMAND": True,
            "ENABLE_RPC_AUTH": True,
            "RPC_PUBLIC_URL": "wss://rpc.exemplo.invalid",
            "RPC_SERVER": "",
            **(config or {}),
        }
        self.dono = dono
        self.docs = {}
        self.gravacoes = []
        self.pool = SimpleNamespace(
            guild_prefix_cache={},
            user_prefix_cache={},
            default_skin="classic",
            default_static_skin="simple",
            get_guild_bots=lambda guild_id: [self],
        )
        self.music = SimpleNamespace(players={})
        self.user = SimpleNamespace(id=7, name="Bot", display_name="Bot", mention="<@7>")
        self.default_prefix = "!!"
        self.player_skins = {}
        self.player_static_skins = {}
        self.guilds = {}
        self.cogs = {}

    def _doc(self, id_, db_name, *, escopo):
        modelos = global_db_models if escopo == "global" else db_models
        chave = (escopo, db_name, str(id_))
        if chave not in self.docs:
            self.docs[chave] = copy.deepcopy(modelos[db_name])
        return self.docs[chave]

    def documento(self, escopo, db_name, id_):
        return self._doc(id_, db_name, escopo=escopo)

    async def get_global_data(self, id_, *, db_name):
        return self._doc(id_, db_name, escopo="global")

    async def update_global_data(self, id_, data, *, db_name):
        self.gravacoes.append(("global", str(id_), copy.deepcopy(data)))

    async def get_data(self, id_, *, db_name):
        return self._doc(id_, db_name, escopo="bot")

    async def update_data(self, id_, data, *, db_name):
        self.gravacoes.append(("bot", str(id_), copy.deepcopy(data)))

    async def is_owner(self, user):
        return self.dono

    def get_color(self, *args):
        return 0

    def get_guild(self, guild_id):
        return self.guilds.get(guild_id)

    def get_cog(self, nome):
        return self.cogs.get(nome)

    def get_global_command_named(self, name, cmd_type=None):
        return SimpleNamespace(id=1)

    def is_ready(self):
        return True

    @property
    def ultimo_global(self):
        return next((copia for escopo, _, copia in reversed(self.gravacoes) if escopo == "global"), None)

    @property
    def ultimo_do_bot(self):
        return next((copia for escopo, _, copia in reversed(self.gravacoes) if escopo == "bot"), None)


class ContextoDeComando:
    """``ctx`` de um comando de prefixo: guarda o que foi enviado ao usuário."""

    def __init__(self, bot, *, servidor=SERVIDOR_DO_COMANDO, autor=AUTOR):
        self.bot = bot
        self.guild = SimpleNamespace(id=servidor, me=SimpleNamespace(), name="origem")
        self.guild_id = servidor
        self.author = SimpleNamespace(id=autor, mention=f"<@{autor}>")
        self.enviados = []

    async def send(self, *args, **kwargs):
        embed = kwargs.get("embed")
        self.enviados.append(" ".join(str(a) for a in args) + ((embed.description or "") if embed else ""))

    @property
    def texto(self):
        return "\n".join(self.enviados)


class Resultado(SimpleNamespace):
    """O que o usuário vê (``texto`` ou ``erro``) e o bot que guardou as gravações."""

    @property
    def recusado(self):
        return self.erro is not None


async def executar(bot, comando, *args, servidor=SERVIDOR_DO_COMANDO, autor=AUTOR, **kwargs):
    """Roda ``comando.callback`` do ``legacy_cmds.Owner``; ``GenericError`` vira ``Resultado.erro`` (texto)."""
    ctx = ContextoDeComando(bot, servidor=servidor, autor=autor)
    cog = legacy_cmds.Owner(bot)
    erro = None
    try:
        await comando.callback(cog, ctx, *args, **kwargs)
    except GenericError as falha:
        erro = falha.text
    return Resultado(erro=erro, texto=ctx.texto, bot=bot, ctx=ctx)


# ============================================================================ interações genéricas


class RespostaFalsa:
    def __init__(self, inter):
        self.inter = inter
        self.feita = False
        self.modais = []

    async def defer(self, **kwargs):
        self.feita = True

    def is_done(self):
        return self.feita

    async def send_message(self, *args, **kwargs):
        self.feita = True
        self.inter.enviados.append(self.inter.texto_de(args, kwargs))

    async def send_modal(self, *args, **kwargs):
        self.feita = True
        self.modais.append(args[0] if args else kwargs)

    async def edit_message(self, *args, **kwargs):
        self.feita = True
        self.inter.enviados.append(self.inter.texto_de(args, kwargs))


class InteracaoFalsa:
    """Interação de comando ou componente: registra o texto enviado e os modais abertos."""

    def __init__(self, *, autor=AUTOR, guild_id=SERVIDOR_DO_COMANDO, guild=None, custom_id="", valores=None):
        self.author = SimpleNamespace(
            id=autor,
            mention=f"<@{autor}>",
            display_name="Ana",
            discriminator="0",
            display_avatar=SimpleNamespace(
                with_static_format=lambda formato: SimpleNamespace(url="https://exemplo.invalid/avatar.png")
            ),
        )
        self.user = self.author
        self.guild_id = guild_id
        self.guild = guild
        self.id = 1234
        self.enviados = []
        self.componentes = []
        self.response = RespostaFalsa(self)
        self.data = SimpleNamespace(custom_id=custom_id, values=valores or [])
        self.text_values = {}
        self.channel = SimpleNamespace(id=11)
        self.message = SimpleNamespace(flags=SimpleNamespace(ephemeral=True), embeds=[], edit=self._nada)

    async def _nada(self, *args, **kwargs):
        return None

    @staticmethod
    def texto_de(args, kwargs):
        embed = kwargs.get("embed")
        partes = [str(a) for a in args] + ([str(kwargs["content"])] if kwargs.get("content") else [])
        if embed is not None:
            partes.append(embed.description or "")
        return "\n".join(partes)

    async def send(self, *args, **kwargs):
        self.response.feita = True
        self.componentes.extend(kwargs.get("components") or [])
        self.enviados.append(self.texto_de(args, kwargs))

    async def edit_original_message(self, *args, **kwargs):
        self.enviados.append(self.texto_de(args, kwargs))

    edit_original_response = edit_original_message

    @property
    def texto(self):
        return "\n".join(self.enviados)


# ============================================================================ painel: drivers diretos dos serviços


class BotDoPainel:
    """Só a configuração e o dono do bot que as seções de favoritos, integrações e links pedem."""

    def __init__(self, *, favs=10, integracoes=10, nome_max=35, url_max=90, dono=False):
        self.config = {
            "MAX_USER_FAVS": favs,
            "MAX_USER_INTEGRATIONS": integracoes,
            "USER_FAV_MAX_NAME_LENGTH": nome_max,
            "USER_FAV_MAX_URL_LENGTH": url_max,
        }
        self.dono = dono

    async def is_owner(self, user):
        return self.dono


def contexto_conta(bot):
    return AccountContext(pool=SimpleNamespace(), bot=bot, user_id=AUTOR)


def args_de(**campos):
    return {nome: [valor] for nome, valor in campos.items()}


async def painel_favoritos(bot, acao, favs, **campos):
    """``Parsed`` da seção de favoritos do painel para a ação (``favorito_adicionar``, ``favoritos_importar``…)."""
    return await FavoritesSection().parse(contexto_conta(bot), {"fav_links": dict(favs)}, acao, args_de(**campos))


# ============================================================================ bot: importadores e modais


# ============================================================================ cargos de DJ, skins, volume e status de voz


# ============================================================================ status de voz e volume

STATUS_ANTIGO = "Tocando {track.title}"


class ContextoDoStatus:
    """``ctx`` da view ``SetStageTitle``: só o autor e a mensagem editável que o ``close`` tenta atualizar."""

    def __init__(self):
        self.author = SimpleNamespace(id=AUTOR, mention=f"<@{AUTOR}>")

    async def edit_original_message(self, **kwargs):
        return None


# ============================================================================ botões de favoritar do player

URL_DA_MUSICA = "https://exemplo.invalid/musica"
SERVIDOR_DO_PLAYER = 5000


class BotDoPlayer(BotDeComandos):
    """``BotDeComandos`` com o que o ``player_controller`` pede antes de chegar nos botões de favoritar."""

    bot_ready = True

    def __init__(self, *, favs=10, dono=False, url_max=90, favoritos=None):
        super().__init__(dono=dono)
        self.config.update({"MAX_USER_FAVS": favs, "USER_FAV_MAX_URL_LENGTH": url_max})
        self.pool.add_fav_embed_cooldown = SimpleNamespace(
            get_bucket=lambda inter: SimpleNamespace(update_rate_limit=lambda: None)
        )
        self.eventos = []
        self.documento("global", "users", AUTOR)["fav_links"].update(favoritos or {})

        async def sem_canal_de_pedidos(gid):
            from empiremuse.utils.server_request import JukeboxConfig

            return JukeboxConfig(None, None, None, 0)  # sem painel configurado: o clique segue pelo caminho normal

        self.pool.servers = SimpleNamespace(jukebox=SimpleNamespace(get=sem_canal_de_pedidos))
        self.pool.i18n = get_i18n()  # o clique que falha responde no idioma do servidor

    def dispatch(self, nome, *args):
        self.eventos.append(nome)

    @property
    def favoritos(self):
        return self.documento("global", "users", AUTOR)["fav_links"]


class MusicaFalsa:
    """O ``self`` de ``Music.player_controller``: só o bot, os nomes dos comandos de prefixo e a trava de concorrência."""

    def __init__(self, bot):
        self.bot = bot
        self.play_legacy = SimpleNamespace(name="play")
        self.fav_manager_legacy = SimpleNamespace(name="fav_manager")
        self.player_interaction_concurrency = SimpleNamespace(acquire=self._nada, release=self._nada)

    @staticmethod
    async def _nada(*args, **kwargs):
        return None


def interacao(guild):
    inter = InteracaoFalsa(guild_id=SERVIDOR_DO_PLAYER, guild=guild)
    inter.message = SimpleNamespace(
        embeds=[], flags=SimpleNamespace(ephemeral=True), edit=inter._nada, content=str(AUTOR)
    )
    return inter


async def favoritar_pelo_embed(bot, titulo, url=URL_DA_MUSICA):
    """Clica em "Favoritar" no embed da música (``PlayerControls.embed_add_fav``); o nome é o autor do embed sem o 1º char."""
    guild = SimpleNamespace(me=SimpleNamespace())
    inter = interacao(guild)
    inter.message.embeds = [disnake.Embed(description="x").set_author(name=f"⠂{titulo}", url=url)]
    await Music.player_controller(MusicaFalsa(bot), inter, PlayerControls.embed_add_fav)
    return inter


async def favoritar_pelo_seletor(bot, titulo, url=URL_DA_MUSICA):
    """Clica em "Favoritar" no player da música atual (``PlayerControls.add_favorite``) com um único item para escolher."""
    canal = SimpleNamespace(id=77)
    guild = SimpleNamespace(me=SimpleNamespace(voice=SimpleNamespace(channel=canal)))
    inter = interacao(guild)
    inter.author.voice = SimpleNamespace(channel=canal)
    player = SimpleNamespace(
        message=inter.message,
        interaction_cooldown=False,
        guild=guild,
        tr=get_i18n().for_locale("pt-BR"),
        current=SimpleNamespace(
            uri=url, title=titulo, album_url=None, album_name=None, playlist_url=None, playlist_name=None
        ),
    )
    bot.music.players[SERVIDOR_DO_PLAYER] = player
    await Music.player_controller(MusicaFalsa(bot), inter, PlayerControls.add_favorite)
    return inter


def gravou(bot):
    return bool(bot.gravacoes)


# ============================================================================ token do app de RPC
