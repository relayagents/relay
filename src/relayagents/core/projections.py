"""Projections: read models derived from the event log.

``apply(session, event)`` is called on every append. ``rebuild(session, store)`` truncates and
replays. Projections must be pure functions of the log so that ``relay replay`` is safe.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relayagents.core.events import (
    ActionItemClosed,
    ActionItemCreated,
    ActionItemUpdated,
    DecisionMade,
    Event,
)
from relayagents.core.models import ActionItemRow, DecisionRow
from relayagents.core.store import EventStore


async def apply(session: AsyncSession, event: Event) -> None:
    p = event.payload
    if isinstance(p, ActionItemCreated):
        existing = await session.get(ActionItemRow, p.item_id)
        if existing is None:
            session.add(
                ActionItemRow(
                    id=p.item_id,
                    title=p.title,
                    assignee=p.assignee,
                    status="open",
                    due=p.due,
                    details=p.details,
                    meeting_id=p.meeting_id,
                    source_event_id=event.id,
                    last_event_id=event.id,
                    created_at=event.ts,
                    updated_at=event.ts,
                )
            )
    elif isinstance(p, ActionItemUpdated):
        row = await session.get(ActionItemRow, p.item_id)
        if row is not None:
            if p.title is not None:
                row.title = p.title
            if p.assignee is not None:
                row.assignee = p.assignee
            if p.due is not None:
                row.due = p.due
            if p.status is not None:
                row.status = p.status
            row.last_event_id = event.id
            row.updated_at = event.ts
    elif isinstance(p, ActionItemClosed):
        row = await session.get(ActionItemRow, p.item_id)
        if row is not None:
            row.status = "closed"
            row.last_event_id = event.id
            row.updated_at = event.ts
    elif isinstance(p, DecisionMade):
        existing = await session.get(DecisionRow, p.decision_id)
        if existing is None:
            session.add(
                DecisionRow(
                    id=p.decision_id,
                    statement=p.statement,
                    topic=p.topic,
                    rationale=p.rationale,
                    decided_by=list(p.decided_by),
                    decided_at=event.ts,
                    supersedes=p.supersedes,
                    source_event_id=event.id,
                )
            )
        if p.supersedes:
            old = await session.get(DecisionRow, p.supersedes)
            if old is not None:
                old.superseded_by = p.decision_id
    await session.flush()


async def rebuild(session: AsyncSession, store: EventStore) -> int:
    await session.execute(delete(ActionItemRow))
    await session.execute(delete(DecisionRow))
    n = 0
    async for event in store.iter_all():
        await apply(session, event)
        n += 1
    return n


async def list_items(
    session: AsyncSession, *, assignee: str | None = None, status: str = "open", limit: int = 100
) -> list[ActionItemRow]:
    stmt = select(ActionItemRow)
    if assignee:
        stmt = stmt.where(ActionItemRow.assignee == assignee)
    if status == "open":
        stmt = stmt.where(ActionItemRow.status != "closed")
    elif status != "all":
        stmt = stmt.where(ActionItemRow.status == status)
    stmt = stmt.order_by(ActionItemRow.updated_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def list_decisions(
    session: AsyncSession,
    *,
    topic: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    current_only: bool = False,
) -> list[DecisionRow]:
    stmt = select(DecisionRow)
    if current_only:
        stmt = stmt.where(DecisionRow.superseded_by.is_(None))
    if topic:
        stmt = stmt.where(
            (DecisionRow.topic.ilike(f"%{topic}%")) | (DecisionRow.statement.ilike(f"%{topic}%"))
        )
    if since:
        stmt = stmt.where(DecisionRow.decided_at >= since)
    stmt = stmt.order_by(DecisionRow.decided_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def distinct_topics(session: AsyncSession, *, limit: int = 500) -> list[str]:
    """Topic names in use, most recently decided first."""
    stmt = (
        select(DecisionRow.topic, func.max(DecisionRow.decided_at).label("last"))
        .where(DecisionRow.topic.isnot(None))
        .group_by(DecisionRow.topic)
        .order_by(func.max(DecisionRow.decided_at).desc())
        .limit(limit)
    )
    return [t for t, _ in (await session.execute(stmt)).all()]


def utcnow() -> datetime:
    return datetime.now(UTC)
