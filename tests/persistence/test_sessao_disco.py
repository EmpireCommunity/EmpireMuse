"""F3-6b, etapa 5 (RED, commit 2) — as sessões no DISCO: ``save_session_local``, ``get_player_sessions_local``, ``delete_data_local``.

Formato novo: ``local_database/player_sessions_json/<bot>/<guild>.json`` (JSON puro, escrita atômica, 0600) e uma cópia
``<guild>.json.bak``. A leitura devolve só dicionários do JSON validado: nome só ``[0-9]{1,20}.json`` de arquivo comum (sem link
simbólico, sem pasta), tamanho conferido antes de ler, ``_id`` do conteúdo igual ao do nome (condição 2 do 5.1). Tudo o que falhar
FICA onde está, com um aviso só com ``guild_id`` e ``code``. O ``.pkl`` do formato antigo não é lido nem apagado.
"""

import logging
import os
import stat

import disnake
import pytest

from tests.support.formatos_reais import BOT_ID, INSTANTE, SERVIDOR, cog_de_sessoes
from tests.support.avisos_no_log import registrou
from tests.support.serializacao_nucleo import SEGREDO
from tests.support.sessoes import (
    escrever_json,
    escrever_pkl,
    pasta_json,
    pasta_legado,
    session,
    sessao_minima,
)
from tests.support.formato_sessao import jogador_completo

pytestmark = pytest.mark.critical

OUTRO = SERVIDOR + 7


@pytest.fixture(autouse=True)
def _ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    monkeypatch.setattr(disnake.utils, "utcnow", lambda: INSTANTE)


# ============================================================================ o que o save_info e o save_session_local gravam


class TestGravacao:
    @pytest.mark.asyncio
    @pytest.mark.skipif(not hasattr(os, "fchmod"), reason="modo de arquivo só em POSIX")
    async def test_o_arquivo_e_privado_0600(self, tmp_path):
        await cog_de_sessoes().save_info(jogador_completo())
        modo = stat.S_IMODE((pasta_json(tmp_path) / f"{SERVIDOR}.json").stat().st_mode)
        assert modo == 0o600

    @pytest.mark.asyncio
    async def test_falha_na_troca_atomica_deixa_o_antigo_sem_temporario_e_sem_derrubar(
        self, tmp_path, monkeypatch, caplog
    ):
        cog = cog_de_sessoes()
        await cog.save_session_local(SERVIDOR, sessao_minima(volume=11))
        antigo = (pasta_json(tmp_path) / f"{SERVIDOR}.json").read_bytes()

        def quebrar(*args, **kwargs):
            raise OSError("disco cheio")

        monkeypatch.setattr(os, "replace", quebrar)
        with caplog.at_level(logging.DEBUG):
            await cog.save_session_local(SERVIDOR, sessao_minima(volume=99, prefix_info=SEGREDO[:20]))
        assert (pasta_json(tmp_path) / f"{SERVIDOR}.json").read_bytes() == antigo
        assert [p.name for p in pasta_json(tmp_path).iterdir() if p.name.endswith(".tmp")] == []
        assert all(SEGREDO[:20] not in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_varias_sessoes_no_mesmo_bot_e_so_dele(self, tmp_path):
        cog = cog_de_sessoes()
        for guild in (SERVIDOR, OUTRO):
            await cog.save_session_local(guild, sessao_minima(_id=guild))
        assert sorted(p.name for p in pasta_json(tmp_path).glob("*.json")) == sorted(
            [f"{SERVIDOR}.json", f"{OUTRO}.json"]
        )
        assert pasta_json(tmp_path).name == str(BOT_ID)


# ============================================================================ a leitura


class TestLeitura:
    @pytest.mark.asyncio
    async def test_nao_le_nada_que_nao_seja_json_de_sessao(self, tmp_path):
        bom = escrever_json(tmp_path, SERVIDOR)
        for nome in (
            f"{OUTRO}.json.bak",
            f"{OUTRO}.pkl",
            f"{OUTRO}.bak",
            f"{OUTRO}.json.tmp",
            f"{OUTRO}.txt",
            f"{OUTRO}",
        ):
            (bom.parent / nome).write_bytes(bom.read_bytes())
        assert [s["_id"] for s in await cog_de_sessoes().get_player_sessions_local()] == [SERVIDOR]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "nome",
        [
            "abc.json",
            "0x10.json",
        ],
        ids=[
            "letras",
            "hexa",
        ],
    )
    async def test_nome_fora_do_padrao_e_ignorado_e_nao_e_tocado(self, tmp_path, nome):
        bom = escrever_json(tmp_path, SERVIDOR)
        estranho = bom.parent / nome
        estranho.write_bytes(bom.read_bytes())
        assert [s["_id"] for s in await cog_de_sessoes().get_player_sessions_local()] == [SERVIDOR]
        assert estranho.is_file()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("quando", ["9999-12-31T23:59:59+00:00", "1999-01-01T00:00:00+00:00"])
    async def test_documento_gravado_direto_com_time_fora_do_intervalo_nao_retoma(self, tmp_path, caplog, quando):
        """Condição de aceite do AppSec (R2): a checagem vale na LEITURA, não só na gravação."""
        import json

        bom = escrever_json(tmp_path, SERVIDOR)
        forjado = pasta_json(tmp_path) / f"{OUTRO}.json"
        documento = json.loads(session.encode_session(sessao_minima(_id=OUTRO)))
        documento["time"] = quando
        forjado.write_text(json.dumps(documento))
        with caplog.at_level(logging.DEBUG):
            lidas = await cog_de_sessoes().get_player_sessions_local()
        assert [s["_id"] for s in lidas] == [SERVIDOR] and forjado.is_file() and bom.is_file()
        assert registrou(caplog, OUTRO, "range")

    @pytest.mark.asyncio
    async def test_so_le_a_pasta_do_proprio_bot(self, tmp_path):
        escrever_json(tmp_path, SERVIDOR)
        outra = tmp_path / "local_database" / "player_sessions_json" / str(BOT_ID + 1)
        outra.mkdir(parents=True)
        (outra / f"{OUTRO}.json").write_bytes(session.encode_session(sessao_minima(_id=OUTRO)))
        assert [s["_id"] for s in await cog_de_sessoes().get_player_sessions_local()] == [SERVIDOR]


# ============================================================================ a remoção


class TestRemocao:
    def test_remove_o_json_e_a_copia_so_do_servidor_pedido_e_nao_toca_no_formato_antigo(self, tmp_path):
        for guild in (SERVIDOR, OUTRO):
            escrever_json(tmp_path, guild)
            (pasta_json(tmp_path) / f"{guild}.json.bak").write_bytes(b"copia")
            escrever_pkl(tmp_path, guild)
            escrever_pkl(tmp_path, guild, ext=".bak")
        cog_de_sessoes().delete_data_local(SERVIDOR)
        restantes = sorted(p.name for pasta in (pasta_json(tmp_path), pasta_legado(tmp_path)) for p in pasta.iterdir())
        assert restantes == sorted(
            [f"{OUTRO}.json", f"{OUTRO}.json.bak", f"{SERVIDOR}.pkl", f"{SERVIDOR}.bak", f"{OUTRO}.pkl", f"{OUTRO}.bak"]
        )


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_classic_default_of_a_session_survives_save_encode_and_decode():
    from empiremuse.rules.skins import is_chosen, jukebox_choice
    from empiremuse.serialization import session
    from tests.support.formato_sessao import gravar

    data = await gravar(cog_de_sessoes(), jogador_completo(skin="default", skin_static="default"))
    back = session.decode_session(session.encode_session(data))

    assert jukebox_choice(back["skin"], is_chosen(back, "skin")) == "default"
    assert jukebox_choice(back["skin_static"], is_chosen(back, "skin_static")) == "default"
