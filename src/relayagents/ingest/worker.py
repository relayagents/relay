"""arq worker for the ``relay:ingest`` queue. Runs wherever the GPU is (Tailscale reaches Redis)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from arq.connections import RedisSettings

from relayagents.core.config import get_settings
from relayagents.core.db import Database
from relayagents.core.models import MeetingRow
from relayagents.core.queue import job_deserializer, job_serializer
from relayagents.ingest.fixture import FixtureTranscriber

log = structlog.get_logger()
INGEST_QUEUE = "relay:ingest"


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
    async with db.session() as session:
        meeting = await session.get(MeetingRow, meeting_id)
        if meeting is None:
            raise KeyError(meeting_id)
        if not meeting.audio_path:
            raise RuntimeError("meeting has no audio")
        meeting.status = "transcribing"
        audio = Path(meeting.audio_path)
        participants = list(meeting.participants)
        await session.commit()
    try:
        transcript = await ctx["transcriber"].transcribe(audio, meeting_id=meeting_id)
        transcript.segments = _resolve_speakers(transcript.segments, participants)
        out = audio.with_name("transcript.json")
        out.write_text(transcript.model_dump_json())
        async with db.session() as session:
            meeting = await session.get(MeetingRow, meeting_id)
            assert meeting is not None
            meeting.transcript_path = str(out)
            meeting.status = "queued"
            await session.commit()
        await ctx["redis"].enqueue_job(
            "extract_meeting", meeting_id
        )  # default queue → relay-workers
        log.info("meeting.transcribed", meeting_id=meeting_id, segments=len(transcript.segments))
        return str(out)
    except Exception as exc:
        async with db.session() as session:
            meeting = await session.get(MeetingRow, meeting_id)
            if meeting is not None:
                meeting.status, meeting.error = "failed", f"{type(exc).__name__}: {exc}"
                await session.commit()
        raise


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


__all__ = ["INGEST_QUEUE", "WorkerSettings", "json", "transcribe_meeting"]
