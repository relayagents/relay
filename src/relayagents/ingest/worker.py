"""arq worker for the ``relay:ingest`` queue. Runs wherever the GPU is (Tailscale reaches Redis)."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import structlog
from arq.connections import RedisSettings
from sqlalchemy import update

from relayagents.core.config import get_settings
from relayagents.core.db import Database
from relayagents.core.models import MeetingRow
from relayagents.core.queue import (
    INGEST_QUEUE,
    enqueue_meeting_job,
    job_deserializer,
    job_serializer,
)
from relayagents.ingest.fixture import FixtureTranscriber

log = structlog.get_logger()


def make_transcriber(settings: Any) -> Any:
    if settings.transcriber == "fixture":
        return FixtureTranscriber()
    from relayagents.ingest.whisperx_transcriber import WhisperXTranscriber

    return WhisperXTranscriber(
        model=settings.whisperx_model,
        device=settings.whisperx_device,
        compute_type=settings.whisperx_compute_type,
        hf_token=settings.hf_token,
    )


async def transcribe_meeting(ctx: dict[str, Any], meeting_id: str) -> str:
    db: Database = ctx["db"]
    # Claim the meeting with a compare-and-set, as extract_meeting does, so a duplicate ticket (a
    # random-id one left from before fixed job ids, or a second ingest worker) backs off instead of
    # resetting a finished meeting to `queued` and getting it extracted twice. A saved transcript
    # means transcription is over, which is also how the sweeper picks the next job. The one
    # legitimate rerun of a `transcribing` meeting is arq retrying a timed-out job (job_try > 1).
    claimable = ["queued", "transcribing"] if ctx.get("job_try", 1) > 1 else ["queued"]
    async with db.session() as session:
        claimed = await session.execute(
            update(MeetingRow)
            .where(
                MeetingRow.id == meeting_id,
                MeetingRow.status.in_(claimable),
                MeetingRow.transcript_path.is_(None),
            )
            .values(status="transcribing")
        )
        await session.commit()
        meeting = await session.get(MeetingRow, meeting_id)
        if meeting is None:
            raise KeyError(meeting_id)
        if not claimed.rowcount:
            log.warning("meeting.transcribe_skipped", meeting_id=meeting_id, status=meeting.status)
            return meeting.transcript_path or ""
        if not meeting.audio_path:
            # Terminal, so the stale-meeting sweeper doesn't keep re-driving it.
            meeting.status, meeting.error = "failed", "RuntimeError: meeting has no audio"
            await session.commit()
            raise RuntimeError("meeting has no audio")
        audio = Path(meeting.audio_path)
        participants = list(meeting.participants)
    try:
        transcript = await ctx["transcriber"].transcribe(audio, meeting_id=meeting_id)
        transcript.segments = _resolve_speakers(transcript.segments, participants)
        out = audio.with_name("transcript.json")
        out.write_text(transcript.model_dump_json())
        owned = await _finish(db, meeting_id, transcript_path=str(out), status="queued")
    except Exception as exc:
        await _finish(db, meeting_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    if not owned:
        # Something else moved the meeting on mid-run; its state is no longer ours to advance.
        log.warning("meeting.transcribe_superseded", meeting_id=meeting_id)
        return str(out)
    log.info("meeting.transcribed", meeting_id=meeting_id, segments=len(transcript.segments))
    # Outside the try: the transcript is saved and the meeting is `queued`, so a failed enqueue
    # leaves it for requeue_stale_meetings instead of marking finished work as failed.
    await enqueue_meeting_job(ctx["redis"], "extract_meeting", meeting_id)  # → relay-workers
    return str(out)


async def _finish(db: Database, meeting_id: str, **values: Any) -> bool:
    """Write a transcription outcome only while this run still owns the meeting."""
    async with db.session() as session:
        result = await session.execute(
            update(MeetingRow)
            .where(MeetingRow.id == meeting_id, MeetingRow.status == "transcribing")
            .values(**values)
        )
        await session.commit()
    return bool(result.rowcount)


def _resolve_speakers(segments: list[Any], participants: list[str]) -> list[Any]:
    """Map SPEAKER_00.. to participants in order of first appearance when counts match.
    Anything else stays as a diarization label; humans can fix it later via an event."""
    labels: list[str] = []
    for s in segments:
        if s.speaker not in labels:
            labels.append(s.speaker)
    if (
        participants
        and len(labels) == len(participants)
        and all(lb.startswith("SPEAKER_") for lb in labels)
    ):
        mapping = dict(zip(labels, participants, strict=True))
        for s in segments:
            s.speaker = mapping[s.speaker]
    return segments


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    ctx["db"] = Database(settings.database_url)
    ctx["transcriber"] = make_transcriber(settings)
    log.info(
        "ingest.started",
        transcriber=settings.transcriber,
        model=settings.whisperx_model,
        device=settings.whisperx_device,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["db"].dispose()


class WorkerSettings:
    functions = [transcribe_meeting]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    queue_name = INGEST_QUEUE
    job_serializer = job_serializer
    job_deserializer = job_deserializer
    max_jobs = 1
    job_timeout = 3600
    # arq's health key is per-queue (`<queue_name>:health-check`), not per-process. The CPU
    # fallback here and the optional GPU worker (docker-compose.gpu.yml) both consume
    # `relay:ingest`, so a shared key would let either one's heartbeat mask the other's death.
    # Scope the key to this host so `arq --check` inside a given container only ever reads its
    # own heartbeat. Also lower the interval from arq's 3600s default (key TTL = interval + 1s)
    # so a dead worker is caught in seconds, not up to an hour.
    health_check_key = f"{INGEST_QUEUE}:health-check:{socket.gethostname()}"
    health_check_interval = 30


__all__ = ["INGEST_QUEUE", "WorkerSettings", "json", "transcribe_meeting"]
