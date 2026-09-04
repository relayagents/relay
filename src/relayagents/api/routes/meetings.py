"""Meeting ingest: upload audio (queued to WhisperX) or a transcript JSON (skip ASR)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from relayagents.api.auth import current_principal, get_services
from relayagents.core.events import Event, MeetingStarted
from relayagents.core.ids import new_id
from relayagents.core.models import MeetingRow
from relayagents.core.protocols import Transcript
from relayagents.core.store import EventStore
from relayagents.tools.context import Principal, Services

router = APIRouter(prefix="/v1/meetings", tags=["meetings"])

ALLOWED_AUDIO = {".m4a", ".mp3", ".wav", ".flac", ".ogg", ".webm", ".mp4"}
CHUNK = 1 << 20


async def _save_upload(upload: UploadFile, dest: Path, *, max_bytes: int) -> int:
    """Stream to disk in chunks; never hold the whole file in memory."""
    size = 0
    with dest.open("wb") as f:
        while chunk := await upload.read(CHUNK):
            size += len(chunk)
            if size > max_bytes:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    413, f"upload exceeds {max_bytes >> 20} MB (RELAY_MAX_UPLOAD_MB)"
                )
            f.write(chunk)
    return size


class MeetingOut(BaseModel):
    id: str
    title: str
    status: str
    participants: list[str]
    created_by: str
    created_at: datetime
    error: str | None = None


def _out(m: MeetingRow) -> MeetingOut:
    return MeetingOut(
        id=m.id,
        title=m.title,
        status=m.status,
        participants=list(m.participants),
        created_by=m.created_by,
        created_at=m.created_at,
        error=m.error,
    )


async def _enqueue(request: Request, job: str, *args: Any, queue: str | None = None) -> None:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return  # tests / no-redis dev: the CLI can run `relay worker --once`
    await redis.enqueue_job(job, *args, _queue_name=queue) if queue else await redis.enqueue_job(
        job, *args
    )


@router.post("", status_code=202)
async def upload_meeting(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
    title: Annotated[str, Form()] = "Untitled meeting",
    participants: Annotated[str, Form(description="Comma-separated user ids")] = "",
    audio: Annotated[UploadFile | None, File()] = None,
    transcript: Annotated[
        UploadFile | None, File(description="Transcript JSON (skips ASR)")
    ] = None,
) -> MeetingOut:
    if audio is None and transcript is None:
        raise HTTPException(400, "provide audio or transcript")
    meeting_id = new_id("mtg")
    now = datetime.now(UTC)
    people = [p.strip() for p in participants.split(",") if p.strip()] or [principal.user_id]
    base = services.settings.data_dir / "meetings" / meeting_id
    base.mkdir(parents=True, exist_ok=True)
    audio_path: str | None = None
    transcript_path: str | None = None
    if audio is not None:
        suffix = Path(audio.filename or "audio.m4a").suffix.lower() or ".m4a"
        if suffix not in ALLOWED_AUDIO:
            raise HTTPException(400, f"unsupported audio type {suffix}")
        dest = base / f"audio{suffix}"
        await _save_upload(audio, dest, max_bytes=services.settings.max_upload_mb << 20)
        audio_path = str(dest)
    if transcript is not None:
        tdest = base / "transcript.upload.json"
        await _save_upload(
            transcript, tdest, max_bytes=min(64, services.settings.max_upload_mb) << 20
        )
        try:
            data = json.loads(tdest.read_bytes())
            data.setdefault("meeting_id", meeting_id)
            Transcript.model_validate(data)
        except Exception as exc:
            raise HTTPException(422, f"invalid transcript: {exc}") from exc
        dest = base / "transcript.json"
        dest.write_text(json.dumps(data))
        tdest.unlink(missing_ok=True)
        transcript_path = str(dest)
    row = MeetingRow(
        id=meeting_id,
        title=title,
        status="queued",
        audio_path=audio_path,
        transcript_path=transcript_path,
        participants=people,
        created_by=principal.user_id,
        created_at=now,
    )
    async with services.db.session() as session:
        session.add(row)
        await EventStore(session).append(
            Event.new(
                MeetingStarted(
                    meeting_id=meeting_id,
                    title=title,
                    participants=people,
                    started_at=now,
                    recording_ref=f"meetings/{meeting_id}" if audio_path else None,
                ),
                actor=principal.actor,
                source="meeting",
                thread_id=meeting_id,
            )
        )
        await session.commit()
    if transcript_path:
        await _enqueue(request, "extract_meeting", meeting_id)
    else:
        await _enqueue(request, "transcribe_meeting", meeting_id, queue="relay:ingest")
    return _out(row)


@router.get("")
async def list_meetings(
    _: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> list[MeetingOut]:
    from sqlalchemy import select

    async with services.db.session() as session:
        rows = (
            await session.scalars(
                select(MeetingRow).order_by(MeetingRow.created_at.desc()).limit(100)
            )
        ).all()
    return [_out(m) for m in rows]


@router.get("/{meeting_id}")
async def get_meeting(
    meeting_id: str,
    _: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> MeetingOut:
    async with services.db.session() as session:
        row = await session.get(MeetingRow, meeting_id)
    if row is None:
        raise HTTPException(404, "no such meeting")
    return _out(row)
