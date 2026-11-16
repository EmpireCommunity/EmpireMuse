"""Rádios favoritas que dependem de um servidor (B9, contrato T-456 rev. 9 §4 ``[r9-fav]``): ``GET /guilds/{g}/radio-favorites`` e
``POST``/``DELETE`` de ``…/radio-favorites/server`` (a lista do servidor, de quem gerencia) e ``…/radio-favorites/mine`` (as ``custom``/``genre`` do próprio
usuário NESTE servidor). A lista pessoal global (ao vivo e filtros) fica em ``/account/radio-favorites``.

Ordem dos erros nas escritas: ``401`` (sessão) → ``404`` (servidor, reconferido agora) → ``403`` (premium do servidor para ``custom``/``genre``;
``FavoriteForbidden`` do store) → ``400`` de forma (``kind`` fora do conjunto da rota, ``id`` fora do formato) → ``Idempotency-Key`` → domínio: já está =
``200`` sem consultar o diretório; cheia = ``400`` ``fields:["limit"]`` antes de qualquer consulta externa; estação ou gênero que não existe (inclusive a
``custom`` de OUTRO servidor) = o mesmo ``404``; diretório ou banco fora = ``503``, nunca ``404``. Sem versão (o store acrescenta por CAS e o resultado é função
do estado). O usuário e o servidor vêm só da sessão e do caminho; o nome nunca vem do corpo. Nenhum log leva uuid, nome nem usuário + estação.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from empiremuse.api.errors import FORBIDDEN, ApiError, not_found, validation_failed
from empiremuse.api.idempotency import Reply
from empiremuse.api.radio_base import GUILD_ID, RadioRoute, radio_endpoint
from empiremuse.api.radio_data import safe_stored_text, safe_text
from empiremuse.api.writes import MINE_KINDS, SERVER_KINDS, RadioFavorite, parse_radio_favorite
from empiremuse.panel.services.base import FormContext
from empiremuse.panel.services.radio import can_write
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.favorites import SERVER_FAVORITES_MAX, AddResult, FavoriteForbidden, FavoriteNotFound
from empiremuse.utils.radio.custom_favorites import MAX_CUSTOM_FAVORITES
from empiremuse.utils.radio.favorite_refs import is_genre, resolve_refs
from empiremuse.utils.usage import record_panel_action


class GuildUser(Protocol):
    """O que as listas precisam: o servidor do CAMINHO e o usuário da SESSÃO (``FormContext`` de gerente ou ``MemberContext`` de membro)."""

    @property
    def guild_id(self) -> int: ...

    @property
    def user_id(self) -> Any: ...


ACTION_LABEL = "radios/favoritas"  # rótulo FIXO do registro de uso do painel (nunca o id nem o nome)
PREMIUM_KINDS = ("custom", "genre")  # estação do servidor e gênero pronto são recurso premium; a ao vivo não


def ref_item(ref: str, station: Any) -> dict[str, Any]:
    """Uma entrada ``custom``/``genre`` (só o id é guardado), com a estação relida agora. ``name`` é texto tratado (``null`` no gênero: o painel traduz o
    ``id``); ``missing`` quando já não resolve (estação apagada, gênero fora do catálogo): segue listada e removível."""
    kind = "genre" if is_genre(ref) else "custom"
    entry: dict[str, Any] = {
        "kind": kind,
        "id": ref,
        "name": safe_text(station.name) if station is not None and kind == "custom" else None,
    }
    if station is None:
        entry["missing"] = True
    return entry


async def ref_items(hub: Any, ctx: GuildUser, refs: list[str]) -> list[dict[str, Any]]:
    """As entradas de uma lista: as ``custom`` numa leitura só das estações do servidor (nunca uma por favorita); o gênero vem do catálogo, sem banco.
    A de outro servidor não está nessa leitura e sai como ``missing``, igual a uma apagada."""
    return [ref_item(ref, station) for ref, station in await resolve_refs(hub, ctx.guild_id, refs)]


def envelope(items: list[dict[str, Any]], maximum: int) -> dict[str, Any]:
    return {"items": items, "max": maximum, "total": len(items)}


async def server_listing(hub: Any, ctx: GuildUser) -> dict[str, Any]:
    """A lista do servidor: as ao vivo (nome guardado, já tratado) e depois as ``custom``/``genre``, cada grupo na ordem de inserção."""
    stations, refs = await hub.favorites.list_all(ctx.guild_id)
    items: list[dict[str, Any]] = [
        {"kind": "station", "id": f.uuid, "name": safe_stored_text(f.name)} for f in stations
    ]  # nome do diretório: guardado escapado
    items.extend(await ref_items(hub, ctx, refs))
    return envelope(items, SERVER_FAVORITES_MAX)


async def mine_listing(hub: Any, ctx: GuildUser) -> dict[str, Any]:
    """As ``custom``/``genre`` do PRÓPRIO usuário neste servidor (a chave do documento é montada aqui, da sessão e do caminho)."""
    ids = await hub.custom_favorites.list(ctx.guild_id, int(ctx.user_id))
    return envelope(await ref_items(hub, ctx, ids), MAX_CUSTOM_FAVORITES)


async def favorite_change_reply(
    change: Callable[[], Awaitable[Any]], listing: Callable[[], Awaitable[dict[str, Any]]], usage: Any
) -> Reply:
    """O miolo de TODA escrita de favorita (gerente e membro, revisão do PR 264): ``change`` adiciona (``AddResult``) ou remove; a resposta é a lista
    atualizada. Cheia = ``400 limit``; recusa do store = ``403``; estação desconhecida ou de outro servidor = ``404``; armazém fora = ``503``. O status sai
    sempre no registro de uso (``usage``: o ``FormContext`` ou o objeto só de uso do membro), com o rótulo fixo."""
    status = 200
    try:
        if await change() is AddResult.FULL:
            raise validation_failed("limit")
        return Reply(200, await listing())
    except ApiError as error:  # o 400 de "cheia" e os demais erros da API passam como estão, com o próprio status
        status = error.status_code
        raise
    except FavoriteForbidden:
        status = 403
        raise ApiError(403, FORBIDDEN) from None
    except FavoriteNotFound:
        status = 404
        raise not_found() from None
    except RadioUnavailable:
        status = 503
        raise
    except Exception:
        status = 500
        raise
    finally:
        record_panel_action(usage, ACTION_LABEL, status)


async def mine_add(hub: Any, ctx: GuildUser, favorite: RadioFavorite) -> AddResult:
    """Adiciona às ``custom``/``genre`` do usuário da SESSÃO no servidor do CAMINHO; a de outro servidor é igual a uma que não existe."""

    async def exists(ref: str) -> bool:
        return await hub.stations.get(ctx.guild_id, ref) is not None

    return await hub.custom_favorites.add(ctx.guild_id, int(ctx.user_id), favorite.id, exists)


async def mine_remove(hub: Any, ctx: GuildUser, favorite: RadioFavorite) -> bool:
    return await hub.custom_favorites.remove(ctx.guild_id, int(ctx.user_id), favorite.id)


class FavoriteListRoute(RadioRoute):
    """Base das listas: monta a resposta ``{items, max, total}`` e traduz as recusas do store."""

    kinds: tuple[str, ...] = ()

    async def listing(self, ctx: FormContext) -> dict[str, Any]:
        raise NotImplementedError

    async def exists(self, ctx: FormContext, ref: str) -> bool:
        """``custom`` (do SERVIDOR do caminho) ou gênero do catálogo; a de outro servidor é igual a uma que não existe."""
        return await self.hub(ctx).stations.get(ctx.guild_id, ref) is not None

    async def add(self, ctx: FormContext, favorite: RadioFavorite) -> AddResult:
        raise NotImplementedError

    async def remove(self, ctx: FormContext, favorite: RadioFavorite) -> bool:
        raise NotImplementedError

    @radio_endpoint
    async def post(self, guild_id: str) -> None:
        ctx, _ = await self.guild_access(
            guild_id, write=True
        )  # o premium só é lido (fresco) para custom/genre: estação ao vivo não o exige
        body = self.json_body()
        kind = body.get("kind")
        if isinstance(kind, str) and kind in PREMIUM_KINDS and kind in self.kinds:
            if not can_write(await self.hub(ctx).premium.fresh(ctx.guild_id)):  # premium AGORA (sem o cache de 30 s)
                record_panel_action(ctx, ACTION_LABEL, 403)
                raise ApiError(403, FORBIDDEN)
        favorite = parse_radio_favorite(body, self.kinds)

        async def work() -> Reply:
            return await favorite_change_reply(lambda: self.add(ctx, favorite), lambda: self.listing(ctx), ctx)

        await self.run_write(body, work)

    @radio_endpoint
    async def delete(self, guild_id: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=True)  # remover nunca exige premium: não o lê
        query = self.query_arguments("kind", "id")
        favorite = parse_radio_favorite(query, self.kinds)

        async def work() -> Reply:  # remover o que não está = 200, como na conta
            return await favorite_change_reply(lambda: self.remove(ctx, favorite), lambda: self.listing(ctx), ctx)

        await self.run_write(query, work)


class ServerFavoritesHandler(FavoriteListRoute):
    """``POST``/``DELETE /guilds/{g}/radio-favorites/server``: a lista do servidor; só quem gerencia (G agora e o store com o ator da SESSÃO)."""

    kinds = SERVER_KINDS

    async def listing(self, ctx: FormContext) -> dict[str, Any]:
        return await server_listing(self.hub(ctx), ctx)

    async def add(self, ctx: FormContext, favorite: RadioFavorite) -> AddResult:
        favorites, actor = self.hub(ctx).favorites, int(ctx.user_id)  # o ator é o da sessão: nunca de corpo
        if favorite.kind == "station":
            return await favorites.add_live(ctx.guild_id, actor, favorite.id, self.hub(ctx).directory.by_uuid_strict)
        return await favorites.add_ref(ctx.guild_id, actor, favorite.id, lambda ref: self.exists(ctx, ref))

    async def remove(self, ctx: FormContext, favorite: RadioFavorite) -> bool:
        favorites, actor = self.hub(ctx).favorites, int(ctx.user_id)
        if favorite.kind == "station":
            return await favorites.remove(ctx.guild_id, actor, favorite.id)
        return await favorites.remove_ref(ctx.guild_id, actor, favorite.id)


class MyFavoritesHandler(FavoriteListRoute):
    """``POST``/``DELETE /guilds/{g}/radio-favorites/mine``: as ``custom``/``genre`` do PRÓPRIO usuário neste servidor (a chave do documento é montada aqui)."""

    kinds = MINE_KINDS

    async def listing(self, ctx: FormContext) -> dict[str, Any]:
        return await mine_listing(self.hub(ctx), ctx)

    async def add(self, ctx: FormContext, favorite: RadioFavorite) -> AddResult:
        return await mine_add(self.hub(ctx), ctx, favorite)

    async def remove(self, ctx: FormContext, favorite: RadioFavorite) -> bool:
        return await mine_remove(self.hub(ctx), ctx, favorite)


class GuildRadioFavoritesHandler(RadioRoute):
    """``GET /guilds/{g}/radio-favorites`` → ``{server: {items, max, total}, mine: {items, max, total}}``. ``mine`` traz só as ``custom``/``genre`` DESTE servidor
    do próprio usuário (as ao vivo e os filtros vêm de ``GET /account/radio-favorites``). Nenhuma rota lê a lista pessoal de outro usuário."""

    @radio_endpoint
    async def get(self, guild_id: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=False)
        hub = self.hub(ctx)
        self.write({"server": await server_listing(hub, ctx), "mine": await mine_listing(hub, ctx)})


def radio_favorite_routes() -> list[tuple[str, Any, str]]:
    """(expressão do caminho sob o prefixo, handler, caminho do OpenAPI)."""
    return [
        (f"/guilds/{GUILD_ID}/radio-favorites", GuildRadioFavoritesHandler, "/guilds/{guild_id}/radio-favorites"),
        (
            f"/guilds/{GUILD_ID}/radio-favorites/server",
            ServerFavoritesHandler,
            "/guilds/{guild_id}/radio-favorites/server",
        ),
        (f"/guilds/{GUILD_ID}/radio-favorites/mine", MyFavoritesHandler, "/guilds/{guild_id}/radio-favorites/mine"),
    ]
