"""Base das seções da conta do usuário (``/painel/conta``): contexto, recusas e contrato de cada seção.

Os dados são sempre os do usuário da sessão (``DBModel.users``, escopo global). Segredos (``lastfm.sessionkey`` e
``token``) nunca entram em ``version_fields``/``extras``/``defaults``: só ``values`` os compara, internamente.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, NamedTuple, Optional

from empiremuse.panel.services.base import Args
from empiremuse.utils.music.user_limits import quota_for

ACCOUNT_PATH = "/painel/conta"
ACTION_UNKNOWN_TEXT = "Ação desconhecida."


@dataclass(frozen=True)
class AccountContext:
    """Bot de entrada (primeiro bot pronto do pool) e id ``int`` do usuário da sessão."""

    pool: Any
    bot: Any
    user_id: int

    @property
    def user(self) -> Any:
        """Objeto mínimo aceito por ``bot.is_owner`` (só o ``id`` é usado)."""
        return SimpleNamespace(id=self.user_id)


class Refusal(NamedTuple):
    status: int
    message: str


class Parsed(NamedTuple):
    new: dict[str, Any]
    errors: dict[str, str]
    echo: dict[str, Any]
    shown: dict[str, Any] = {}  # extras só desta resposta (ex.: o token recém-gerado)


def is_web_link(url: str) -> bool:
    """Só http/https pode virar ``<a href>``."""
    return url.lower().startswith(("http://", "https://"))


async def effective_limit(ctx: AccountContext, config_key: str) -> int:
    """Limite aplicável ao usuário: 0 = ilimitado (config 0 ou dono do bot), como no comando; lido a cada chamada."""
    return await quota_for(ctx.bot, ctx.user, config_key)


class AccountSection:
    """Uma seção da página: formulários (``actions``), versão própria e regras. Subclasses implementam o que muda."""

    key = ""
    actions: tuple[str, ...] = ()
    own_store = False  # True: os dados não estão no documento do usuário; a seção implementa ``submit_own``

    @property
    def template(self) -> str:
        return f"bloco_conta_{self.key}.html"

    @property
    def form_id(self) -> str:
        return f"conta-{self.key}"

    def values(self, doc: dict) -> dict[str, Any]:
        """Campos gravados pela seção (comparação interna e nomes da auditoria; nunca vão para a tela)."""
        raise NotImplementedError

    def version_fields(self, doc: dict) -> dict[str, Any]:
        """Tudo que compõe a ``versao`` (sem segredos)."""
        return self.values(doc)

    def defaults(self, doc: dict) -> dict[str, Any]:
        return {}

    async def extras(self, ctx: AccountContext, doc: dict) -> dict[str, Any]:
        return {}

    async def guard(self, ctx: AccountContext, doc: dict, action: str) -> Optional[Refusal]:
        """Recusa a ação antes de olhar a versão (conta não vinculada, recurso indisponível)."""
        return None

    async def parse(self, ctx: AccountContext, doc: dict, action: str, args: Args) -> Parsed:
        raise NotImplementedError

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        raise NotImplementedError

    async def submit_own(self, service: Any, ctx: AccountContext, action: str, args: Args) -> Any:
        """Só para ``own_store``: grava no armazenamento da própria seção e devolve o ``AccountOutcome``."""
        raise NotImplementedError

    async def before_store(self, ctx: AccountContext, doc: dict) -> None:
        """Efeito que precisa acontecer ANTES da gravação (documento ainda sem a mudança)."""
        return None

    async def effects(self, ctx: AccountContext, new: dict[str, Any]) -> None:
        """Efeitos em memória depois da gravação."""
        return None
