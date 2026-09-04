"""EventStore: append-only writes and cursor-friendly reads over the ``events`` table."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relayagents.core.events import Event
from relayagents.core.models import EventRow

_TEXT_FIELDS = (
    "text",
    "statement",
    "title",
    "details",
    "rationale",
    "answer",
    "action",
    "summary",
    "note",
)


def flatten_text(event: Event) -> str:
    """Searchable text for the event, used by keyword/full-text search."""
    data = event.payload.model_dump()
    parts: list[str] = []
    for key in _TEXT_FIELDS:
        v = data.get(key)
        if isinstance(v, str) and v:
            parts.append(v)
    for key in (
        "done",
        "doing",
        "blocked",
        "shipped",
        "in_progress",
        "blockers",
        "decisions_needed",
    ):
        v = data.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
    return "\n".join(parts)


def event_summary(event: Event) -> str:
    """One line for humans and search hits: ``[type] main text``."""
    p = event.payload.model_dump()
    for key in ("statement", "text", "title", "action", "answer"):
        if p.get(key):
            return f"[{event.type}] {p[key]}"
    return f"[{event.type}]"


def parse_since(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Accept ISO timestamps or relative windows like ``24h``, ``7d``, ``30m``."""
    if not value:
        return None
    now = now or datetime.now(UTC)
    m = re.fullmatch(r"(\d+)\s*([smhdw])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return now - delta
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def row_to_event(row: EventRow) -> Event:
    return Event.model_validate(
        {
            "id": row.id,
            "ts": row.ts,
            "type": row.type,
            "actor": {"kind": row.actor_kind, "id": row.actor_id},
            "source": row.source,
            "visibility": row.visibility,
            "thread_id": row.thread_id,
            "payload": row.payload,
            "provenance": row.provenance,
        }
    )


def event_to_row(event: Event, *, embedding: list[float] | None = None) -> EventRow:
    return EventRow(
        id=event.id,
        ts=event.ts,
        type=event.type,
        actor_kind=event.actor.kind,
        actor_id=event.actor.id,
        source=event.source,
        visibility=event.visibility,
        thread_id=event.thread_id,
        payload=event.payload.model_dump(mode="json"),
        provenance=event.provenance.model_dump(mode="json"),
        text_index=flatten_text(event),
        embedding=embedding,
    )


class EventStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(self, event: Event, *, embedding: list[float] | None = None) -> Event:
        self.session.add(event_to_row(event, embedding=embedding))
        await self.session.flush()
        return event

    async def append_many(self, events: Sequence[Event]) -> list[Event]:
        self.session.add_all([event_to_row(e) for e in events])
        await self.session.flush()
        return list(events)

    async def get(self, event_id: str) -> Event | None:
        row = await self.session.scalar(select(EventRow).where(EventRow.id == event_id))
        return row_to_event(row) if row else None

    async def query(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        types: Sequence[str] | None = None,
        thread_id: str | None = None,
        actor_id: str | None = None,
        user_id: str | None = None,
        after_seq: int | None = None,
        text: str | None = None,
        limit: int = 100,
        descending: bool = False,
    ) -> list[Event]:
        stmt = select(EventRow)
        if since is not None:
            stmt = stmt.where(EventRow.ts >= since)
        if until is not None:
            stmt = stmt.where(EventRow.ts < until)
        if types:
            stmt = stmt.where(EventRow.type.in_(list(types)))
        if thread_id:
            stmt = stmt.where(EventRow.thread_id == thread_id)
        if actor_id:
            stmt = stmt.where(EventRow.actor_id == actor_id)
        if user_id:
            # a human and all of their agents (`ada`, `ada.hermes`, `ada.claude-code`)
            stmt = stmt.where(
                (EventRow.actor_id == user_id) | EventRow.actor_id.like(f"{user_id}.%")
            )
        if after_seq is not None:
            stmt = stmt.where(EventRow.seq > after_seq)
        if text:
            stmt = stmt.where(EventRow.text_index.ilike(f"%{text}%"))
        stmt = stmt.order_by(EventRow.seq.desc() if descending else EventRow.seq.asc()).limit(limit)
        rows = (await self.session.scalars(stmt)).all()
        return [row_to_event(r) for r in rows]

    async def iter_all(self, *, batch: int = 500) -> AsyncIterator[Event]:
        after = 0
        while True:
            rows = (
                await self.session.scalars(
                    select(EventRow).where(EventRow.seq > after).order_by(EventRow.seq).limit(batch)
                )
            ).all()
            if not rows:
                return
            for r in rows:
                after = r.seq
                yield row_to_event(r)

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(EventRow)) or 0)

    async def keyword_search(
        self, query: str, *, limit: int = 10, types: Sequence[str] | None = None
    ) -> list[tuple[Event, float]]:
        """Naive lexical search: each query term must match; score = fraction of terms matched
        weighted by recency. Good enough as the "event log" leg of hybrid recall."""
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
        if not terms:
            return []
        stmt = select(EventRow)
        if types:
            stmt = stmt.where(EventRow.type.in_(list(types)))
        stmt = stmt.where(EventRow.text_index != "")
        for t in terms[:6]:
            stmt = stmt.where(EventRow.text_index.ilike(f"%{t}%"))
        stmt = stmt.order_by(EventRow.seq.desc()).limit(limit)
        rows = (await self.session.scalars(stmt)).all()
        out: list[tuple[Event, float]] = []
        for r in rows:
            hay = r.text_index.lower()
            matched = sum(1 for t in terms if t in hay)
            out.append((row_to_event(r), matched / len(terms)))
        return out

    async def set_embeddings(self, pairs: Sequence[tuple[str, list[float]]]) -> None:
        if not pairs:
            return
        from sqlalchemy import bindparam, update

        table = EventRow.__table__
        stmt = (
            update(table)
            .where(table.c.id == bindparam("event_id"))
            .values(embedding=bindparam("vec"))
        )
        await self.session.execute(stmt, [{"event_id": i, "vec": v} for i, v in pairs])

    async def clear_embeddings(self) -> None:
        from sqlalchemy import update

        await self.session.execute(
            update(EventRow).where(EventRow.embedding.isnot(None)).values(embedding=None)
        )

    async def vector_search(
        self, embedding: list[float], *, limit: int = 10
    ) -> list[tuple[Event, float]]:
        """pgvector cosine search. No-op on SQLite."""
        bind = self.session.get_bind()
        if bind.dialect.name != "postgresql":
            return []
        dist = EventRow.embedding.cosine_distance(embedding)  # type: ignore[attr-defined]
        stmt = (
            select(EventRow, dist.label("d"))
            .where(EventRow.embedding.isnot(None))
            .order_by(dist)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return [(row_to_event(r), max(0.0, 1.0 - float(d))) for r, d in result.all()]


def event_payload_get(event: Event, key: str, default: Any = None) -> Any:
    return getattr(event.payload, key, default)
