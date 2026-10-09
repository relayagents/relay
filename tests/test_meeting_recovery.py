"""Meetings whose queue ticket was lost get re-driven from Postgres (issue #21)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from relayagents.core.models import MeetingRow
from relayagents.core.queue import INGEST_QUEUE, WORKER_QUEUE
from relayagents.ingest.worker import transcribe_meeting
from relayagents.tools.context import Services
from relayagents.workers.jobs import extract_meeting, requeue_stale_meetings
from relayagents.workers.main import WorkerSettings

from .conftest import FIXTURES, auth


class ArqLikeQueue:
    """Mirrors two arq contracts: while a job with a given ``_job_id`` is queued, running, or
    holding a result, ``enqueue_job`` is a no-op that returns ``None``; and ``_queue_name=None``
    means the pool's default queue, which inside a worker is that worker's own queue."""

    def __init__(self, held: set[str] | None = None, default_queue: str = WORKER_QUEUE) -> None:
        self.held = set(held or ())
        self.default_queue = default_queue
        self.jobs: list[tuple[str, tuple[Any, ...], str | None, str | None]] = []

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: str | None = None,
        _queue_name: str | None = None,
        **_: Any,
    ) -> object | None:
        if _job_id is not None and _job_id in self.held:
            return None
        if _job_id is not None:
            self.held.add(_job_id)
        self.jobs.append((function, args, _job_id, _queue_name or self.default_queue))
        return object()


async def _add_meeting(
    services: Services,
    meeting_id: str,
    status: str,
    *,
    transcript_path: str | None = None,
    audio_path: str | None = None,
) -> None:
    async with services.db.session() as session:
        session.add(
            MeetingRow(
                id=meeting_id,
                title=meeting_id,
                status=status,
                transcript_path=transcript_path,
                audio_path=audio_path,
                participants=["ada"],
                created_by="ada",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def test_sweeper_requeues_each_queued_meeting_to_its_next_job(services: Services) -> None:
    await _add_meeting(services, "mtg_upload", "queued", transcript_path="/t.json")
    await _add_meeting(services, "mtg_audio", "queued", audio_path="/a.wav")
    await _add_meeting(services, "mtg_done", "done", transcript_path="/t.json")
    await _add_meeting(services, "mtg_failed", "failed", audio_path="/a.wav")
    queue = ArqLikeQueue()

    result = await requeue_stale_meetings({"services": services, "redis": queue})

    assert sorted(queue.jobs) == [
        ("extract_meeting", ("mtg_upload",), "extract_meeting:mtg_upload", WORKER_QUEUE),
        ("transcribe_meeting", ("mtg_audio",), "transcribe_meeting:mtg_audio", INGEST_QUEUE),
    ]
    assert sorted(result["requeued"]) == ["mtg_audio", "mtg_upload"]


async def test_sweeper_never_starts_a_second_run_of_a_running_job(services: Services) -> None:
    """A meeting is only ``transcribing``/``extracting`` while its job runs. If Redis loses that
    job's key, the run still finishes and advances the meeting itself; re-driving it would race the
    live run and post the summary twice (#22)."""
    await _add_meeting(services, "mtg_asr", "transcribing", audio_path="/a.wav")
    await _add_meeting(services, "mtg_llm", "extracting", transcript_path="/t.json")
    queue = ArqLikeQueue()  # empty: as if Redis were wiped mid-run

    result = await requeue_stale_meetings({"services": services, "redis": queue})

    assert queue.jobs == [] and result["requeued"] == []


async def test_sweeper_leaves_meetings_whose_job_arq_still_holds(services: Services) -> None:
    await _add_meeting(services, "mtg_live", "queued", transcript_path="/t.json")
    await _add_meeting(services, "mtg_lost", "queued", transcript_path="/t.json")
    queue = ArqLikeQueue(held={"extract_meeting:mtg_live"})

    result = await requeue_stale_meetings({"services": services, "redis": queue})

    assert [j[0:2] for j in queue.jobs] == [("extract_meeting", ("mtg_lost",))]
    assert result["requeued"] == ["mtg_lost"]


async def test_upload_enqueues_under_the_id_the_sweeper_uses(
    client: httpx.AsyncClient, app, team, services: Services
) -> None:  # type: ignore[no-untyped-def]
    queue = ArqLikeQueue()
    app.state.redis = queue
    transcript = (FIXTURES / "transcript_sample.json").read_bytes()
    r = await client.post(
        "/v1/meetings",
        files={"transcript": ("t.json", transcript, "application/json")},
        headers=auth(team["ada"]["human"]),
    )
    assert r.status_code == 202
    meeting_id = r.json()["id"]

    assert queue.jobs == [
        ("extract_meeting", (meeting_id,), f"extract_meeting:{meeting_id}", WORKER_QUEUE)
    ]
    await requeue_stale_meetings({"services": services, "redis": queue})
    assert len(queue.jobs) == 1  # the sweeper's enqueue was deduplicated against the upload's


async def test_audio_upload_enqueues_transcription_under_a_fixed_id(
    client: httpx.AsyncClient, app, team
) -> None:  # type: ignore[no-untyped-def]
    queue = ArqLikeQueue()
    app.state.redis = queue
    r = await client.post(
        "/v1/meetings",
        files={"audio": ("m.wav", b"RIFF0000WAVE", "audio/wav")},
        headers=auth(team["ada"]["human"]),
    )
    assert r.status_code == 202
    meeting_id = r.json()["id"]
    assert queue.jobs == [
        ("transcribe_meeting", (meeting_id,), f"transcribe_meeting:{meeting_id}", INGEST_QUEUE)
    ]


async def test_transcription_hands_off_under_a_fixed_id(services: Services, tmp_path: Path) -> None:
    from relayagents.ingest.fixture import FixtureTranscriber

    audio = tmp_path / "mtg_ingest" / "audio.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF0000WAVE")
    audio.with_suffix(".json").write_text((FIXTURES / "transcript_sample.json").read_text())
    await _add_meeting(services, "mtg_ingest", "queued", audio_path=str(audio))
    queue = ArqLikeQueue(default_queue=INGEST_QUEUE)  # the ingest worker's own pool

    await transcribe_meeting(
        {"db": services.db, "transcriber": FixtureTranscriber(), "redis": queue}, "mtg_ingest"
    )

    assert queue.jobs == [
        ("extract_meeting", ("mtg_ingest",), "extract_meeting:mtg_ingest", WORKER_QUEUE)
    ]


async def test_extracting_a_done_meeting_again_does_not_redispatch(
    services: Services, team
) -> None:  # type: ignore[no-untyped-def]
    path = services.settings.data_dir / "m" / "transcript.json"
    path.parent.mkdir(parents=True)
    path.write_text((FIXTURES / "transcript_sample.json").read_text())
    await _add_meeting(services, "mtg_twice", "queued", transcript_path=str(path))
    await extract_meeting({"services": services}, "mtg_twice")
    posts = len(services.chat.posts)  # type: ignore[union-attr]

    result = await extract_meeting({"services": services}, "mtg_twice")

    assert result == {"meeting_id": "mtg_twice", "skipped": "done"}
    assert len(services.chat.posts) == posts  # type: ignore[union-attr]


def test_sweeper_runs_at_worker_startup() -> None:
    sweeps = [c for c in WorkerSettings.cron_jobs if c.name.endswith("requeue_stale_meetings")]
    assert len(sweeps) == 1 and sweeps[0].run_at_startup


async def test_transcribing_a_meeting_without_audio_marks_it_failed(services: Services) -> None:
    """Otherwise it stays ``queued`` and the sweeper retries it every hour forever."""
    await _add_meeting(services, "mtg_silent", "queued")

    with pytest.raises(RuntimeError, match="no audio"):
        await transcribe_meeting({"db": services.db, "redis": ArqLikeQueue()}, "mtg_silent")

    async with services.db.session() as session:
        meeting = await session.get(MeetingRow, "mtg_silent")
    assert meeting is not None and meeting.status == "failed"
    assert meeting.error == "RuntimeError: meeting has no audio"


async def test_failed_handoff_leaves_the_meeting_for_the_sweeper(
    services: Services, tmp_path: Path
) -> None:
    """Transcription succeeded, so a Redis error on the handoff must not mark the meeting failed:
    it stays ``queued`` with its transcript and the next sweep enqueues the extraction."""
    from relayagents.ingest.fixture import FixtureTranscriber

    class DownQueue:
        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("redis unavailable")

    audio = tmp_path / "mtg_handoff" / "audio.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF0000WAVE")
    audio.with_suffix(".json").write_text((FIXTURES / "transcript_sample.json").read_text())
    await _add_meeting(services, "mtg_handoff", "queued", audio_path=str(audio))

    with pytest.raises(ConnectionError):
        await transcribe_meeting(
            {"db": services.db, "transcriber": FixtureTranscriber(), "redis": DownQueue()},
            "mtg_handoff",
        )

    async with services.db.session() as session:
        meeting = await session.get(MeetingRow, "mtg_handoff")
    assert meeting is not None and meeting.status == "queued" and meeting.transcript_path
    queue = ArqLikeQueue()
    await requeue_stale_meetings({"services": services, "redis": queue})
    assert [j[0:2] for j in queue.jobs] == [("extract_meeting", ("mtg_handoff",))]


class CountingTranscriber:
    """FixtureTranscriber that counts runs and can run a side effect mid-transcription."""

    def __init__(self, during: Any = None) -> None:
        from relayagents.ingest.fixture import FixtureTranscriber

        self.inner = FixtureTranscriber()
        self.during = during
        self.calls = 0

    async def transcribe(self, audio: Path, *, meeting_id: str) -> Any:
        self.calls += 1
        if self.during is not None:
            await self.during()
        return await self.inner.transcribe(audio, meeting_id=meeting_id)


async def _audio_meeting(
    services: Services, tmp_path: Path, meeting_id: str, status: str, **kwargs: Any
) -> None:
    audio = tmp_path / meeting_id / "audio.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF0000WAVE")
    audio.with_suffix(".json").write_text((FIXTURES / "transcript_sample.json").read_text())
    await _add_meeting(services, meeting_id, status, audio_path=str(audio), **kwargs)


async def _status(services: Services, meeting_id: str) -> str:
    async with services.db.session() as session:
        meeting = await session.get(MeetingRow, meeting_id)
    assert meeting is not None
    return meeting.status


@pytest.mark.parametrize("status", ["done", "extracting", "failed", "transcribing"])
async def test_a_duplicate_transcription_ticket_backs_off(
    services: Services, tmp_path: Path, status: str
) -> None:
    """An old random-id ticket still on relay:ingest at deploy can run after the fixed-id chain
    has finished. Resetting the meeting to `queued` would get it extracted a second time."""
    await _audio_meeting(services, tmp_path, "mtg_dup", status)
    transcriber, queue = CountingTranscriber(), ArqLikeQueue(default_queue=INGEST_QUEUE)

    await transcribe_meeting(
        {"db": services.db, "transcriber": transcriber, "redis": queue, "job_try": 1}, "mtg_dup"
    )

    assert await _status(services, "mtg_dup") == status
    assert transcriber.calls == 0 and queue.jobs == []


async def test_a_queued_meeting_with_a_transcript_is_not_transcribed_again(
    services: Services, tmp_path: Path
) -> None:
    """Between transcription and extraction the meeting is `queued`; a stale ticket running then
    would race the extraction and could reset a `done` meeting when it finished."""
    await _audio_meeting(services, tmp_path, "mtg_between", "queued", transcript_path="/t.json")
    transcriber, queue = CountingTranscriber(), ArqLikeQueue(default_queue=INGEST_QUEUE)

    result = await transcribe_meeting(
        {"db": services.db, "transcriber": transcriber, "redis": queue}, "mtg_between"
    )

    assert result == "/t.json"
    assert transcriber.calls == 0 and queue.jobs == []


async def test_arq_retrying_a_timed_out_transcription_still_runs_it(
    services: Services, tmp_path: Path
) -> None:
    """arq cancels a job past job_timeout and retries it with job_try > 1; the cancelled run
    left the meeting `transcribing`, and the retry must reclaim it."""
    await _audio_meeting(services, tmp_path, "mtg_slow", "transcribing")
    transcriber, queue = CountingTranscriber(), ArqLikeQueue(default_queue=INGEST_QUEUE)

    await transcribe_meeting(
        {"db": services.db, "transcriber": transcriber, "redis": queue, "job_try": 2}, "mtg_slow"
    )

    assert transcriber.calls == 1 and await _status(services, "mtg_slow") == "queued"
    assert [j[0:2] for j in queue.jobs] == [("extract_meeting", ("mtg_slow",))]


async def test_a_transcription_that_lost_the_meeting_does_not_advance_it(
    services: Services, tmp_path: Path
) -> None:
    async def move_on() -> None:
        async with services.db.session() as session:
            meeting = await session.get(MeetingRow, "mtg_moved")
            assert meeting is not None
            meeting.status = "done"
            await session.commit()

    await _audio_meeting(services, tmp_path, "mtg_moved", "queued")
    queue = ArqLikeQueue(default_queue=INGEST_QUEUE)

    await transcribe_meeting(
        {"db": services.db, "transcriber": CountingTranscriber(move_on), "redis": queue},
        "mtg_moved",
    )

    async with services.db.session() as session:
        meeting = await session.get(MeetingRow, "mtg_moved")
    assert meeting is not None and meeting.status == "done" and meeting.transcript_path is None
    assert queue.jobs == []


async def _meeting_with_transcript(services: Services, meeting_id: str, status: str) -> None:
    path = services.settings.data_dir / meeting_id / "transcript.json"
    path.parent.mkdir(parents=True)
    path.write_text((FIXTURES / "transcript_sample.json").read_text())
    await _add_meeting(services, meeting_id, status, transcript_path=str(path))


async def test_a_second_ticket_backs_off_while_extraction_runs(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    """The sweep can read a ``queued`` row just before its own job claims it; if Redis is flushed
    in that window the sweep enqueues a second ticket. That ticket must not run the meeting."""
    await _meeting_with_transcript(services, "mtg_race", "extracting")

    result = await extract_meeting({"services": services, "job_try": 1}, "mtg_race")

    assert result == {"meeting_id": "mtg_race", "skipped": "extracting"}
    assert services.chat.posts == []  # type: ignore[union-attr]


async def test_arq_retrying_a_crashed_extraction_still_runs_it(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    """A worker that died mid-job leaves the meeting ``extracting``; arq re-runs the same job with
    ``job_try`` > 1, and that retry must go through."""
    await _meeting_with_transcript(services, "mtg_retry", "extracting")

    result = await extract_meeting({"services": services, "job_try": 2}, "mtg_retry")

    assert result["dispatch"]["summary_posted"] is True
