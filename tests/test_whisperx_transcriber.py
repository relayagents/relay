"""WhisperXTranscriber without a Hugging Face token.

First compose boot: HF_TOKEN is blank in .env.example, and diarization defaulted on, so every
recording was transcribed and aligned and then the meeting failed with "diarization needs
RELAY_HF_TOKEN". Without a token the transcriber must skip diarization and keep the transcript.
A fake ``whisperx`` module stands in for the real one so this runs without torch.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from relayagents.ingest.whisperx_transcriber import WhisperXTranscriber


@pytest.fixture
def fake_whisperx(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    segments = [{"start": 0.0, "end": 2.5, "text": " Action: ada will write the notes."}]

    class Model:
        def transcribe(self, audio: Any, **kwargs: Any) -> dict[str, Any]:
            return {"segments": segments, "language": "en"}

    wx = types.ModuleType("whisperx")
    wx.load_model = lambda *a, **k: Model()  # type: ignore[attr-defined]
    wx.load_audio = lambda path: b"pcm"  # type: ignore[attr-defined]
    wx.load_align_model = lambda **k: (object(), {})  # type: ignore[attr-defined]
    wx.align = lambda segs, *a, **k: {"segments": segs}  # type: ignore[attr-defined]

    def assign_word_speakers(diarized: Any, result: dict[str, Any]) -> dict[str, Any]:
        calls.append("diarize")
        return {"segments": [{**s, "speaker": "SPEAKER_00"} for s in result["segments"]]}

    wx.assign_word_speakers = assign_word_speakers  # type: ignore[attr-defined]
    diarize_mod = types.ModuleType("whisperx.diarize")

    class DiarizationPipeline:  # whisperx 3.8's signature (the locked version): token, not
        # use_auth_token, so a wrong keyword fails here as it does in the image.
        def __init__(self, model_name=None, token=None, device="cpu", cache_dir=None):  # type: ignore[no-untyped-def]
            calls.append(f"token={token}")

        def __call__(self, audio: Any) -> str:
            return "turns"

    diarize_mod.DiarizationPipeline = DiarizationPipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "whisperx", wx)
    monkeypatch.setitem(sys.modules, "whisperx.diarize", diarize_mod)
    return calls


async def test_no_hf_token_skips_diarization_instead_of_failing(
    fake_whisperx: list[str], tmp_path: Path
) -> None:
    t = WhisperXTranscriber(model="small", device="cpu", compute_type="int8", hf_token="")
    out = await t.transcribe(tmp_path / "a.wav", meeting_id="mtg_1")
    assert [s.text for s in out.segments] == ["Action: ada will write the notes."]
    assert out.segments[0].speaker == "SPEAKER_UNKNOWN"
    assert fake_whisperx == []


async def test_hf_token_still_diarizes(fake_whisperx: list[str], tmp_path: Path) -> None:
    t = WhisperXTranscriber(model="small", device="cpu", compute_type="int8", hf_token="hf_x")
    out = await t.transcribe(tmp_path / "a.wav", meeting_id="mtg_1")
    assert out.segments[0].speaker == "SPEAKER_00"
    assert fake_whisperx == ["token=hf_x", "diarize"]
