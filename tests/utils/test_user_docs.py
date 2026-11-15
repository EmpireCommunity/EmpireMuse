"""BK-2: quem grava o documento inteiro do membro (``global.users``) passa por ``change_user_doc``, sob o lock do usuário.

O lock e a sepultura são os reais (``UserLocks``, ``ErasureTombstones`` com relógio falso); o banco é um dict em memória. Sem relógio
real e sem ``sleep``: a fila do lock é o próprio ``asyncio.Lock``.
"""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.utils.erasure import ErasureTombstones
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.interactions.common import ViewMode
from empiremuse.utils.music.interactions.fav_menu import FavMenuView
from empiremuse.utils.user_docs import SKIP, Skip, change_user_doc
from empiremuse.utils.user_locks import UserLocks

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

MEMBRO = 42
ERASED_TEXT = get_i18n().for_locale("pt-BR")("fav.erased_while_saving")


class Bot:
    """Só o que os escritores pedem: o documento do membro (o MESMO dict do cache, como no banco real), o lock e a sepultura."""

    def __init__(self, **doc):
        self.relogio = [1000.0]
        self.doc = {"custom_prefix": "", "token": "", "fav_links": {}, "integration_links": {}, **doc}
        self.gravados: list[dict] = []
        self.leituras = 0
        self.pool = SimpleNamespace(
            erasure_tombstones=ErasureTombstones(clock=lambda: self.relogio[0]),
            user_locks=UserLocks(),
            user_prefix_cache={},
            i18n=get_i18n(),
            get_all_bots=lambda: [],  # o RPC fecha a presença do token antigo antes de gravar: aqui não há player
        )

    async def get_global_data(self, id_, *, db_name):
        self.leituras += 1
        return self.doc

    async def update_global_data(self, id_, data, *, db_name):
        self.gravados.append(copy.deepcopy(data))
        self.doc = data

    def apagamento_roda_enquanto_a_escrita_espera(self):
        """O lock do membro passa a ser o de uma escrita que ficou na fila do apagamento: ao entrar, a exclusão já terminou."""
        real, bot = self.pool.user_locks, self

        class NaFila:
            def __init__(self, lock):
                self._lock = lock

            async def __aenter__(self):
                bot.relogio[0] += 1
                bot.pool.erasure_tombstones.mark(MEMBRO)  # o fim da exclusão, depois do ``read_at`` de quem esperava
                bot.doc = {"custom_prefix": "", "token": "", "fav_links": {}, "integration_links": {}}
                await self._lock.acquire()

            async def __aexit__(self, *exc):
                self._lock.release()

        self.pool.user_locks = SimpleNamespace(lock=lambda user_id: NaFila(real.lock(user_id)))


# ------------------------------------------------------------------------------------------------ o helper


async def test_a_write_queued_behind_the_erasure_reads_and_writes_nothing():
    bot = Bot(custom_prefix="!")
    lock, na_fila = bot.pool.user_locks.lock(MEMBRO), asyncio.Event()
    await lock.acquire()  # o apagamento está rodando

    class Sinaliza:
        async def __aenter__(self):
            na_fila.set()  # síncrono até o ``acquire``: quando o teste acorda, a escrita já está na fila do lock
            await lock.acquire()

        async def __aexit__(self, *exc):
            lock.release()

    bot.pool.user_locks = SimpleNamespace(lock=lambda user_id: Sinaliza())
    read_at = bot.pool.erasure_tombstones.now()
    pendente = asyncio.ensure_future(
        change_user_doc(bot, MEMBRO, lambda doc: doc.__setitem__("custom_prefix", "?"), read_at=read_at)
    )
    await na_fila.wait()
    bot.relogio[0] += 1
    bot.pool.erasure_tombstones.mark(MEMBRO)  # fim da exclusão
    lock.release()

    change = await pendente

    assert change.erased and not change.saved and change.doc is None
    assert bot.gravados == [] and bot.leituras == 0  # nem a leitura (que recriaria o documento padrão) acontece


async def test_the_change_runs_on_a_copy_reread_under_the_lock_and_skip_keeps_the_reason():
    bot = Bot(fav_links={"Rock": "https://example.com/rock"})
    cache = bot.doc
    read_at = bot.pool.erasure_tombstones.now()

    recusado = await change_user_doc(bot, MEMBRO, lambda doc: Skip("cheio"), read_at=read_at)
    intacto = await change_user_doc(bot, MEMBRO, lambda doc: doc["fav_links"].clear() or SKIP, read_at=read_at)
    gravado = await change_user_doc(bot, MEMBRO, lambda doc: doc["fav_links"].pop("Rock"), read_at=read_at)
    desconhecido = await change_user_doc(bot, MEMBRO, lambda doc: None, read_at=None)

    assert (recusado.saved, recusado.result) == (False, "cheio")
    assert not intacto.saved and cache["fav_links"] == {
        "Rock": "https://example.com/rock"
    }  # a cópia do cache nunca é mexida
    assert gravado.saved and gravado.result == "https://example.com/rock" and bot.gravados == [gravado.doc]
    assert desconhecido.erased and len(bot.gravados) == 1  # hora da leitura desconhecida: falha fechado


# ------------------------------------------------------------------------------------------------ os escritores que não tinham lock


def _interacao(custom_id=None):
    return SimpleNamespace(
        author=SimpleNamespace(id=MEMBRO),
        guild=None,
        guild_id=1,
        data=SimpleNamespace(custom_id=custom_id),
        response=SimpleNamespace(defer=AsyncMock()),
        message=SimpleNamespace(flags=SimpleNamespace(ephemeral=True)),
        send=AsyncMock(),
        edit_original_message=AsyncMock(),
    )


async def _prefixo(bot):
    from empiremuse.cogs.legacy_cmds import Owner

    cog = Owner.__new__(Owner)
    cog.bot = bot
    with pytest.raises(GenericError) as erro:
        await Owner.setuserprefix.callback(cog, SimpleNamespace(author=SimpleNamespace(id=MEMBRO), guild=None), "?")
    return str(erro.value)


async def _token_do_rpc(bot):
    from empiremuse.cogs.music_settings import RPCCog

    cog = RPCCog.__new__(RPCCog)
    cog.bot = bot
    inter = _interacao(f"rpc_gen.{MEMBRO}")
    await cog.rpc_button_event(inter)
    return inter.send.await_args.args[0]


async def _menu(bot, acao):
    view = FavMenuView.__new__(FavMenuView)
    view.bot, view.mode, view.current, view.log = bot, ViewMode.fav_manager, "Rock", ""
    view.tr = get_i18n().for_locale("pt-BR")
    inter = _interacao()
    await getattr(view, acao)(inter)
    aviso = inter.edit_original_message if acao == "remove_callback" else inter.send
    return aviso.await_args.args[0]


ESCRITORES = {
    "prefixo": _prefixo,
    "token-do-rpc": _token_do_rpc,
    "remover-favorito": lambda bot: _menu(bot, "remove_callback"),
    "limpar-favoritos": lambda bot: _menu(bot, "clear_callback"),
}


@pytest.mark.regression
@pytest.mark.parametrize("escritor", ESCRITORES)
async def test_a_legacy_writer_queued_behind_the_erasure_does_not_recreate_the_document(escritor):
    bot = Bot(custom_prefix="!", token="T" * 50, fav_links={"Rock": "https://example.com/rock"})
    bot.apagamento_roda_enquanto_a_escrita_espera()

    aviso = await ESCRITORES[escritor](bot)

    assert bot.gravados == [] and ERASED_TEXT in aviso
    assert bot.pool.user_prefix_cache == {}  # nem o cache guarda o que não foi gravado
