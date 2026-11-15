"""Erro de limpeza de memória de um usuário (sessões do painel): fica em ``rules`` para o apagamento e o banimento o importarem sem puxar o painel."""

from __future__ import annotations


class UserCleanupError(Exception):
    """As sessões do usuário foram destruídas, mas algum ouvinte não conseguiu limpar os dados dele. Levantada só depois de
    todos os ouvintes rodarem; o apagamento a trata como limpeza parcial (``ok=False``)."""

    def __init__(self, destroyed: int, failures: int) -> None:
        super().__init__(f"{failures} ouvinte(s) falharam")
        self.destroyed = destroyed
        self.failures = failures
