"""A visão de MEMBRO (T-660, contrato r10 §2 d; threat model T1-T12, CC1-CC4): rotas sob ``/api/v1/member`` para quem está no servidor sem gerenciá-lo.

- ``GET /member/guilds`` → ``{guilds: [{id, name, icon}]}``: os ids de ``session.member_guild_ids`` com bot pronto AGORA; nome e ícone do cache do bot,
  sem nenhuma chamada ao Discord.
- ``GET /member/guilds/{g}/radio-favorites`` → ``{guild, supporter, server, mine, catalog}``: a lista do servidor SÓ LEITURA, as ``custom``/``genre`` do
  próprio usuário neste servidor e o catálogo para favoritar (vazio sem o acesso de Apoiador).
- ``POST``/``DELETE /member/guilds/{g}/radio-favorites/mine``: as ``custom``/``genre`` do usuário (o mesmo store do ``MyFavoritesHandler``; a chave
  ``<servidor>:<usuário>`` é montada aqui, da sessão e do caminho). Não existe ``…/server`` sob ``/member``. A ⭐ de estação ao vivo é a rota da conta.

CC1 (lista de PERMISSÃO): ``MemberRoute`` deriva de ``DataHandler`` (nunca de ``RadioRoute``, cujos métodos de gerente seriam herdados), tem o ``hub``
próprio e só ``member_access``; ``guild_access`` (o único caminho de gerente do ``DataHandler``) levanta. O contexto é ``MemberContext`` (nunca o
``FormContext`` de gerente) e o registro de uso recebe só ``(pool, bot, guild)``.
CC4: "só estações da lista do servidor" e "sem filtros de local" são escopo da TELA (P2, P3): a rota da conta já aceita qualquer estação do diretório e
filtros para qualquer sessão; aqui nada afirma o contrário.
Ordem dos erros nas escritas: ``401`` → ``404`` (membro, agora) → ``503`` (Discord sem resposta definitiva) → ``403`` (Apoiador, para adicionar) → ``400``
de forma → ``Idempotency-Key`` → domínio (já está = ``200``; cheia = ``400 fields:["limit"]`` antes de conferir a estação; desconhecida ou de OUTRO
servidor = o mesmo ``404``; banco fora = ``503``). Nenhum log leva usuário, servidor e estação juntos.
"""

from __future__ import annotations

import logging
import re
from typing import Any, NamedTuple, NoReturn, Optional

from empiremuse.api.data import DataHandler
from empiremuse.api.errors import BOT_UNAVAILABLE, FORBIDDEN, ApiError, not_found, validation_failed
from empiremuse.api.handlers_radio_favorites import ACTION_LABEL, PREMIUM_KINDS, mine_listing, server_listing
from empiremuse.api.idempotency import Reply
from empiremuse.api.radio_base import GUILD_ID, radio_endpoint
from empiremuse.api.radio_data import safe_text
from empiremuse.api.writes import MINE_KINDS, parse_radio_favorite
from empiremuse.panel.authz import guild_icon_url, ready_bots_with_guild
from empiremuse.panel.member_authz import MemberAccess, MemberAccessDenied, MemberLookupUnavailable
from empiremuse.panel.services.radio import can_write
from empiremuse.rules.radio_station import genre_ref
from empiremuse.utils.radio.favorites import AddResult, FavoriteNotFound
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.stations import StationStore
from empiremuse.utils.usage import record_panel_action

logger = logging.getLogger(__name__)

# o ícone só no formato da CDN do Discord (T5): qualquer outra coisa sai como ``null``
_ICON_URL = re.compile(r"https://cdn\.discordapp\.com/icons/[0-9]{1,20}/(?:a_)?[0-9a-f]{32}\.png\?size=64", re.ASCII)


class MemberContext(NamedTuple):
    """O que uma rota de membro sabe: o pool, o servidor do CAMINHO e o usuário da SESSÃO. Não é ``FormContext``: nenhum serviço de gerente o aceita."""

    pool: Any
    guild_id: int
    user_id: str


class UsageScope(NamedTuple):
    """O objeto só de uso para ``record_panel_action`` (bot e servidor, nunca o usuário)."""

    pool: Any
    bot: Any
    guild: Any


def safe_icon(url: Optional[str]) -> Optional[str]:
    return url if isinstance(url, str) and _ICON_URL.fullmatch(url) else None


def guild_entry(access: MemberAccess) -> dict[str, Any]:
    return {"id": access.guild_id, "name": safe_text(access.name), "icon": safe_icon(access.icon_url)}


class MemberRoute(DataHandler):
    """Base de TODAS as rotas sob ``/member`` (o teste de invariante confere). Só ``member_access``; o caminho de gerente levanta (falha fechada)."""

    @staticmethod
    def hub(pool: Any) -> Any:
        hub = getattr(pool, "radio", None)
        if hub is None:
            raise ApiError(503, BOT_UNAVAILABLE)  # a rádio não está ligada neste bot
        return hub

    async def guild_access(self, guild_id: str, *, write: bool) -> NoReturn:  # type: ignore[override]
        """Caminho de GERENTE: nunca numa rota de membro (CC1). Chamado por engano = ``500`` e o nome da rota no log, nada autorizado."""
        logger.error("Rota de membro chamou o caminho de gerente (%s)", type(self).__name__)
        raise ApiError(500, BOT_UNAVAILABLE)

    async def member_access(self, guild_id: str, *, write: bool) -> tuple[MemberContext, MemberAccess, UsageScope]:
        """Membro agora (a escrita sempre pergunta ao Discord). Não membro, inexistente, sem bot ou fora das listas = o mesmo ``404``."""
        try:
            access = await self.ctx.member_authorizer.authorize(self.session, guild_id, fresh=write)
        except MemberAccessDenied:
            raise not_found() from None
        except MemberLookupUnavailable:
            raise ApiError(503, BOT_UNAVAILABLE) from None
        present = ready_bots_with_guild(self.ctx.pool, int(guild_id))
        if not present:
            raise not_found()
        bot, guild = present[0]
        ctx = MemberContext(pool=self.ctx.pool, guild_id=int(guild_id), user_id=self.session.user.id)
        return ctx, access, UsageScope(pool=self.ctx.pool, bot=bot, guild=guild)


class MemberGuildsHandler(MemberRoute):
    """``GET /member/guilds``: só o cache do bot (nenhuma chamada ao Discord, mesmo com 200 ids na sessão)."""

    async def get(self) -> None:
        guilds = []
        for guild_id in sorted(self.session.member_guild_ids, key=int):
            present = ready_bots_with_guild(self.ctx.pool, int(guild_id))
            if not present:
                continue
            guild = present[0][1]
            icon = guild_icon_url(guild_id, guild.icon.key if guild.icon else None)
            guilds.append(guild_entry(MemberAccess(guild_id=guild_id, name=guild.name, icon_url=icon)))
        self.write({"guilds": guilds})


async def catalog(hub: Any, ctx: MemberContext, supporter: bool) -> dict[str, Any]:
    """O que o membro pode favoritar: as estações do servidor (só id e nome) e os gêneros por grupo. Sem o acesso de Apoiador, vazio."""
    if not supporter:
        return {"stations": [], "genres": []}
    stations = await hub.stations.list_for_guild(ctx.guild_id)
    return {
        "stations": [{"kind": "custom", "id": station.id, "name": safe_text(station.name)} for station in stations],
        "genres": [
            {"group": group, "genres": [genre_ref(genre_id) for genre_id in ids]}
            for group, ids in StationStore.genre_groups()
        ],  # o id completo (``g:<id>``), o mesmo que a escrita recebe
    }


class MemberRadioFavoritesHandler(MemberRoute):
    """``GET /member/guilds/{g}/radio-favorites``: campos fechados (nunca artistas, playlists, ``updated_by``, rádios, canais, bots nem convites)."""

    @radio_endpoint
    async def get(self, guild_id: str) -> None:
        ctx, access, _ = await self.member_access(guild_id, write=False)
        hub = self.hub(ctx.pool)
        supporter = can_write(await hub.premium.get(ctx.guild_id))
        self.write(
            {
                "guild": guild_entry(access),
                "supporter": supporter,
                "server": await server_listing(hub, ctx),
                "mine": await mine_listing(hub, ctx),
                "catalog": await catalog(hub, ctx, supporter),
            }
        )


class MemberMineHandler(MemberRoute):
    """``POST``/``DELETE /member/guilds/{g}/radio-favorites/mine``: só ``custom``/``genre``; adicionar exige o servidor Apoiador AGORA."""

    @radio_endpoint
    async def post(self, guild_id: str) -> None:
        ctx, _, usage = await self.member_access(guild_id, write=True)
        hub = self.hub(ctx.pool)
        body = self.json_body()
        kind = body.get("kind")
        if isinstance(kind, str) and kind in PREMIUM_KINDS and not can_write(await hub.premium.fresh(ctx.guild_id)):
            record_panel_action(usage, ACTION_LABEL, 403)
            raise ApiError(403, FORBIDDEN)  # o Apoiador vem antes do corpo e antes do ``claim`` (T12)
        favorite = parse_radio_favorite(body, MINE_KINDS)

        async def exists(ref: str) -> bool:
            return (
                await hub.stations.get(ctx.guild_id, ref) is not None
            )  # a de OUTRO servidor é igual a uma que não existe

        async def work() -> Reply:
            status = 200
            try:
                if (
                    await hub.custom_favorites.add(ctx.guild_id, int(ctx.user_id), favorite.id, exists)
                    is AddResult.FULL
                ):
                    raise validation_failed("limit")
                return Reply(200, await mine_listing(hub, ctx))
            except ApiError as error:
                status = error.status_code
                raise
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

        await self.run_write(body, work)

    @radio_endpoint
    async def delete(self, guild_id: str) -> None:
        ctx, _, usage = await self.member_access(guild_id, write=True)  # remover nunca exige o Apoiador
        hub = self.hub(ctx.pool)
        query = self.query_arguments("kind", "id")
        favorite = parse_radio_favorite(query, MINE_KINDS)

        async def work() -> Reply:
            status = 200
            try:
                await hub.custom_favorites.remove(ctx.guild_id, int(ctx.user_id), favorite.id)  # o que não está = 200
                return Reply(200, await mine_listing(hub, ctx))
            except ApiError as error:
                status = error.status_code
                raise
            except RadioUnavailable:
                status = 503
                raise
            except Exception:
                status = 500
                raise
            finally:
                record_panel_action(usage, ACTION_LABEL, status)

        await self.run_write(query, work)


def member_routes() -> list[tuple[str, Any, str]]:
    """(expressão do caminho sob o prefixo, handler, caminho do OpenAPI)."""
    return [
        ("/member/guilds", MemberGuildsHandler, "/member/guilds"),
        (
            f"/member/guilds/{GUILD_ID}/radio-favorites",
            MemberRadioFavoritesHandler,
            "/member/guilds/{guild_id}/radio-favorites",
        ),
        (
            f"/member/guilds/{GUILD_ID}/radio-favorites/mine",
            MemberMineHandler,
            "/member/guilds/{guild_id}/radio-favorites/mine",
        ),
    ]
