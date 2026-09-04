"""The semantic legs of `recall` (pgvector + graph).

``semantic_search`` runs where the team key lives: in a worker. relay-api reaches it through
``ArqSemanticSearch``, which enqueues the ``semantic_recall`` job and waits briefly for the
result. If workers are unreachable, recall falls back to the event-log keyword leg.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import structlog

from relayagents.core.protocols import MemoryHit
from relayagents.core.store import EventStore

log = structlog.get_logger()


def _summary(event: Any) -> str:
    p = event.payload.model_dump()
    for key in ("statement", "text", "title", "action", "answer"):
        if p.get(key):
            return f"[{event.type}] {p[key]}"
    return f"[{event.type}]"


async def semantic_search(
    services: Any, query: str, *, limit: int = 10, kinds: Sequence[str] = ("vector", "graph")
) -> list[MemoryHit]:
    """Worker-side: embed the query for pgvector, and ask the graph. Each leg fails independently."""
    hits: list[MemoryHit] = []
    if "vector" in kinds and services.embedder is not None and query.strip():
        try:
            vec = await services.embedder.embed_query(query)
            async with services.db.session() as session:
                for event, score in await EventStore(session).vector_search(vec, limit=limit):
                    hits.append(
                        MemoryHit(
                            text=_summary(event),
                            score=round(score, 3),
                            kind="vector",
                            event_ids=[event.id],
                            valid_from=event.ts,
                            ref=event.id,
                        )
                    )
        except Exception as exc:
            log.warning("recall.vector_failed", error=str(exc))
    if "graph" in kinds and services.memory is not None:
        try:
            hits.extend(await services.memory.search(query, limit=limit))
        except Exception as exc:
            log.warning("recall.graph_failed", error=str(exc))
    return hits


class ArqSemanticSearch:
    """API-side ``SemanticSearch``: delegate to the workers over the job queue."""

    def __init__(self, pool: Any, *, timeout_s: float = 15.0) -> None:
        self.pool = pool
        self.timeout_s = timeout_s

    async def __call__(
        self, query: str, *, limit: int = 10, kinds: Sequence[str] = ("vector", "graph")
    ) -> list[MemoryHit]:
        job = await self.pool.enqueue_job("semantic_recall", query, limit, list(kinds))
        if job is None:
            return []
        try:
            raw = await asyncio.wait_for(job.result(poll_delay=0.2), timeout=self.timeout_s)
        except TimeoutError:
            log.warning("recall.semantic_timeout", timeout_s=self.timeout_s)
            return []
        return [MemoryHit.model_validate(h) for h in raw]
