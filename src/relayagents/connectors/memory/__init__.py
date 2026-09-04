"""``MemoryStore`` reference implementation: Graphiti on embedded Kuzu.

The graph is derived state. ``relay replay --rebuild-graph`` calls ``reset()`` and re-indexes every
event, which is the proof that the event log is the source of truth (ADR-0001, ADR-0005).

Kuzu's upstream development stopped in late 2025; the pip package still works and Graphiti still
ships the driver. Graphiti also has an embedded FalkorDB driver, so switching is a config change
here and nowhere else. See ADR-0005 for the revisit trigger.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from relayagents.core.config import Settings
from relayagents.core.events import Event
from relayagents.core.protocols import MemoryHit
from relayagents.core.store import flatten_text

INDEXED_TYPES = {
    "decision.made",
    "action_item.created",
    "action_item.closed",
    "question.opened",
    "question.answered",
    "report.posted",
    "standup.posted",
    "digest.posted",
    "transcript.segment",
}


class NullMemory:
    """No graph. `recall` falls back to event-log search."""

    async def index(self, events: Sequence[Event]) -> None:
        return None

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryHit]:
        return []

    async def reset(self) -> None:
        return None

    async def close(self) -> None:
        return None


class GraphitiKuzuMemory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.graph_path)
        self._graphiti = None
        self._ready = False

    async def _client(self):  # type: ignore[no-untyped-def]
        if self._graphiti is None:
            from graphiti_core import Graphiti
            from graphiti_core.driver.kuzu_driver import KuzuDriver

            self.path.parent.mkdir(parents=True, exist_ok=True)
            driver = KuzuDriver(db=str(self.path))
            # LLM/embedder clients read OPENAI_API_KEY etc. from the environment: the team key, workers only.
            self._graphiti = Graphiti(graph_driver=driver)
        if not self._ready:
            await self._graphiti.build_indices_and_constraints()
            self._ready = True
        return self._graphiti

    async def index(self, events: Sequence[Event]) -> None:
        from graphiti_core.nodes import EpisodeType

        g = await self._client()
        for ev in events:
            if ev.type not in INDEXED_TYPES:
                continue
            body = flatten_text(ev)
            if not body:
                continue
            await g.add_episode(
                name=ev.id,
                episode_body=f"{ev.actor.id}: {body}",
                source_description=f"relay {ev.type} ({ev.source})",
                reference_time=ev.ts,
                source=EpisodeType.message if ev.type == "transcript.segment" else EpisodeType.text,
                group_id=self.settings.team_name,
                uuid=ev.id,
            )

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryHit]:
        g = await self._client()
        edges = await g.search(query, group_ids=[self.settings.team_name], num_results=limit)
        hits = []
        for e in edges:
            hits.append(
                MemoryHit(
                    text=e.fact,
                    score=0.5,
                    kind="graph",
                    event_ids=list(getattr(e, "episodes", []) or []),
                    valid_from=_dt(getattr(e, "valid_at", None)),
                    valid_to=_dt(getattr(e, "invalid_at", None)),
                    ref=e.uuid,
                )
            )
        return hits

    async def reset(self) -> None:
        await self.close()
        if self.path.exists():
            if self.path.is_dir():
                shutil.rmtree(self.path)
            else:
                self.path.unlink()
        wal = self.path.with_name(self.path.name + ".wal")
        if wal.exists():
            wal.unlink()

    async def close(self) -> None:
        if self._graphiti is not None:
            try:
                await self._graphiti.close()
            finally:
                self._graphiti = None
                self._ready = False


def _dt(v: object) -> datetime | None:
    return v if isinstance(v, datetime) else None


def make_memory(settings: Settings) -> GraphitiKuzuMemory | NullMemory:
    if settings.memory_backend == "graphiti-kuzu":
        return GraphitiKuzuMemory(settings)
    return NullMemory()
