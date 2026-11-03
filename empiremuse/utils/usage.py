# -*- coding: utf-8 -*-
"""Registro de uso do bot: onde (servidor, canal), como (comando/botão, o que tocou) e quando.

Os eventos entram em um buffer em memória (``record`` nunca levanta nem espera o banco) e uma tarefa os grava em lote na coleção
``usage_events`` do banco global (MongoDB ou local). Nada aqui identifica quem usou: a lista de campos permitidos está em
``empiremuse.rules.usage.normalize_event``. Falha do banco: uma linha de log por queda, contagem nas métricas e o lote é descartado.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from collections import deque
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Optional

from cachetools import TTLCache

from empiremuse.logs.redact import redact
from empiremuse.rules.usage import (
    KIND_GUILD,
    KIND_PANEL,
    KIND_PLAYBACK,
    LABEL_LIMIT,
    MAX_LATENCY_MS,
    MS_PER_DAY,
    MS_PER_SECOND,
    SUMMARY_DAYS,
    clamp_retention_days,
    clean_text,
    end_reason,
    error_label,
    normalize_event,
)
from empiremuse.utils.db import DBModel

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

USAGE_DB = "global"
FLUSH_INTERVAL = 10.0
FLUSH_BATCH = 500
MAX_BUFFER = 5000
PRUNE_INTERVAL = 3600.0
QUERY_LIMIT = 200_000  # acima disso o resumo avisa que os totais são parciais
STAMP_LIMIT = 1000  # quantas faixas da fila recebem o rótulo do comando que as enfileirou
METRIC_KIND = "usage_write"
EXTRA_KEY = "usage_cmd"
OPEN_COMMANDS_MAX = 2000  # comandos iniciados e ainda sem desfecho
OPEN_COMMANDS_TTL = 900
# Só o que enfileira música carimba as faixas (um /skip ou /volume não enfileira nada).
ENQUEUE_PREFIXES = ("play", "search", "add", "enqueue", "embed_", "jukebox")


def _id_and_name(obj: object) -> tuple[Optional[int], Optional[str]]:
    """``(id, nome)`` de um servidor ou canal; ``(None, None)`` se faltar."""
    if obj is None:
        return None, None
    return getattr(obj, "id", None), getattr(obj, "name", None)


def _now_ms() -> int:
    return int(time.time() * MS_PER_SECOND)


class _Playback:
    __slots__ = ("event_id", "started", "length", "paused_total", "paused_since")

    def __init__(self, event_id: str, length: Optional[int]) -> None:
        self.event_id = event_id
        self.started = time.monotonic()
        self.length = length
        self.paused_total = 0.0
        self.paused_since: Optional[float] = None

    def set_paused(self, paused: bool) -> None:
        now = time.monotonic()
        if paused and self.paused_since is None:
            self.paused_since = now
        elif not paused and self.paused_since is not None:
            self.paused_total += now - self.paused_since
            self.paused_since = None

    def listened(self) -> int:
        """Segundos tocando de fato: o tempo desde o início sem as pausas, até o tamanho da faixa (se conhecido)."""
        now = time.monotonic()
        paused = self.paused_total + (now - self.paused_since if self.paused_since is not None else 0.0)
        seconds = max(0, int(now - self.started - paused))
        return seconds if self.length is None else min(seconds, self.length)


class _OpenCommand:
    __slots__ = ("event_id", "started", "name", "guild_id", "bot")

    def __init__(self, event_id: str, name: str, guild_id: int, bot: object) -> None:
        self.event_id = event_id
        self.started = time.monotonic()
        self.name = name
        self.guild_id = guild_id
        self.bot = bot


def _open_key(bot: object, source: object) -> Optional[tuple[Optional[int], int]]:
    """Chave do comando aberto: o bot que recebeu + a interação (``id``) ou a mensagem do prefixo (``id``).

    O bot entra na chave porque o mapa é do pool inteiro e, no prefixo, todos os bots veem a mesma mensagem: o bot recusado pelo
    roteamento não pode fechar o evento do bot que executa.
    """
    key = getattr(source, "id", None) or getattr(getattr(source, "message", None), "id", None)
    return None if key is None else (getattr(getattr(bot, "user", None), "id", None), key)


class UsageRecorder:
    def __init__(self, pool: BotPool) -> None:
        self.pool = pool
        self._buffer: deque[tuple[str, Any]] = deque()
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._playing: dict[tuple[int, int], _Playback] = {}
        self._open: TTLCache = TTLCache(maxsize=OPEN_COMMANDS_MAX, ttl=OPEN_COMMANDS_TTL)
        self._failure_logged = False
        self._indexes_ready = False
        self._last_prune: Optional[float] = None

    # ------------------------------------------------------------------ entrada (síncrona, nunca levanta)

    def record(
        self,
        kind: str,
        name: str = "",
        *,
        bot: object = None,
        guild: object = None,
        text_channel: object = None,
        voice_channel: object = None,
        ok: Optional[bool] = None,
        error: Optional[str] = None,
        ms: Optional[int] = None,
        **extra: Any,
    ) -> Optional[str]:
        """Põe um evento no buffer e devolve o ``id`` dele (``None`` se foi descartado). Falha nunca chega a quem chama."""
        try:
            guild_id, guild_name = _id_and_name(guild)
            text_id, text_name = _id_and_name(text_channel)
            voice_id, voice_name = _id_and_name(voice_channel)
            event = normalize_event(
                {
                    "id": uuid.uuid4().hex,
                    "ts": _now_ms(),
                    "kind": kind,
                    "name": name,
                    "bot": getattr(getattr(bot, "user", None), "id", None),
                    "guild": guild_id,
                    "guild_name": guild_name,
                    "text_id": text_id,
                    "text_name": text_name,
                    "voice_id": voice_id,
                    "voice_name": voice_name,
                    "ok": ok,
                    "error": error,
                    "ms": ms,
                    **extra,
                }
            )
            if event is None:
                return None
            self._enqueue(
                ("insert", {key: redact(value) if isinstance(value, str) else value for key, value in event.items()})
            )
            return event["id"]
        except Exception as exc:  # noqa: BLE001 - o registro de uso nunca derruba o comando
            self._note_failure("registrar", exc)
            return None

    def _enqueue(self, op: tuple[str, Any]) -> None:
        if len(self._buffer) >= MAX_BUFFER:
            self._buffer.popleft()
            self._count_failure()
        self._buffer.append(op)

    # ------------------------------------------------------------------ comandos e servidores

    def command_started(self, kind: str, source: object, name: str, *, bot: object) -> None:
        """Abre o evento de um comando de barra, de menu ou de prefixo no gancho do ``COMMAND_LOG``: só o nome e onde foi usado.

        O usuário e os valores das opções ficam de fora; o desfecho (``ok``/erro e latência) entra depois, em ``command_finished``.
        """
        try:
            guild = getattr(source, "guild", None)
            key = _open_key(bot, source)
            if guild is None or key is None or not name:
                return
            event_id = self.record(kind, name, bot=bot, guild=guild, text_channel=getattr(source, "channel", None))
            if event_id is not None:
                self._open[key] = _OpenCommand(event_id, name, guild.id, bot)
        except Exception as exc:  # noqa: BLE001
            self._note_failure("registrar", exc)

    def command_finished(
        self, source: object, error: Optional[BaseException] = None, *, bot: object, music_bot: object = None
    ) -> None:
        """Fecha o evento aberto por ``command_started`` pelo mesmo ``bot`` que o abriu: ``ok`` ou o rótulo fixo do erro, a latência e o
        bot que executou."""
        try:
            opened = self._open.pop(_open_key(bot, source), None)
            if opened is None:
                return
            fields: dict[str, Any] = {
                "ok": error is None,
                "ms": min(int((time.monotonic() - opened.started) * MS_PER_SECOND), MAX_LATENCY_MS),
            }
            if error is not None:
                fields["error"] = label_for(error)
            executor = music_bot or opened.bot
            if executor is not opened.bot:
                fields["bot"] = executor.user.id  # type: ignore[attr-defined]
            self._enqueue(("patch", (opened.event_id, fields)))
            self.stamp_queue(executor, opened.guild_id, opened.name)
        except Exception as exc:  # noqa: BLE001
            self._note_failure("registrar", exc)

    def record_command(
        self, kind: str, name: str, source: object, *, bot: object, ok: bool, error: Optional[str] = None
    ) -> None:
        """Um comando (barra, prefixo, botão, Jukebox ou painel). ``source`` é a interação ou o contexto de origem."""
        try:
            guild = getattr(source, "guild", None)
            if guild is None:  # mensagem privada: sem servidor não há o que registrar
                return
            channel = getattr(source, "channel", None)
            created = getattr(source, "created_at", None) or getattr(
                getattr(source, "message", None), "created_at", None
            )
            latency = None
            if created is not None:
                latency = max(0, int((time.time() - created.timestamp()) * MS_PER_SECOND))
            self.record(kind, name, bot=bot, guild=guild, text_channel=channel, ok=ok, error=error, ms=latency)
            self.stamp_queue(bot, guild.id, name)
        except Exception as exc:  # noqa: BLE001
            self._note_failure("registrar", exc)

    def record_guild(self, action: str, guild: object, *, bot: object) -> None:
        """Entrada (``join``) ou saída (``leave``) do bot em um servidor."""
        self.record(KIND_GUILD, action, bot=bot, guild=guild)

    # ------------------------------------------------------------------ reprodução

    def stamp_queue(self, bot: object, guild_id: int, label: str) -> None:
        """Marca as faixas ainda sem rótulo (e que não vieram do autoplay) com o comando que acabou de enfileirar."""
        if not label.startswith(ENQUEUE_PREFIXES):
            return
        try:
            player = bot.music.players.get(guild_id)  # type: ignore[attr-defined]
            if player is None:
                return
            tracks = list(itertools.islice(reversed(player.queue), STAMP_LIMIT))  # o que acabou de entrar fica no fim
            if player.current is not None:
                tracks.append(player.current)
            for track in tracks:
                extra = track.info.get("extra")
                if isinstance(extra, dict) and EXTRA_KEY not in extra and not extra.get("autoplay"):
                    extra[EXTRA_KEY] = label
                    if track is player.current:
                        self._label_playing(bot, guild_id, label)
        except Exception:  # noqa: BLE001 - carimbo é só rótulo; sem ele a faixa fica sem "via"
            return

    def _label_playing(self, bot: object, guild_id: int, label: str) -> None:
        """A faixa começou antes de o comando terminar: acrescenta o ``via`` ao evento de reprodução já registrado."""
        playback = self._playing.get((bot.user.id, guild_id))  # type: ignore[attr-defined]
        if playback is not None:
            self._enqueue(("patch", (playback.event_id, {"via": clean_text(label, LABEL_LIMIT)})))

    def playback_started(self, bot: object, player: object, track: object) -> None:
        try:
            guild = player.guild  # type: ignore[attr-defined]
            info = getattr(track, "info", None) or {}
            extra = info.get("extra") or {}
            length_ms = info.get("length")
            length = None if info.get("isStream") or not length_ms else int(length_ms // MS_PER_SECOND)
            channel_id = getattr(player, "channel_id", None)
            voice = bot.get_channel(channel_id) if channel_id else None  # type: ignore[attr-defined]
            via = extra.get(EXTRA_KEY) or ("autoplay" if extra.get("autoplay") else None)
            event_id = self.record(
                KIND_PLAYBACK,
                "play",
                bot=bot,
                guild=guild,
                text_channel=getattr(player, "text_channel", None),
                voice_channel=voice,
                title=None
                if info.get("sourceName") == "http"
                else info.get("title"),  # no link direto o título é a URL do usuário
                author=info.get("author"),
                source=info.get("sourceName"),
                length=length,
                via=via,
            )
            key = (bot.user.id, guild.id)  # type: ignore[attr-defined]
            if (previous := self._playing.pop(key, None)) is not None:
                self._patch(previous, "stopped")
            if event_id is not None:
                self._playing[key] = _Playback(event_id, length)
        except Exception as exc:  # noqa: BLE001
            self._note_failure("registrar", exc)

    def playback_ended(self, bot: object, guild_id: int, reason: object) -> None:
        try:
            playback = self._playing.pop((bot.user.id, guild_id), None)  # type: ignore[attr-defined]
            if playback is not None:
                self._patch(playback, end_reason(reason))
        except Exception as exc:  # noqa: BLE001
            self._note_failure("registrar", exc)

    def playback_paused(self, bot: object, guild_id: int, paused: bool) -> None:
        playback = self._playing.get((bot.user.id, guild_id))  # type: ignore[attr-defined]
        if playback is not None:
            playback.set_paused(paused)

    def _patch(self, playback: _Playback, reason: str) -> None:
        self._enqueue(("patch", (playback.event_id, {"listened": playback.listened(), "end": reason})))

    # ------------------------------------------------------------------ gravação em lote

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Inicia a tarefa de gravação no laço do pool; chamar de novo não duplica."""
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run())

    async def close(self) -> None:
        """No encerramento: para a tarefa e grava o que sobrou."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self.flush()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self.flush()

    async def flush(self) -> None:
        """Grava o buffer. O lote com falha é descartado e o resto fica para a próxima rodada (o buffer tem teto)."""
        async with self._lock:
            try:
                db = self.pool.database
                if db is None:
                    return
                await self._prepare(db)
            except Exception as exc:  # noqa: BLE001 - nada aqui pode derrubar a tarefa
                self._note_failure("preparar", exc)
                return
            failed = False
            while self._buffer and not failed:
                try:
                    await self._write(db, self._drain())
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    self._note_failure("gravar", exc)
            try:
                await self._prune(db)
            except Exception as exc:  # noqa: BLE001
                failed = True
                self._note_failure("podar", exc)
            if not failed:
                self._failure_logged = False

    def _drain(self) -> list[tuple[str, Any]]:
        return [self._buffer.popleft() for _ in range(min(FLUSH_BATCH, len(self._buffer)))]

    async def _prepare(self, db: Any) -> None:
        if not self._indexes_ready:
            await db.ensure_event_indexes(db_name=DBModel.usage_events, collection=USAGE_DB)
            self._indexes_ready = True

    async def _write(self, db: Any, ops: list[tuple[str, Any]]) -> None:
        """Os inserts do lote em uma escrita só (com o desfecho da faixa já dobrado no insert); o resto como ``patch``."""
        pending: dict[str, dict] = {}
        patches: list[tuple[str, dict]] = []
        for action, payload in ops:
            if action == "insert":
                doc = {key: value for key, value in payload.items() if key != "id"}
                pending[payload["id"]] = {"_id": payload["id"], **doc}
            elif payload[0] in pending:
                pending[payload[0]].update(payload[1])
            else:
                patches.append(payload)
        expire_after = timedelta(days=clamp_retention_days(self.pool.config.get("USAGE_RETENTION_DAYS")))
        if pending:
            await db.append_events(
                list(pending.values()), db_name=DBModel.usage_events, collection=USAGE_DB, expire_after=expire_after
            )
        for event_id, fields in patches:
            await db.patch_event(event_id, fields, db_name=DBModel.usage_events, collection=USAGE_DB)

    async def _prune(self, db: Any) -> None:
        """No banco local a retenção é por poda (no MongoDB é o índice TTL), a cada ``PRUNE_INTERVAL``."""
        now = time.monotonic()
        if self._last_prune is not None and now - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = now
        days = clamp_retention_days(self.pool.config.get("USAGE_RETENTION_DAYS"))
        await db.purge_events(_now_ms() - days * MS_PER_DAY, db_name=DBModel.usage_events, collection=USAGE_DB)

    # ------------------------------------------------------------------ consulta (painel do dono)

    async def fetch_guild_events(self, guild_id: int, *, days: int = SUMMARY_DAYS) -> list[dict]:
        """Os eventos do servidor nos últimos ``days`` dias (mais recentes primeiro, no máximo ``QUERY_LIMIT``)."""
        await self.flush()
        since = _now_ms() - days * MS_PER_DAY
        async with self._lock:
            rows = await self.pool.database.find_events(
                {"guild": guild_id, "ts": {"$gte": since}},
                db_name=DBModel.usage_events,
                collection=USAGE_DB,
                limit=QUERY_LIMIT,
            )
        return [{**row, "id": row.get("_id")} for row in rows]

    # ------------------------------------------------------------------ falhas

    def _count_failure(self) -> None:
        metrics = getattr(self.pool, "metrics", None)
        if metrics is not None:
            metrics.error(METRIC_KIND)

    def _note_failure(self, what: str, exc: BaseException) -> None:
        self._count_failure()
        if not self._failure_logged:
            self._failure_logged = True
            logger.warning(
                "Registro de uso: falha ao %s (%s); os eventos da falha são descartados.", what, type(exc).__name__
            )


def usage_of(owner: object) -> Optional[UsageRecorder]:
    """O gravador do pool de um bot, do próprio pool ou de um contexto do painel; ``None`` se não houver (fakes de teste)."""
    pool = getattr(owner, "pool", owner)
    recorder = getattr(pool, "usage", None)
    return recorder if isinstance(recorder, UsageRecorder) else None


def label_for(error: BaseException) -> str:
    """Rótulo fixo (``ERROR_LABELS``) de uma exceção de comando; o texto dela nunca é usado."""
    error = getattr(error, "original", error)
    return error_label(cls.__name__ for cls in type(error).__mro__)


def component_name(interaction: object) -> str:
    """Nome do botão ou select de uma interação (o ``custom_id`` sem o prefixo ``musicplayer_`` do player)."""
    custom_id = str(getattr(getattr(interaction, "data", None), "custom_id", "") or "")
    return custom_id.removeprefix("musicplayer_")


def record_panel_action(ctx: object, name: str, status: int) -> None:
    """Uma ação do painel web (``ctx`` é o ``FormContext``): rótulo fixo da ação e resultado pelo status HTTP; nada do usuário do painel."""
    if (usage := usage_of(ctx)) is None:
        return
    ok = status < 400
    usage.record_command(KIND_PANEL, name, ctx, bot=getattr(ctx, "bot", None), ok=ok, error=None if ok else "rejected")
