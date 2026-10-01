"""arq job functions. Each is small and idempotent; state lives in Postgres, not in the worker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update

from relayagents.core.events import Actor, Event, TranscriptSegment
from relayagents.core.models import MeetingRow
from relayagents.core.projections import apply as project
from relayagents.core.projections import distinct_topics, list_decisions
from relayagents.core.protocols import ExtractionContext, RecentDecision, Transcript
from relayagents.core.queue import enqueue_meeting_job
from relayagents.core.store import EventStore
from relayagents.tools.context import Services
from relayagents.workers import pm
from relayagents.workers.digest import post_digest
from relayagents.workers.extraction import make_extractor

log = structlog.get_logger()


async def extract_meeting(ctx: dict[str, Any], meeting_id: str) -> dict[str, Any]:
    """Transcript (from file or from transcript.segment events) → decision/item/question events → PM."""
    services: Services = ctx["services"]
    # Claim the meeting with a compare-and-set, so a duplicate ticket (the sweep re-driving a
    # meeting another job just claimed, or one that already finished) backs off instead of
    # appending the decisions again and re-posting the summary. The one legitimate rerun of an
    # `extracting` meeting is arq retrying a job whose worker died, which arq marks with job_try.
    claimable = ["queued", "extracting"] if ctx.get("job_try", 1) > 1 else ["queued"]
    async with services.db.session() as session:
        claimed = await session.execute(
            update(MeetingRow)
            .where(MeetingRow.id == meeting_id, MeetingRow.status.in_(claimable))
            .values(status="extracting")
        )
        await session.commit()
        if not claimed.rowcount:
            meeting = await session.get(MeetingRow, meeting_id)
            if meeting is None:
                raise KeyError(meeting_id)
            log.warning("meeting.extract_skipped", meeting_id=meeting_id, status=meeting.status)
            return {"meeting_id": meeting_id, "skipped": meeting.status}
    try:
        transcript = await _load_transcript(services, meeting_id)
        async with services.db.session() as session:
            store = EventStore(session)
            existing = await store.query(
                types=["transcript.segment"], thread_id=meeting_id, limit=1
            )
            if not existing:
                seg_events = [
                    Event.new(
                        TranscriptSegment(
                            meeting_id=meeting_id,
                            segment_id=s.segment_id,
                            speaker=s.speaker,
                            start_s=s.start_s,
                            end_s=s.end_s,
                            text=s.text,
                            confidence=s.confidence,
                        ),
                        actor=Actor.system(f"relay.ingest.{transcript.engine}"),
                        source="meeting",
                        thread_id=meeting_id,
                    )
                    for s in transcript.segments
                ]
                await store.append_many(seg_events)
            meeting = await session.get(MeetingRow, meeting_id)
            assert meeting is not None
            extractor = make_extractor(services.settings.extraction_model)
            context = await extraction_context(session)
            events = [
                ev
                async for ev in extractor.extract(
                    transcript,
                    meeting_id=meeting_id,
                    participants=list(meeting.participants),
                    context=context,
                )
            ]
            for ev in events:
                await store.append(ev)
                await project(session, ev)
            dispatched = await pm.dispatch(session, services, meeting, events)
            meeting.status = "done"
            await session.commit()
        await _index(services, events)
        log.info(
            "meeting.extracted", meeting_id=meeting_id, events=len(events), dispatched=dispatched
        )
        return {"meeting_id": meeting_id, "events": [e.id for e in events], "dispatch": dispatched}
    except Exception as exc:
        async with services.db.session() as session:
            meeting = await session.get(MeetingRow, meeting_id)
            if meeting is not None:
                meeting.status = "failed"
                meeting.error = f"{type(exc).__name__}: {exc}"
                await session.commit()
        raise


async def requeue_stale_meetings(ctx: dict[str, Any]) -> dict[str, Any]:
    """Re-drive meetings whose queue ticket was lost (Redis flushed, or the enqueue after the
    commit failed). A ``queued`` meeting has nothing running, and arq's ``_job_id`` uniqueness
    makes re-enqueueing it a no-op while its ticket still exists, so no age threshold is needed
    (issue #21).

    ``transcribing``/``extracting`` are left alone: those statuses mean a job is running, and a run
    advances the meeting itself even if Redis loses its key. Re-driving one would race that run
    and dispatch twice. A meeting stays stuck only if Redis and the running worker are both lost
    mid-job (#22)."""
    services: Services = ctx["services"]
    async with services.db.session() as session:
        rows = (
            await session.execute(
                select(MeetingRow.id, MeetingRow.transcript_path).where(
                    MeetingRow.status == "queued"
                )
            )
        ).all()
    requeued = []
    for meeting_id, transcript_path in rows:
        job = "extract_meeting" if transcript_path else "transcribe_meeting"
        if await enqueue_meeting_job(ctx["redis"], job, meeting_id):
            log.warning("meeting.requeued", meeting_id=meeting_id, job=job)
            requeued.append(meeting_id)
    return {"requeued": requeued}


async def extraction_context(session: Any, *, recent: int = 50) -> ExtractionContext:
    """Known topics and the still-current recent decisions from the projections. This replaces the
    knowledge graph's two real jobs, topic resolution and supersedes detection (ADR-0005)."""
    rows = await list_decisions(session, limit=recent, current_only=True)
    return ExtractionContext(
        known_topics=await distinct_topics(session),
        recent_decisions=[
            RecentDecision(decision_id=r.id, topic=r.topic, statement=r.statement)
            for r in reversed(rows)  # oldest first, so "most recent" is last
        ],
    )


async def _load_transcript(services: Services, meeting_id: str) -> Transcript:
    async with services.db.session() as session:
        meeting = await session.get(MeetingRow, meeting_id)
        assert meeting is not None
        if meeting.transcript_path and Path(meeting.transcript_path).exists():
            data = json.loads(Path(meeting.transcript_path).read_text())
            data.setdefault("meeting_id", meeting_id)
            return Transcript.model_validate(data)
        segs = await EventStore(session).query(
            types=["transcript.segment"], thread_id=meeting_id, limit=100000
        )
    if not segs:
        raise RuntimeError(
            f"meeting {meeting_id} has neither a transcript file nor transcript.segment events"
        )
    from relayagents.core.protocols import Segment

    return Transcript(
        meeting_id=meeting_id,
        engine="events",
        segments=[
            Segment(
                segment_id=e.payload.segment_id,
                speaker=e.payload.speaker,
                start_s=e.payload.start_s,
                end_s=e.payload.end_s,
                text=e.payload.text,
                confidence=e.payload.confidence,
            )
            for e in segs
        ],
    )  # type: ignore[attr-defined]


async def _index(services: Services, events: list[Event], *, raise_errors: bool = False) -> None:
    """Derived stores: embeddings (pgvector leg) and the graph.

    In the extraction path a failure is logged and the job still succeeds (the log is the
    truth; derived stores can be rebuilt). In a rebuild, failures propagate: a half-rebuilt
    graph reported as success would be worse than a loud error.
    """
    if not events:
        return
    if services.embedder is not None:
        from relayagents.connectors.memory.embeddings import embed_events

        try:
            await embed_events(services.db, services.embedder, events)
        except Exception as exc:
            if raise_errors:
                raise
            log.warning("embeddings.failed", error=str(exc))
    if services.memory is not None:
        try:
            await services.memory.index(events)
        except Exception as exc:
            if raise_errors:
                raise
            log.warning("memory.index_failed", error=str(exc))


async def index_events(ctx: dict[str, Any], event_ids: list[str]) -> int:
    services: Services = ctx["services"]
    async with services.db.session() as session:
        store = EventStore(session)
        events = [e for e in [await store.get(i) for i in event_ids] if e is not None]
    await _index(services, events)
    return len(events)


async def embed_backlog(ctx: dict[str, Any]) -> int:
    """Belt and braces for anything that was appended without an index_later: embed events that
    still have no vector. Runs on a short cron; a no-op without an embedder."""
    services: Services = ctx["services"]
    if services.embedder is None:
        return 0
    from relayagents.connectors.memory import INDEXED_TYPES
    from relayagents.connectors.memory.embeddings import embed_events

    async with services.db.session() as session:
        events = await EventStore(session).unembedded(sorted(INDEXED_TYPES))
    return await embed_events(services.db, services.embedder, events) if events else 0


async def daily_digest(ctx: dict[str, Any]) -> str:
    services: Services = ctx["services"]
    ev = await post_digest(services)
    return ev.id


async def rebuild_graph(ctx: dict[str, Any]) -> int:
    """Wipe the derived graph and embeddings, then re-derive both from every event in the log.

    Raises on the first indexing failure so a broken rebuild is never reported as success.
    """
    services: Services = ctx["services"]
    if services.memory is None and services.embedder is None:
        return 0
    if services.memory is not None:
        await services.memory.reset()
    if services.embedder is not None:
        async with services.db.session() as session:
            await EventStore(session).clear_embeddings()
            await session.commit()
    n = 0
    batch: list[Event] = []
    async with services.db.session() as session:
        async for ev in EventStore(session).iter_all():
            batch.append(ev)
            if len(batch) >= 50:
                await _index(services, batch, raise_errors=True)
                n += len(batch)
                batch = []
    if batch:
        await _index(services, batch, raise_errors=True)
        n += len(batch)
    return n


async def semantic_recall(
    ctx: dict[str, Any], query: str, limit: int, kinds: list[str]
) -> list[dict[str, Any]]:
    """The vector + graph legs of `recall`, run here so the team key never reaches relay-api."""
    from relayagents.connectors.memory.search import semantic_search

    hits = await semantic_search(ctx["services"], query, limit=limit, kinds=kinds)
    return [h.model_dump(mode="json") for h in hits]
