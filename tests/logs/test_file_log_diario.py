"""``DailyRetentionFileHandler``: arquivo por dia (UTC), poda por idade do nome, suspensão legal e teto diário (T16, T17)."""

import logging
import os
import stat
import sys
from datetime import UTC, datetime, timedelta

import pytest

from empiremuse.utils import file_log
from empiremuse.utils.file_log import DailyRetentionFileHandler, prune_logs
from tests.logs.apoio import registro

pytestmark = pytest.mark.critical

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="modos de arquivo e links POSIX")
AGORA = datetime(2026, 11, 9, 23, 59, 0, tzinfo=UTC)


class Relogio:
    def __init__(self, agora: datetime = AGORA) -> None:
        self.agora = agora

    def __call__(self) -> datetime:
        return self.agora

    def avancar(self, **delta: float) -> None:
        self.agora += timedelta(**delta)


def nome(dias_atras: int, prefixo: str = "disnake", base: datetime = AGORA) -> str:
    return f"{prefixo}-{(base - timedelta(days=dias_atras)).date().isoformat()}.log"


def criar(pasta, *nomes: str) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    for item in nomes:
        (pasta / item).write_text("linha antiga\n", encoding="utf-8")


def nomes_na_pasta(pasta) -> set[str]:
    return {item.name for item in pasta.iterdir()}


class TestPoda:
    def test_184_dias_sai_e_182_fica_pela_data_do_nome_e_nao_pelo_mtime(self, tmp_path):
        pasta = tmp_path / ".logs"
        criar(pasta, nome(184), nome(183), nome(182), nome(400, "access"))
        os.utime(pasta / nome(184), None)  # modificado agora: a idade é a do nome
        os.utime(pasta / nome(182), (1, 1))  # "modificado em 1970": a idade é a do nome

        removidos, falhas = prune_logs(pasta, AGORA.date(), 183)

        assert (removidos, falhas) == (2, 0)
        assert nomes_na_pasta(pasta) == {nome(183), nome(182)}

    @POSIX
    def test_so_apaga_arquivo_regular_de_nome_exato(self, tmp_path):
        pasta = tmp_path / ".logs"
        fora = tmp_path / "alvo.txt"
        fora.write_text("não pode sumir", encoding="utf-8")
        intactos = [
            f"{nome(300)}.1",
            nome(300).replace(".log", ".gz"),
            "outro-2026-01-01.log",
            "disnake-2026-13-45.log",
        ]
        criar(pasta, *intactos)
        (pasta / nome(500)).symlink_to(fora)
        (pasta / nome(501)).mkdir()
        (pasta / nome(501) / "dentro.txt").write_text("subpasta intacta", encoding="utf-8")

        removidos, falhas = prune_logs(pasta, AGORA.date(), 183)

        assert (removidos, falhas) == (0, 0)
        assert nomes_na_pasta(pasta) == {*intactos, nome(500), nome(501)}
        assert fora.read_text(encoding="utf-8") == "não pode sumir" and (pasta / nome(501) / "dentro.txt").exists()

    def test_suspensao_legal_nao_apaga_nada(self, tmp_path):
        pasta = tmp_path / ".logs"
        criar(pasta, nome(184), nome(1000))

        assert prune_logs(pasta, AGORA.date(), 183, hold=True) == (0, 0)
        assert nomes_na_pasta(pasta) == {nome(184), nome(1000)}

    def test_falha_num_arquivo_conta_e_nao_impede_os_demais(self, tmp_path, monkeypatch):
        pasta = tmp_path / ".logs"
        criar(pasta, nome(300), nome(301))
        real = os.unlink

        def unlink(caminho, *a, **k):
            if str(caminho).endswith(nome(300)):
                raise PermissionError(13, "negado")
            real(caminho, *a, **k)

        monkeypatch.setattr(file_log.os, "unlink", unlink)

        assert prune_logs(pasta, AGORA.date(), 183) == (1, 1)
        assert nomes_na_pasta(pasta) == {nome(300)}

    def test_pasta_inexistente_conta_uma_falha_sem_levantar(self, tmp_path):
        assert prune_logs(tmp_path / "nao-existe", AGORA.date(), 183) == (0, 1)

    def test_a_subida_poda_e_a_suspensao_legal_do_handler_preserva(self, tmp_path):
        pasta = tmp_path / ".logs"
        criar(pasta, nome(184))

        suspenso = DailyRetentionFileHandler(pasta, hold=True, now=Relogio())
        suspenso.close()
        assert nome(184) in nomes_na_pasta(pasta)

        podado = DailyRetentionFileHandler(pasta, now=Relogio())
        podado.close()
        assert nome(184) not in nomes_na_pasta(pasta) and podado.last_prune_removed == 1

    def test_a_poda_roda_na_troca_de_dia_e_no_gancho_no_maximo_uma_vez_por_hora(self, tmp_path):
        pasta = tmp_path / ".logs"
        relogio = Relogio(datetime(2026, 11, 9, 12, 0, tzinfo=UTC))
        handler = DailyRetentionFileHandler(pasta, now=relogio)
        try:
            criar(pasta, nome(184, base=relogio.agora))
            assert handler.maintain() is False  # a poda da subida foi agora há pouco
            assert nome(184, base=relogio.agora) in nomes_na_pasta(pasta)

            relogio.avancar(hours=1)
            assert handler.maintain() is True
            assert nome(184, base=relogio.agora) not in nomes_na_pasta(pasta)

            criar(pasta, nome(185, base=relogio.agora + timedelta(days=1)))
            relogio.avancar(hours=12)  # passa da meia-noite UTC: a emissão troca o arquivo e poda
            handler.handle(registro("bom dia"))
            assert nome(185, base=relogio.agora + timedelta(days=1)) not in nomes_na_pasta(pasta)
        finally:
            handler.close()


class TestArquivoDoDia:
    @POSIX
    def test_cria_o_arquivo_do_dia_com_modo_0600_na_pasta_0700(self, tmp_path):
        handler = DailyRetentionFileHandler(tmp_path / ".logs", now=Relogio())
        try:
            handler.handle(registro("olá"))
        finally:
            handler.close()

        arquivo = tmp_path / ".logs" / nome(0)
        assert "olá" in arquivo.read_text(encoding="utf-8")
        assert stat.S_IMODE(arquivo.stat().st_mode) == 0o600
        assert stat.S_IMODE(arquivo.parent.stat().st_mode) == 0o700

    @POSIX
    def test_link_simbolico_no_nome_do_dia_nao_e_seguido(self, tmp_path):
        pasta = tmp_path / ".logs"
        pasta.mkdir()
        alvo = tmp_path / "alvo.txt"
        alvo.write_text("intacto", encoding="utf-8")
        (pasta / nome(0)).symlink_to(alvo)

        with pytest.raises(OSError):
            DailyRetentionFileHandler(pasta, now=Relogio())
        assert alvo.read_text(encoding="utf-8") == "intacto"

    def test_troca_de_arquivo_a_meia_noite_utc(self, tmp_path):
        relogio = Relogio()
        handler = DailyRetentionFileHandler(tmp_path / ".logs", now=relogio)
        try:
            handler.handle(registro("antes da meia-noite"))
            relogio.avancar(minutes=2)
            handler.handle(registro("depois da meia-noite"))
        finally:
            handler.close()

        antes = (tmp_path / ".logs" / nome(0)).read_text(encoding="utf-8")
        depois = (tmp_path / ".logs" / nome(-1)).read_text(encoding="utf-8")
        assert "antes da meia-noite" in antes and "depois" not in antes
        assert "depois da meia-noite" in depois and "antes" not in depois

    def test_reinicio_no_mesmo_dia_acrescenta_ao_arquivo(self, tmp_path):
        for texto in ("primeira subida", "segunda subida"):
            handler = DailyRetentionFileHandler(tmp_path / ".logs", now=Relogio())
            handler.handle(registro(texto))
            handler.close()

        conteudo = (tmp_path / ".logs" / nome(0)).read_text(encoding="utf-8")
        assert "primeira subida" in conteudo and "segunda subida" in conteudo


class TestTetoDiario:
    def test_passado_o_teto_escreve_um_marcador_descarta_o_resto_e_o_dia_seguinte_e_normal(self, tmp_path):
        relogio = Relogio()
        handler = DailyRetentionFileHandler(tmp_path / ".logs", now=relogio, max_day_bytes=700)
        try:
            for i in range(40):
                handler.handle(registro(f"linha numero {i:02d}"))
            relogio.avancar(minutes=2)
            handler.handle(registro("linha do dia seguinte"))
        finally:
            handler.close()

        dia = (tmp_path / ".logs" / nome(0)).read_text(encoding="utf-8")
        assert dia.count("log do dia truncado") == 1
        assert "linha numero 00" in dia and "linha numero 39" not in dia
        assert len(dia.encode("utf-8")) < 700 + 300  # o teto mais o marcador; o resto foi descartado
        assert "linha do dia seguinte" in (tmp_path / ".logs" / nome(-1)).read_text(encoding="utf-8")
        assert "truncado" not in (tmp_path / ".logs" / nome(-1)).read_text(encoding="utf-8")

    def test_a_inundacao_do_dia_nunca_apaga_arquivo_de_outro_dia(self, tmp_path):
        pasta = tmp_path / ".logs"
        criar(pasta, nome(5), nome(100))
        handler = DailyRetentionFileHandler(pasta, now=Relogio(), max_day_bytes=300)
        try:
            for i in range(200):
                handler.handle(registro(f"inundação {i}"))
        finally:
            handler.close()

        assert {nome(5), nome(100)} <= nomes_na_pasta(pasta)

    def test_reinicio_com_o_arquivo_do_dia_ja_no_teto_nao_escreve_mais_nada(self, tmp_path):
        pasta = tmp_path / ".logs"
        criar(pasta, nome(0))
        tamanho = (pasta / nome(0)).stat().st_size
        handler = DailyRetentionFileHandler(pasta, now=Relogio(), max_day_bytes=tamanho)
        try:
            handler.handle(registro("não cabe"))
        finally:
            handler.close()

        assert (pasta / nome(0)).stat().st_size == tamanho


class TestRedacao:
    def test_o_handler_pronto_redige_token_de_webhook(self, tmp_path):
        token = "AbC-dEf_123.xYz-SECRETO-token_do_webhook"
        handler = file_log.build_file_log_handler(tmp_path / ".logs", now=Relogio())
        try:
            handler.handle(registro("POST https://discord.com/api/webhooks/1234567890123456/%s", token))
        finally:
            handler.close()

        texto = (tmp_path / ".logs" / nome(0)).read_text(encoding="utf-8")
        assert token not in texto and "/webhooks/1234567890123456/***" in texto

    def test_erro_ao_escrever_nao_levanta(self, tmp_path, monkeypatch):
        handler = DailyRetentionFileHandler(tmp_path / ".logs", now=Relogio())
        monkeypatch.setattr(handler, "_write", lambda *a: (_ for _ in ()).throw(OSError(28, "disco cheio")))
        try:
            handler.handle(registro("x"))  # handleError -> report_emit_failure: nunca levanta
        finally:
            handler.close()
        assert isinstance(handler, logging.Handler)


class TestRelogioPadraoEAviso:
    def test_o_relogio_padrao_e_utc(self, monkeypatch):
        chamadas = []

        class Falso(datetime):
            @classmethod
            def now(cls, tz=None):
                chamadas.append(tz)
                return AGORA

        monkeypatch.setattr(file_log, "datetime", Falso)

        assert file_log._utc_now() == AGORA and chamadas == [UTC]

    def test_o_teto_diario_avisa_uma_vez_no_log_geral(self, tmp_path, caplog):
        handler = DailyRetentionFileHandler(tmp_path / ".logs", now=Relogio(), max_day_bytes=300)
        try:
            with caplog.at_level(logging.WARNING, logger="empiremuse.utils.file_log"):
                for i in range(50):
                    handler.handle(registro(f"inundação {i}"))
        finally:
            handler.close()

        assert caplog.text.count("Log do dia truncado") == 1
        assert all(
            getattr(r, "empiremuse_emit", False) is True for r in caplog.records if "truncado" in r.getMessage()
        )  # auditoria: nunca agregada

    @POSIX
    def test_arquivo_do_dia_ja_existente_com_modo_largo_vira_0600(self, tmp_path):
        pasta = tmp_path / ".logs"
        criar(pasta, nome(0))
        os.chmod(pasta / nome(0), 0o644)  # o arquivo já existia com modo largo: só o fchmod conserta
        handler = DailyRetentionFileHandler(pasta, now=Relogio())
        handler.close()

        assert stat.S_IMODE((tmp_path / ".logs" / nome(0)).stat().st_mode) == 0o600
