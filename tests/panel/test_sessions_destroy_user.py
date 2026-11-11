"""Apagar meus dados (E1): ``SessionStore.destroy_user`` (T10) e a ligação do painel ao pool (lock único de usuário e loja de sessões).

``fake_pool`` é um ``SimpleNamespace``: o painel tem de funcionar sem ``user_locks`` (os outros testes do painel não o trazem).
"""

from types import SimpleNamespace

import pytest

from empiremuse import panel
from empiremuse.panel.sessions import ManagedGuild, PanelUser, SessionStore, UserCleanupError
from empiremuse.utils.user_locks import UserLocks
from tests.panel.helpers import fake_pool, https_config, load_settings
from tests.panel.helpers_login import FakeClock

pytestmark = pytest.mark.critical


def _loja(clock: FakeClock) -> SessionStore:
    return SessionStore(load_settings(https_config()), clock=clock)


def _usuario(user_id: str) -> PanelUser:
    return PanelUser(id=user_id, username=f"u{user_id}", global_name=None, avatar=None)


GUILDS = (ManagedGuild(id="1", name="Servidor", icon=None, permissions=8, owner=False),)


def test_t10_destroy_user_remove_as_5_sessoes_do_usuario_e_so_as_dele():
    loja = _loja(FakeClock())
    do_alvo = [loja.create(_usuario("1001"), GUILDS) for _ in range(5)]
    de_outro = loja.create(_usuario("100"), GUILDS)  # id que é prefixo do alvo
    de_outro_2 = loja.create(_usuario("11001"), GUILDS)  # id que termina com o do alvo

    assert loja.destroy_user("1001") == 5

    assert [loja.get(sid) for sid in do_alvo] == [None] * 5
    assert loja.get(de_outro) is not None and loja.get(de_outro_2) is not None
    assert loja.destroy_user("1001") == 0  # de novo: nada a destruir, nem erro
    assert loja.destroy_user(100) == 1  # aceita o id como número


def test_painel_usa_o_lock_de_usuario_do_pool_e_registra_a_loja_de_sessoes_nele():
    pool = fake_pool()
    pool.user_locks = UserLocks()

    app = panel.build_panel_app(pool, load_settings(https_config()))

    assert app is not None
    assert app.settings["settings_service"].lock("users", 7) is pool.user_locks.lock(7)
    assert pool.panel_sessions is app.settings["session_store"]


def test_painel_funciona_com_pool_sem_user_locks():
    pool = SimpleNamespace(bots=[], database=None)

    app = panel.build_panel_app(pool, load_settings(https_config()))

    assert app is not None and pool.panel_sessions is app.settings["session_store"]


def test_ouvinte_que_falha_nao_impede_os_outros_nem_a_destruicao_e_levanta_depois():
    loja = _loja(FakeClock())
    sid = loja.create(_usuario("1001"), GUILDS)
    called = []

    def broken(user_id):
        raise RuntimeError("falha")

    loja.add_user_listener(broken)
    loja.add_user_listener(called.append)

    with pytest.raises(UserCleanupError) as raised:
        loja.destroy_user("1001")

    assert called == ["1001"] and loja.get(sid) is None
    assert (raised.value.destroyed, raised.value.failures) == (1, 1)


def test_o_apagamento_marca_a_memoria_como_nao_limpa_quando_um_ouvinte_falha():
    from empiremuse.utils.erasure import ErasureService

    loja = _loja(FakeClock())

    def broken(user_id):
        raise RuntimeError("falha")

    loja.add_user_listener(broken)
    service = ErasureService(SimpleNamespace(panel_sessions=loja), rpc_closer=lambda user_id: 0)

    result = service._close_connections(1001)

    assert result.ok is False
    loja._user_listeners.clear()
    assert service._close_connections(1001).ok is True


def test_o_apagamento_conta_as_sessoes_destruidas_mesmo_quando_um_ouvinte_falha():
    from empiremuse.utils.erasure import ErasureService

    loja = _loja(FakeClock())
    for _ in range(2):
        loja.create(_usuario("1001"), GUILDS)

    def broken(user_id):
        raise RuntimeError("falha")

    loja.add_user_listener(broken)
    service = ErasureService(SimpleNamespace(panel_sessions=loja), rpc_closer=lambda user_id: 0)

    result = service._close_connections(1001)

    assert (result.count, result.ok) == (2, False)  # as duas sessões caíram (contam); a limpeza é parcial
