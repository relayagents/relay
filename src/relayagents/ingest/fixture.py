"""``Transcriber`` that reads a transcript JSON sitting next to the audio (or given directly).
Used by ``relay meeting upload --transcript ... --skip-asr`` and tests."""

from __future__ import annotations

import json
from pathlib import Path

from relayagents.core.protocols import Transcript


class FixtureTranscriber:
    name = "fixture"

    def __init__(self, transcript_path: Path | None = None) -> None:
        self.transcript_path = transcript_path

    async def transcribe(
        self,
        audio_path: Path,
        *,
        meeting_id: str,
        language: str | None = None,
        diarize: bool = True,
    ) -> Transcript:
        path = self.transcript_path or audio_path.with_suffix(".json")
        data = json.loads(Path(path).read_text())
        data["meeting_id"] = meeting_id
        data.setdefault("engine", "fixture")
        return Transcript.model_validate(data)
