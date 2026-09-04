"""relay-ingest: audio → diarized transcript. Runs on the GPU worker if present, else CPU."""

from relayagents.ingest.fixture import FixtureTranscriber

__all__ = ["FixtureTranscriber"]
