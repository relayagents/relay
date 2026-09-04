"""arq worker entrypoint: ``arq relayagents.workers.main.WorkerSettings``."""

from __future__ import annotations

from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from relayagents.core.config import get_settings
from relayagents.core.queue import job_deserializer, job_serializer
from relayagents.workers.jobs import (
    daily_digest,
    embed_backlog,
    extract_meeting,
    index_events,
    rebuild_graph,
    semantic_recall,
)

log = structlog.get_logger()


async def startup(ctx: dict[str, Any]) -> None:
    from relayagents.api.app import build_services

    settings = get_settings()
    ctx["services"] = build_services(settings, role="worker")
    log.info(
        "workers.started",
        extraction_model=settings.extraction_model,
        memory=settings.memory_backend,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    services = ctx.get("services")
    if services is not None:
        if services.memory is not None:
            await services.memory.close()
        await services.db.dispose()


_settings = get_settings()


class WorkerSettings:
    functions = [
        extract_meeting,
        index_events,
        daily_digest,
        rebuild_graph,
        semantic_recall,
        embed_backlog,
    ]
    cron_jobs = [
        cron(
            daily_digest,
            hour=_settings.digest_hour_utc,
            minute=_settings.digest_minute_utc,
            run_at_startup=False,
        ),
        cron(embed_backlog, minute={0, 15, 30, 45}, run_at_startup=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    queue_name = "arq:queue"
    job_serializer = job_serializer
    job_deserializer = job_deserializer
    max_jobs = 4
    job_timeout = 1800
