"""Meetings reach the worker that runs their next job, and re-drive when a ticket is lost."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relayagents.core.models import MeetingRow
from relayagents.core.queue import INGEST_QUEUE, WORKER_QUEUE
from relayagents.ingest.worker import transcribe_meeting
from relayagents.tools.context import Services

from .conftest import FIXTURES


class ArqLikeQueue:
    """Mirrors arq's contract that ``_queue_name=None`` means the pool's default queue, which
    inside a worker is that worker's own queue."""

    def __init__(self, default_queue: str = WORKER_QUEUE) -> None:
        self.default_queue = default_queue
        self.jobs: list[tuple[str, tuple[Any, ...], str | None]] = []

    async def enqueue_job(
        self, function: str, *args: Any, _queue_name: str | None = None, **_: Any
    ) -> object | None:
        self.jobs.append((function, args, _queue_name or self.default_queue))
        return object()


async def test_transcription_hands_off_to_the_extraction_workers(
    services: Services, tmp_path: Path
) -> None:
    from relayagents.ingest.fixture import FixtureTranscriber

    audio = tmp_path / "mtg_ingest" / "audio.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF0000WAVE")
    audio.with_suffix(".json").write_text((FIXTURES / "transcript_sample.json").read_text())
    async with services.db.session() as session:
        session.add(
            MeetingRow(
                id="mtg_ingest",
                title="mtg_ingest",
                status="queued",
                audio_path=str(audio),
                participants=["ada"],
                created_by="ada",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    queue = ArqLikeQueue(default_queue=INGEST_QUEUE)  # the ingest worker's own pool

    await transcribe_meeting(
        {"db": services.db, "transcriber": FixtureTranscriber(), "redis": queue}, "mtg_ingest"
    )

    assert queue.jobs == [("extract_meeting", ("mtg_ingest",), WORKER_QUEUE)]
