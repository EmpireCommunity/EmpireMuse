"""Rotas das estações personalizadas do servidor (B4, contrato T-456 §4 "Rádios"): ``GET/POST /guilds/{g}/radio-stations`` e
``PUT/DELETE …/radio-stations/{id}``.

Tudo pede premium de rádio (``403`` sem ele) e a estação é sempre do servidor do caminho (a de outro é o mesmo ``404`` de um
id que não existe). Criar é naturalmente idempotente (o nome é único no servidor); editar e apagar levam ``expected_version``
(o ``rev`` da estação, CAS na store). Apagar uma estação em uso é recusado. Nada de texto de usuário final sai.
"""

from __future__ import annotations

from typing import Any

from empiremuse.api.errors import BOT_UNAVAILABLE, ApiError, conflict, validation_failed
from empiremuse.api.idempotency import Reply
from empiremuse.api.radio_base import (
    GUILD_ID,
    STATION_ID,
    RadioRoute,
    radio_endpoint,
    raise_for_status,
)
from empiremuse.api.radio_data import custom_station_entry, safe_text
from empiremuse.api.writes_radio import CUSTOM_STATION_SPEC, parse_create, parse_versioned, read_station_version
from empiremuse.panel.services.base import FormContext
from empiremuse.panel.services.radio_stations import STATIONS_MAX, StationsView
from empiremuse.rules.panel_serialize import error_fields
from empiremuse.utils.usage import record_panel_action

CATEGORY = "radios"
STATUS_CREATED, STATUS_NO_CONTENT = 201, 204


def view_error_fields(view: StationsView) -> list[str]:
    """Os campos com erro de um resultado do serviço: o formulário de criar ou o da estação editada."""
    found = set(error_fields(view.form))
    for row in view.rows:
        found.update(error_fields(row.form))
    return sorted(found)


class StationsHandler(RadioRoute):
    """``GET`` lista as estações e o que o formulário oferece; ``POST`` cria uma."""

    @radio_endpoint
    async def get(self, guild_id: str) -> None:
        ctx, premium = await self.guild_context(guild_id, write=False)
        self.require_premium(premium)
        page = self.ctx.stations_page
        stations = await self.hub(ctx).stations.list_for_guild(ctx.guild_id)
        entries = [
            await custom_station_entry(ctx, station)
            for station in sorted(stations, key=lambda item: str(item.name).casefold())
            if int(station.guild_id) == ctx.guild_id  # defesa em profundidade: a store já filtra pelo servidor
        ]
        self.write(
            {
                "stations": entries,
                "total": len(entries),
                "max": STATIONS_MAX,
                "genres": [{"group": group, "genres": list(ids)} for group, ids in page.genre_groups(ctx)],
                "playlists": [safe_text(name) or "" for name in await page.playlist_names(ctx)],
            }
        )

    @radio_endpoint
    async def post(self, guild_id: str) -> None:
        ctx, premium = await self.guild_context(guild_id, write=True)
        self.require_premium(premium)
        body = self.json_body()
        write = parse_create(body, CUSTOM_STATION_SPEC)

        async def work() -> Reply:
            return await self.create(ctx, write.args)

        await self.run_write(body, work)

    async def create(self, ctx: FormContext, args: dict[str, list[str]]) -> Reply:
        stations = self.hub(ctx).stations
        if len(await stations.list_for_guild(ctx.guild_id)) >= STATIONS_MAX:
            record_panel_action(ctx, f"{CATEGORY}/estacoes", 400)
            raise validation_failed("limit")
        known = {item.id for item in await stations.list_for_guild(ctx.guild_id)}
        outcome = await self.ctx.stations_page.create(ctx, args, "", self.tr)
        record_panel_action(ctx, f"{CATEGORY}/estacoes", outcome.status)
        raise_for_status(outcome.status, view_error_fields(outcome.view))
        # a estação nova é a que não existia: o nome gravado pode diferir do enviado (espaços internos colapsados)
        created = [item for item in await stations.list_for_guild(ctx.guild_id) if item.id not in known]
        saved = created[0] if created else None
        if saved is None:
            raise ApiError(500, BOT_UNAVAILABLE)
        return Reply(STATUS_CREATED, await custom_station_entry(ctx, saved))


class StationHandler(RadioRoute):
    """``PUT`` edita (substitui) a estação; ``DELETE`` apaga, com ``?expected_version=``."""

    @radio_endpoint
    async def put(self, guild_id: str, station_id: str) -> None:
        ctx, station = await self.station_context(guild_id, station_id, write=True)
        body = self.json_body()
        write = parse_versioned(body, CUSTOM_STATION_SPEC)

        async def work() -> Reply:
            outcome = await self.ctx.stations_page.update(ctx, station, write.service_args(), "", self.tr)
            record_panel_action(ctx, f"{CATEGORY}/estacoes/salvar", outcome.status)
            await self.fail_unless_ok(outcome.status, ctx, station, view_error_fields(outcome.view))
            saved = await self.hub(ctx).stations.get(ctx.guild_id, station.id)
            return Reply(200, await custom_station_entry(ctx, saved))

        await self.run_write(body, work)

    @radio_endpoint
    async def delete(self, guild_id: str, station_id: str) -> None:
        ctx, station = await self.station_context(guild_id, station_id, write=True)
        query = self.query_arguments("expected_version")
        version = read_station_version(query)

        async def work() -> Reply:
            if await self.hub(ctx).stations.radios_using(ctx.guild_id, station.id):  # em uso: recusa, nada é apagado
                record_panel_action(ctx, f"{CATEGORY}/estacoes/apagar", 400)
                raise validation_failed("in_use")
            outcome = await self.ctx.stations_page.delete(ctx, station, {"versao": [version]}, "", self.tr)
            record_panel_action(ctx, f"{CATEGORY}/estacoes/apagar", outcome.status)
            await self.fail_unless_ok(outcome.status, ctx, station, view_error_fields(outcome.view))
            return Reply(STATUS_NO_CONTENT, None)

        await self.run_write(query, work)

    async def fail_unless_ok(self, status: int, ctx: FormContext, station: Any, fields: Any = ()) -> None:  # type: ignore[override]
        """No ``409`` a versão que a estação tem agora (relida da store); estação que sumiu é ``409`` sem versão."""
        if status == 409:
            current = await self.hub(ctx).stations.get(ctx.guild_id, station.id)
            raise conflict(str(current.rev) if current is not None else None)
        raise_for_status(status, fields)


def station_routes() -> list[tuple[str, Any, str]]:
    """(expressão do caminho sob o prefixo, handler, caminho do OpenAPI)."""
    return [
        (f"/guilds/{GUILD_ID}/radio-stations", StationsHandler, "/guilds/{guild_id}/radio-stations"),
        (
            f"/guilds/{GUILD_ID}/radio-stations/{STATION_ID}",
            StationHandler,
            "/guilds/{guild_id}/radio-stations/{station_id}",
        ),
    ]
