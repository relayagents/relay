"""Pluggability boundaries. Every external system sits behind one of these protocols and ships
with exactly one reference implementation (see docs/protocols.md).

Protocol before implementation: add the method here first, then to the reference connector.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from relayagents.core.events import Event

# ---- Shared DTOs ---------------------------------------------------------------------------


class Segment(BaseModel):
    segment_id: str
    speaker: str
    start_s: float
    end_s: float
    text: str
    confidence: float | None = None


class Transcript(BaseModel):
    meeting_id: str
    language: str | None = None
    segments: list[Segment]
    engine: str = "unknown"


class MemoryHit(BaseModel):
    """A search result with provenance so the caller can cite it."""

    text: str
    score: float = Field(ge=0)
    kind: Literal["graph", "vector", "event"]
    event_ids: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    ref: str | None = Field(
        default=None, description="Backend-specific reference (edge uuid, event id)."
    )


class ChatMessageRef(BaseModel):
    channel: str
    ts: str
    permalink: str | None = None


class IssueRef(BaseModel):
    url: str
    number: int
    repo: str


class CodingRun(BaseModel):
    agent: str
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_s: float
    artifacts: list[str] = Field(default_factory=list)


class A2ATask(BaseModel):
    """Minimal broker-side view of an A2A task delivered to a user agent."""

    task_id: str
    context_id: str
    from_agent: str
    to_agent: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---- Protocols -----------------------------------------------------------------------------


@runtime_checkable
class MemoryStore(Protocol):
    """Derived team memory (graph and/or vectors). Must be fully rebuildable from the event log."""

    async def index(self, events: Sequence[Event]) -> None: ...

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryHit]: ...

    async def reset(self) -> None:
        """Drop everything. Called by ``relay replay --rebuild-graph`` before re-indexing."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class Embedder(Protocol):
    """Team embedding model. Workers only; relay-api never holds the key."""

    model_name: str

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


@runtime_checkable
class SemanticSearch(Protocol):
    """The vector + graph legs of `recall`, reachable from relay-api without a model key."""

    async def __call__(
        self, query: str, *, limit: int = 10, kinds: Sequence[str] = ("vector", "graph")
    ) -> list[MemoryHit]: ...


@runtime_checkable
class Transcriber(Protocol):
    async def transcribe(
        self,
        audio_path: Path,
        *,
        meeting_id: str,
        language: str | None = None,
        diarize: bool = True,
    ) -> Transcript: ...


@runtime_checkable
class OfficeSuite(Protocol):
    """Documents/calendar/mail for one user, under that user's own OAuth grant."""

    async def search_documents(
        self, user_id: str, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    async def read_document(self, user_id: str, doc_ref: str) -> str: ...

    async def upcoming_meetings(self, user_id: str, *, hours: int = 24) -> list[dict[str, Any]]: ...


@runtime_checkable
class ChatApp(Protocol):
    """The team chat where humans see what agents do. Reference: Slack via one Socket Mode app."""

    async def post(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        attribution: str | None = None,
    ) -> ChatMessageRef: ...

    async def dm(
        self, user_id: str, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> ChatMessageRef: ...

    async def update(
        self, ref: ChatMessageRef, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> None: ...


@runtime_checkable
class IssueTracker(Protocol):
    async def create_issue(
        self, user_id: str, repo: str, title: str, body: str, *, labels: Sequence[str] = ()
    ) -> IssueRef: ...

    async def list_issues(
        self, user_id: str, repo: str, *, assignee: str | None = None, state: str = "open"
    ) -> list[IssueRef]: ...


@runtime_checkable
class CodingAgent(Protocol):
    """A headless coding-agent invocation in a sandbox. Reference: ``claude -p``, ``codex exec``, ``opencode run``."""

    name: str

    async def run(
        self, prompt: str, *, workdir: Path, env: dict[str, str], timeout_s: int = 1800
    ) -> CodingRun: ...


@runtime_checkable
class UserAgent(Protocol):
    """A per-user agent harness that satisfies docs/agent-contract.md. Reference: Hermes Agent."""

    name: str

    async def provision(self, user_id: str, *, relay_url: str, relay_token: str) -> dict[str, Any]:
        """Create the runtime (container, config) for this user's agent. Idempotent."""
        ...

    async def deliver(self, task: A2ATask) -> bool:
        """Push a task to the agent if it has a push endpoint; return False to leave it in the inbox."""
        ...


@runtime_checkable
class Extractor(Protocol):
    """Transcript → typed events. Reference: Pydantic AI structured extraction."""

    async def extract(
        self, transcript: Transcript, *, meeting_id: str, participants: Sequence[str]
    ) -> AsyncIterator[Event]: ...
