"""Tipos e base comuns dos formulários de configuração (versão otimista, contexto, estado exibido)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple, Optional, Sequence

from empiremuse.i18n import I18n, get_i18n
from empiremuse.rules.i18n import Translator
from empiremuse.utils.db import DBModel

Args = Mapping[str, Sequence[str]]

PANEL_LOCALE = "pt-BR"  # o painel fala um idioma só até a F3 (contrato i18n §5)

HEX16 = re.compile(r"[0-9a-f]{16}", re.ASCII)
DIGITS = re.compile(r"[0-9]{1,20}", re.ASCII)
SAVED_TEXT = "Configurações salvas."
CONFLICT_TEXT = "Estas configurações foram alteradas em outro lugar. Recarregue a página para ver os valores atuais."
INVALID_TEXT = "Corrija os campos destacados e tente novamente."
VERSION_TEXT = "Não foi possível validar o formulário. Recarregue a página e tente novamente."
KIND_OK = "sucesso"
KIND_ERROR = "erro"


def version_of(fields: Mapping[str, Any]) -> str:
    """Versão do formulário: 16 primeiros hex do SHA-256 do JSON canônico dos campos dele."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def first(args: Args, name: str) -> Optional[str]:
    values = args.get(name)
    return values[0] if values else None


def flag(args: Args, name: str) -> bool:
    """Checkbox: presente = marcado; ausente = desmarcado."""
    return bool(args.get(name))


def panel_i18n(pool: Any) -> I18n:
    """``pool.i18n`` (o único do processo); sem pool, o mesmo objeto por ``get_i18n()``."""
    i18n = getattr(pool, "i18n", None)
    return i18n if i18n is not None else get_i18n()


def panel_translator(pool: Any) -> Translator:
    """O ``_`` dos templates e das mensagens do painel: o ``Translator`` de ``PANEL_LOCALE``."""
    return panel_i18n(pool).for_locale(PANEL_LOCALE)


def load_json_object(raw: str) -> Optional[dict]:
    """JSON de importação: o objeto, ou ``None`` se não for JSON válido ou não for um objeto."""
    try:
        data = json.loads(raw)
    except ValueError, RecursionError:
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class FormContext:
    """Quem edita o quê: bot escolhido (acesso a dados), servidor do cache desse bot e usuário do painel."""

    pool: Any
    bot: Any
    guild: Any
    guild_id: int
    user_id: str

    @property
    def bot_id(self) -> str:
        return str(self.bot.user.id)


class ForbiddenAction(Exception):
    """O usuário gerencia o servidor, mas não tem a permissão específica desta ação (vira 403)."""


class ParseResult(NamedTuple):
    new: dict[str, Any]
    errors: dict[str, str]
    echo: dict[str, Any]


@dataclass(frozen=True)
class Formulario:
    """Estado de um formulário entregue ao template (nomes em pt-BR acordados com o Frontend)."""

    key: str
    form_id: str
    template: str
    acao: str
    versao: str
    valores: dict[str, Any]
    erros: dict[str, str] = field(default_factory=dict)
    mensagem: Optional[str] = None
    mensagem_tipo: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)
    alias: str = ""

    def __getattr__(self, name: str) -> Any:
        """Atributos extras para os templates (ex.: ``estado.acao_erro``, ``ouvir_junto.itens``)."""
        if name.startswith("_") or name in ("extras", "valores"):
            raise AttributeError(name)
        for source in (self.extras, self.valores):
            if name in source:
                return source[name]
        raise AttributeError(name)


class SettingsForm:
    """Um formulário de configuração: leitura, validação, aplicação sobre uma cópia e efeitos em memória."""

    key = ""
    category = ""
    scope_kind = "bot"  # "bot", "global" (dados globais do servidor) ou "servidor" (doc único: pool.servers)
    fields: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()  # campos que identificam este formulário num POST

    template_alias = ""  # nome extra da variável do formulário no template (contrato com o Frontend)

    @property
    def template(self) -> str:
        return f"form_{self.key}.html"

    @property
    def form_id(self) -> str:
        return "form-" + self.key.replace("_", "-")

    def scope(self, ctx: FormContext) -> str:
        if self.scope_kind == "bot":
            return ctx.bot_id
        return "servidor" if self.scope_kind == "servidor" else "global"

    async def load(self, ctx: FormContext) -> dict:
        """Documento do cache do banco: somente leitura (nunca mutar nem regravar este objeto)."""
        if self.scope_kind == "servidor":
            return await ctx.pool.servers.settings(ctx.guild_id)
        if self.scope_kind == "bot":
            return await ctx.bot.get_data(ctx.guild_id, db_name=DBModel.guilds)
        return await ctx.bot.get_global_data(ctx.guild_id, db_name=DBModel.guilds)

    async def store(self, ctx: FormContext, doc: dict, before: dict) -> None:
        """Grava ``doc`` (cópia alterada de ``before``, o que ``load`` devolveu). No escopo servidor vai só o que mudou."""
        if self.scope_kind == "servidor":
            changes = {name: value for name, value in doc.items() if before.get(name) != value}
            if changes:
                await ctx.pool.servers.patch(ctx.guild_id, "settings", changes)
        elif self.scope_kind == "bot":
            await ctx.bot.update_data(ctx.guild_id, doc, db_name=DBModel.guilds)
        else:
            await ctx.bot.update_global_data(ctx.guild_id, doc, db_name=DBModel.guilds)

    def extract(self, doc: dict) -> dict[str, Any]:
        raise NotImplementedError

    def aux(self, ctx: FormContext, doc: dict) -> dict[str, Any]:
        """Dados derivados só para exibir/validar (fora da ``versao``); chegam a display/extras/parse junto de ``current``."""
        return {}

    async def guard(self, ctx: FormContext, args: Args) -> None:
        """Permissão extra de uma ação; levanta ``ForbiddenAction`` (antes de qualquer leitura de dados)."""
        return None

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        raise NotImplementedError

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        raise NotImplementedError

    def display(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return dict(current)

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        return None
