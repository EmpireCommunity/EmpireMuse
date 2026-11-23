# -*- coding: utf-8 -*-
import logging
import os
import sys
import time
from platform import python_version
from typing import Any

from empiremuse import paths
from empiremuse.updater import boot_guard


ORIGIN_HOME = "EMPIREMUSE_HOME"
ORIGIN_DEV_RUNTIME = ".runtime de desenvolvimento"
ORIGIN_CWD = "pasta atual"


def _announce_runtime_dir(runtime_dir, origin: str, report=None) -> None:
    """Uma linha na subida com o diretório escolhido e de onde ele veio; nunca o conteúdo de arquivo algum.

    Com um ``BootReport`` a etapa fica guardada até o ``configure_logging`` (o log ainda não existe aqui); sem ele, vai direto ao log.
    """
    detail = f"{runtime_dir} (origem: {origin})"
    if report is not None:
        report.defer("Pasta de execução", "ok", detail)
    else:
        logging.getLogger("empiremuse.boot").info("Pasta de execução: %s", detail)


def enter_runtime_dir(report=None) -> None:
    """Muda o cwd para o `RUNTIME_DIR`, onde o bot grava dados e caches relativos ao diretório de trabalho.

    Plataformas como o Pterodactyl criam o container sem `WorkingDir`, e o cwd da imagem não é gravável pelo
    usuário do nó, então vale o `EMPIREMUSE_HOME`. Em desenvolvimento, sem a variável, vale a `.runtime/` do
    projeto quando ela existe. Sem nenhum dos dois nada muda (o `RUNTIME_DIR` já é o cwd).
    """
    home_is_set = bool(os.environ.get("EMPIREMUSE_HOME"))
    runtime_dir = paths.get_runtime_dir()

    if not home_is_set and paths.dev_runtime_dir() is None:
        _announce_runtime_dir(runtime_dir, ORIGIN_CWD, report)
        return

    try:
        os.chdir(runtime_dir)
    except OSError as e:
        if home_is_set:
            message = (
                f"Não foi possível entrar no diretório indicado por EMPIREMUSE_HOME ({runtime_dir}): {e.strerror}. "
                "Crie a pasta ou corrija a variável EMPIREMUSE_HOME."
            )
        else:
            message = (
                f"Não foi possível entrar na pasta de execução de desenvolvimento ({runtime_dir}): {e.strerror}. "
                "Ajuste as permissões da pasta .runtime ou remova-a para o bot usar o diretório atual."
            )
        raise SystemExit(message) from e

    if home_is_set:
        # Se EMPIREMUSE_HOME era relativo, o valor passaria a apontar para outro lugar depois do chdir.
        os.environ["EMPIREMUSE_HOME"] = str(runtime_dir)

    _announce_runtime_dir(runtime_dir, ORIGIN_HOME if home_is_set else ORIGIN_DEV_RUNTIME, report)


def __getattr__(name: str) -> Any:
    """``BotPool`` só é importado quando preciso: o ``boot_guard`` roda antes de o código do bot carregar."""
    if name == "BotPool":
        from empiremuse.utils.client import BotPool

        return BotPool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def install_log_hooks() -> None:
    """Instala os ganchos de exceção com redação (``logs.hooks``) antes de qualquer outra coisa.

    O import é tardio e tolerante: um erro no pacote de log do código recém-atualizado não pode impedir o ``boot_guard`` de voltar
    ao commit anterior (os ganchos padrão do Python seguem valendo).
    """
    try:
        from empiremuse.logs.hooks import install_early_hooks

        install_early_hooks()
    except Exception as e:
        sys.stderr.write(f"[log] ganchos de exceção não instalados ({type(e).__name__})\n")


def new_boot_report():
    """O relatório de subida que guarda as etapas de antes do log; ``None`` se o pacote de log não importa (o boot segue)."""
    try:
        from empiremuse.logs.boot import BootReport

        return BootReport()
    except Exception:
        return None


def harden_process() -> tuple[str, str]:
    """Endurece o processo (``process_hardening``) antes de tudo, inclusive de qualquer processo filho, e devolve o status e o
    detalhe da etapa para o relatório de subida (o log ainda não existe aqui). Tolerante como os ganchos: um erro de import no
    código recém-atualizado não pode impedir o ``boot_guard``; vira ``warn`` e uma linha no stderr."""
    try:
        from empiremuse.process_hardening import hardening_step, mark_non_dumpable

        return hardening_step(mark_non_dumpable())
    except Exception as e:
        sys.stderr.write(f"[boot] endurecimento do processo não aplicado ({type(e).__name__})\n")
        return "warn", f"endurecimento não aplicado ({type(e).__name__})"


def record_hardening(report, hardening: tuple[str, str]) -> None:
    """Guarda a etapa ``Processo`` no relatório de subida; sem relatório, um aviso vai para o stderr (nunca some)."""
    status, detail = hardening
    if report is not None:
        report.defer("Processo", status, detail)
    elif status != "ok":
        sys.stderr.write(f"[boot] {detail}\n")


def main():
    hardening = harden_process()
    install_log_hooks()
    report = new_boot_report()
    record_hardening(report, hardening)
    enter_runtime_dir(report)
    # Antes de importar o bot: um erro de import no código recém-atualizado não pode impedir a volta ao commit anterior.
    boot_guard.run(paths.get_runtime_dir(), paths.PROJECT_DIR, time.time())
    if report is not None:
        report.defer("Python", "ok", python_version())
    pool_class = globals().get("BotPool") or __getattr__("BotPool")
    pool = pool_class()
    pool.boot_report = report  # o BotPool.setup emite as etapas guardadas logo depois do configure_logging
    pool.setup()
    sys.exit(pool.exit_code)


if __name__ == "__main__":
    main()
