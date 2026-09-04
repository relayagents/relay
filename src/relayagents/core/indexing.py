"""Ask the workers to derive embeddings/graph entries for freshly appended events."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from relayagents.core.events import Event

log = structlog.get_logger()


async def index_later(services: Any, events: Sequence[Event]) -> bool:
    """Enqueue ``index_events`` for these events. No queue (tests, CLI without Redis): no-op.
    Workers holding the embedder/memory directly should call ``workers.jobs._index`` instead."""
    ids = [e.id for e in events]
    if not ids:
        return False
    if services.queue is None:
        return False
    try:
        await services.queue.enqueue_job("index_events", ids)
        return True
    except Exception as exc:
        log.warning("index.enqueue_failed", error=str(exc))
        return False
