"""Categoria Links → "Playlists do servidor" (item 17): a lista única do servidor, lida e gravada SÓ pelo store.

Escopo servidor (``pool.servers.playlists``), não bot. O formulário leva o ``rev`` do documento único (P-V7): uma versão
velha é recusada pelo serviço (409) e, na janela entre ler e gravar, pelo ``expected_rev`` do store. A validação por
campo acontece antes, com as mesmas regras puras do comando (``rules/playlists``); a importação só acrescenta e mostra
o relatório do que entrou e do que foi pulado.
"""

from __future__ import annotations

import logging
import re
from typing import Any, NamedTuple, Optional

from empiremuse.panel.services.base import Args, FormContext, ParseResult, SettingsForm, first
from empiremuse.rules import limits
from empiremuse.rules.errors import (
    DESCRIPTION_INVALID,
    DESCRIPTION_TOO_LONG,
    FILE_TOO_LARGE,
    FILE_TOO_MANY,
    GENRES_INVALID,
    NAME_INVALID,
    NAME_TAKEN,
    NAME_TOO_LONG,
    ORDER_INVALID,
    PLAYLIST_NOT_FOUND,
    QUOTA_FULL,
    URL_INVALID,
    URL_TAKEN,
    URL_TOO_LONG,
    RuleError,
)
from empiremuse.rules.playlists import (
    DUP_IN_FILE,
    DUP_NAME,
    DUP_URL,
    INVALID,
    ImportReport,
    ParsedFile,
    Playlist,
    add_item,
    check_playlist,
    edit_item,
    find_item,
    parse_file,
    plan_import,
    remove_item,
    to_doc,
)
from empiremuse.rules.playlists import QUOTA_FULL as SKIP_QUOTA
from empiremuse.utils.servers import ServerConfigConflict

logger = logging.getLogger(__name__)

ACTIONS = ("adicionar", "editar", "remover", "importar")
GENRE_SPLIT = re.compile(r"[,;]")

MSG_ACTION = "Ação desconhecida."
MSG_NAME = f"O nome deve ter de {limits.PLAYLIST_NAME_MIN} a {limits.PLAYLIST_NAME_MAX} caracteres, sem caracteres de controle."
MSG_NAME_TAKEN = "Já existe uma playlist com esse nome."
MSG_NAME_UNKNOWN = "Playlist não encontrada. Recarregue a página para ver a lista atual."
MSG_URL = f"Informe um endereço http(s) válido de {limits.PLAYLIST_URL_MIN} a {limits.PLAYLIST_URL_MAX} caracteres."
MSG_URL_TAKEN = "Já existe uma playlist com esse link."
MSG_DESCRIPTION = (
    f"A descrição deve ficar vazia ou ter de {limits.PLAYLIST_DESCRIPTION_MIN} a "
    f"{limits.PLAYLIST_DESCRIPTION_MAX} caracteres."
)
MSG_GENRES = (
    f"Até {limits.PLAYLIST_GENRES_MAX} gêneros de {limits.PLAYLIST_GENRE_MIN} a {limits.PLAYLIST_GENRE_MAX} "
    "caracteres, separados por vírgula, sem repetir."
)
MSG_ORDER = f"A ordem deve ficar vazia ou ser um número de 1 a {limits.PLAYLIST_ORDER_MAX}."
MSG_FULL = f"A lista já tem o máximo de {limits.MAX_SERVER_PLAYLISTS} playlists; remova alguma para adicionar outra."
MSG_IMPORT_JSON = "O conteúdo precisa ser um JSON válido, no formato exportado por este painel ou pelo bot."
MSG_IMPORT_LARGE = f"O conteúdo passa de {limits.PLAYLIST_FILE_MAX_BYTES // 1024} KiB."
MSG_IMPORT_MANY = f"O arquivo tem mais de {limits.PLAYLIST_FILE_MAX_ITEMS} playlists."
MSG_IMPORT_EMPTY = "O arquivo não tem nenhuma playlist válida."
MSG_IMPORT_NOTHING = "Nenhuma playlist nova para adicionar"

_FIELD_OF = {
    NAME_INVALID: "nome",
    NAME_TOO_LONG: "nome",
    NAME_TAKEN: "nome",
    QUOTA_FULL: "acao",  # com a lista cheia o formulário de adicionar nem aparece: o aviso vai no topo do bloco
    URL_INVALID: "url",
    URL_TOO_LONG: "url",
    URL_TAKEN: "url",
    DESCRIPTION_INVALID: "descricao",
    DESCRIPTION_TOO_LONG: "descricao",
    GENRES_INVALID: "generos",
    ORDER_INVALID: "ordem",
    PLAYLIST_NOT_FOUND: "acao",
}
_MESSAGE_OF = {
    NAME_INVALID: MSG_NAME,
    NAME_TOO_LONG: MSG_NAME,
    NAME_TAKEN: MSG_NAME_TAKEN,
    QUOTA_FULL: MSG_FULL,
    URL_INVALID: MSG_URL,
    URL_TOO_LONG: MSG_URL,
    URL_TAKEN: MSG_URL_TAKEN,
    DESCRIPTION_INVALID: MSG_DESCRIPTION,
    DESCRIPTION_TOO_LONG: MSG_DESCRIPTION,
    GENRES_INVALID: MSG_GENRES,
    ORDER_INVALID: MSG_ORDER,
    PLAYLIST_NOT_FOUND: MSG_NAME_UNKNOWN,
}
_REASON_TEXT = {
    INVALID: "inválida",
    DUP_NAME: "nome já existe",
    DUP_URL: "link já existe",
    DUP_IN_FILE: "repetida no arquivo",
    SKIP_QUOTA: "limite de playlists do servidor",
}
LIMITES = {
    "nome_min": limits.PLAYLIST_NAME_MIN,
    "nome_max": limits.PLAYLIST_NAME_MAX,
    "descricao_min": limits.PLAYLIST_DESCRIPTION_MIN,
    "descricao_max": limits.PLAYLIST_DESCRIPTION_MAX,
    "url_max": limits.PLAYLIST_URL_MAX,
    "generos_max": limits.PLAYLIST_GENRES_MAX,
    "genero_max": limits.PLAYLIST_GENRE_MAX,
    "ordem_max": limits.PLAYLIST_ORDER_MAX,
    "icone_max": limits.PLAYLIST_ICON_MAX,
    "ordem_digitos": len(str(limits.PLAYLIST_ORDER_MAX)),
}


class PlaylistView(NamedTuple):
    ordem: int
    nome: str
    url: str
    descricao: str
    generos: tuple[str, ...]
    icone: str | None = None


class RelatorioView(NamedTuple):
    """Relatório da importação para o template: quantas entraram e, por nome, por que cada uma foi pulada."""

    adicionadas: int
    ignoradas: tuple[tuple[str, str], ...]


def _report_view(report: ImportReport) -> RelatorioView:
    return RelatorioView(
        len(report.added), tuple((skip.name, _REASON_TEXT.get(skip.reason, skip.reason)) for skip in report.skipped)
    )


def _error(field: str, message: str, echo: dict[str, Any]) -> ParseResult:
    return ParseResult({}, {field: message}, echo)


def _rule_error(error: RuleError, echo: dict[str, Any]) -> ParseResult:
    return _error(_FIELD_OF.get(error.code, "acao"), _MESSAGE_OF.get(error.code, MSG_ACTION), echo)


def split_genres(raw: str) -> list[str]:
    return [part.strip() for part in GENRE_SPLIT.split(raw) if part.strip()]


def parse_order(raw: str, default: int) -> Optional[int]:
    """Ordem digitada: vazio = ``default``; senão um inteiro de 1 a ``PLAYLIST_ORDER_MAX``; ``None`` se inválida."""
    text = raw.strip()
    if not text:
        return default
    if not text.isascii() or not text.isdigit() or len(text) > LIMITES["ordem_digitos"]:
        return None
    value = int(text)
    return value if 1 <= value <= limits.PLAYLIST_ORDER_MAX else None


def _item_from(echo: dict[str, Any], default_order: int) -> Playlist | RuleError:
    """A playlist do formulário, validada pelas regras puras (``0`` na ordem = no fim da lista)."""
    order = parse_order(echo["ordem"], default_order)
    if order is None:
        return RuleError(ORDER_INVALID)
    raw = {
        "order": order,
        "name": echo["nome"],
        "description": echo["descricao"],
        "genres": split_genres(echo["generos"]),
        "url": echo["url"],
        "icon": echo["icone"],  # emoji inválido vira sem ícone, nunca erro
    }
    return check_playlist(raw)


def _docs(playlists: tuple[Playlist, ...]) -> list[dict[str, Any]]:
    return [to_doc(p) for p in playlists]


def _nothing_added_text(report: ImportReport) -> str:
    counts: dict[str, int] = {}
    for skip in report.skipped:
        counts[skip.reason] = counts.get(skip.reason, 0) + 1
    parts = ", ".join(f"{n} com {_REASON_TEXT.get(reason, reason)}" for reason, n in counts.items())
    return f"{MSG_IMPORT_NOTHING}: {parts}." if parts else f"{MSG_IMPORT_NOTHING}."


class LinksForm(SettingsForm):
    """Lê e grava só pelo store; ``load``/``store`` próprios porque a seção não é ``settings``."""

    key = "links"
    category = "links"
    scope_kind = "servidor"
    fields = ("playlists", "rev")
    markers = ("acao",)
    template_alias = "estado"

    @property
    def template(self) -> str:
        return "bloco_links.html"

    @property
    def form_id(self) -> str:
        return "bloco-links"

    # ------------------------------------------------------------------ dados

    async def load(self, ctx: FormContext) -> dict:
        items, rev = await ctx.pool.servers.playlists.snapshot(ctx.guild_id)
        return {"playlists": items, "rev": rev}

    def extract(self, doc: dict) -> dict[str, Any]:
        # ``rev`` entra na versão do formulário: qualquer gravação no servidor (comando, outro bot) invalida a página aberta.
        return {"playlists": _docs(doc["playlists"]), "rev": doc["rev"]}

    def aux(self, ctx: FormContext, doc: dict) -> dict[str, Any]:
        return {"itens": doc["playlists"], "relatorio": doc.get("relatorio")}

    def apply(self, doc: dict, new: dict[str, Any]) -> None:
        doc["acao"] = new["acao"]

    async def store(self, ctx: FormContext, doc: dict, before: dict) -> None:
        """Executa a ação no store com o ``rev`` lido; depois relê a lista e o ``rev`` para a resposta."""
        store = ctx.pool.servers.playlists
        gid, rev = ctx.guild_id, before["rev"]
        kind, payload = doc["acao"]
        if kind == "adicionar":
            result: Any = await store.add(gid, payload, expected_rev=rev)
        elif kind == "editar":
            result = await store.edit(gid, payload[0], payload[1], expected_rev=rev)
        elif kind == "remover":
            result = await store.remove(gid, payload, expected_rev=rev)
        else:
            result = await store.import_file(gid, payload, expected_rev=rev)
            doc["relatorio"] = _report_view(result)
        if isinstance(result, RuleError):
            # A lista é a mesma que ``parse`` validou (o ``rev`` conferiu): só regras divergentes chegariam aqui.
            logger.warning("Playlists do servidor [%s]: o store recusou %s (%s)", gid, kind, result.code)
            raise ServerConfigConflict
        doc["playlists"], doc["rev"] = await store.snapshot(gid)

    # ------------------------------------------------------------------ exibição

    def display(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        return {
            "acao_erro": None,
            "alvo_erro": None,
            "alvo": "",
            "nome": "",
            "url": "",
            "descricao": "",
            "generos": "",
            "icone": "",
            "ordem": "",
            "dados": "",
        }

    def extras(self, ctx: FormContext, current: dict[str, Any]) -> dict[str, Any]:
        items = tuple(PlaylistView(p.order, p.name, p.url, p.description, p.genres, p.icon) for p in current["itens"])
        return {
            "itens": items,
            "total": len(items),
            "maximo": limits.MAX_SERVER_PLAYLISTS,
            "pode_adicionar": len(items) < limits.MAX_SERVER_PLAYLISTS,
            "relatorio": current.get("relatorio"),
            "limites": LIMITES,
        }

    # ------------------------------------------------------------------ ações

    def parse(self, ctx: FormContext, current: dict[str, Any], args: Args) -> ParseResult:
        action = first(args, "acao") or ""
        target = (first(args, "alvo") or "").strip()
        echo = {
            "acao_erro": action,
            "alvo_erro": target if action == "editar" else None,
            "alvo": target,
            "nome": (first(args, "nome") or "").strip(),
            "url": (first(args, "url") or "").strip(),
            "descricao": (first(args, "descricao") or "").strip(),
            "generos": (first(args, "generos") or "").strip(),
            "icone": (first(args, "icone") or "").strip(),
            "icone_enviado": "icone" in args,  # sem o campo (página velha), editar mantém o ícone gravado
            "ordem": (first(args, "ordem") or "").strip(),
            "dados": first(args, "dados") or "",
        }
        if action not in ACTIONS:
            return _error("acao", MSG_ACTION, echo)
        handler = {"adicionar": self._add, "editar": self._edit, "remover": self._remove, "importar": self._import}
        return handler[action](current["itens"], current["rev"], echo)

    @staticmethod
    def _result(playlists: tuple[Playlist, ...], rev: int, action: tuple[str, Any], echo: dict) -> ParseResult:
        return ParseResult({"playlists": _docs(playlists), "rev": rev, "acao": action}, {}, echo)

    def _add(self, items: tuple[Playlist, ...], rev: int, echo: dict) -> ParseResult:
        item = _item_from(echo, default_order=0)
        if isinstance(item, RuleError):
            return _rule_error(item, echo)
        result = add_item(items, item)
        if isinstance(result, RuleError):
            return _rule_error(result, echo)
        return self._result(result, rev, ("adicionar", item), echo)

    def _edit(self, items: tuple[Playlist, ...], rev: int, echo: dict) -> ParseResult:
        existing = find_item(items, echo["alvo"])
        if existing is None:
            return _error("acao", MSG_NAME_UNKNOWN, echo)
        item = _item_from(echo, default_order=existing.order)
        if isinstance(item, RuleError):
            return _rule_error(item, echo)
        if not echo.get("icone_enviado", True):
            item = item._replace(icon=existing.icon)
        result = edit_item(items, existing.name, item)
        if isinstance(result, RuleError):
            return _rule_error(result, echo)
        return self._result(result, rev, ("editar", (existing.name, item)), echo)

    def _remove(self, items: tuple[Playlist, ...], rev: int, echo: dict) -> ParseResult:
        result = remove_item(items, echo["alvo"])
        if isinstance(result, RuleError):
            return _rule_error(result, echo)
        return self._result(result, rev, ("remover", echo["alvo"]), echo)

    def _import(self, items: tuple[Playlist, ...], rev: int, echo: dict) -> ParseResult:
        parsed = self._parse_import(echo["dados"])
        if isinstance(parsed, str):
            return _error("dados", parsed, echo)
        planned = plan_import(items, parsed.items)
        if not planned.added:
            return _error("dados", _nothing_added_text(ImportReport((), (*parsed.invalid, *planned.skipped))), echo)
        return self._result((*items, *planned.added), rev, ("importar", parsed), echo)

    @staticmethod
    def _parse_import(raw: str) -> ParsedFile | str:
        """O arquivo colado, pelas regras do comando; ou a mensagem de erro para o campo."""
        if not raw.strip():
            return MSG_IMPORT_JSON
        parsed = parse_file(raw, max_bytes=limits.PLAYLIST_FILE_MAX_BYTES)
        if isinstance(parsed, RuleError):
            return {FILE_TOO_LARGE: MSG_IMPORT_LARGE, FILE_TOO_MANY: MSG_IMPORT_MANY}.get(parsed.code, MSG_IMPORT_JSON)
        if not parsed.items and not parsed.invalid:
            return MSG_IMPORT_EMPTY
        return parsed

    async def effects(self, ctx: FormContext, current: dict[str, Any], new: dict[str, Any]) -> None:
        return None  # o embed ocioso de cada bot é atualizado pelo próprio store
