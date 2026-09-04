"""``Transcriber`` on WhisperX (faster-whisper + wav2vec2 alignment) with pyannote diarization.

Heavy imports are deferred so the rest of Relay never depends on torch. Install with
``uv sync --extra ingest`` (or use the ``relay-ingest`` image).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from relayagents.core.protocols import Segment, Transcript


class WhisperXTranscriber:
    name = "whisperx"

    def __init__(
        self,
        *,
        model: str = "large-v3",
        device: str = "auto",
        compute_type: str = "auto",
        hf_token: str = "",
        batch_size: int = 16,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.hf_token = hf_token
        self.batch_size = batch_size
        self._model: Any = None

    def _resolve(self) -> tuple[str, str]:
        import torch

        device = (
            self.device
            if self.device != "auto"
            else ("cuda" if torch.cuda.is_available() else "cpu")
        )
        compute = (
            self.compute_type
            if self.compute_type != "auto"
            else ("float16" if device == "cuda" else "int8")
        )
        return device, compute

    def _transcribe_sync(
        self, audio_path: Path, meeting_id: str, language: str | None, diarize: bool
    ) -> Transcript:
        import whisperx

        device, compute = self._resolve()
        if self._model is None:
            self._model = whisperx.load_model(
                self.model_name, device, compute_type=compute, language=language
            )
        audio = whisperx.load_audio(str(audio_path))
        result = self._model.transcribe(audio, batch_size=self.batch_size, language=language)
        lang = result.get("language", language)
        align_model, metadata = whisperx.load_align_model(language_code=lang, device=device)
        result = whisperx.align(
            result["segments"], align_model, metadata, audio, device, return_char_alignments=False
        )
        if diarize:
            if not self.hf_token:
                raise RuntimeError("diarization needs RELAY_HF_TOKEN (pyannote models are gated)")
            from whisperx.diarize import DiarizationPipeline

            diarizer = DiarizationPipeline(use_auth_token=self.hf_token, device=device)
            result = whisperx.assign_word_speakers(diarizer(audio), result)
        segments = [
            Segment(
                segment_id=f"{meeting_id}:seg{i:04d}",
                speaker=str(s.get("speaker", "SPEAKER_UNKNOWN")),
                start_s=float(s["start"]),
                end_s=float(s["end"]),
                text=s["text"].strip(),
            )
            for i, s in enumerate(result["segments"])
            if s.get("text", "").strip()
        ]
        return Transcript(
            meeting_id=meeting_id,
            language=lang,
            segments=segments,
            engine=f"whisperx/{self.model_name}",
        )

    async def transcribe(
        self,
        audio_path: Path,
        *,
        meeting_id: str,
        language: str | None = None,
        diarize: bool = True,
    ) -> Transcript:
        return await asyncio.to_thread(
            self._transcribe_sync, audio_path, meeting_id, language, diarize
        )
