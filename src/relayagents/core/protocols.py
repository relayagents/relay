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
from relayagents.core.store import event_summary

# Payload fields that point at other things. Kept next to the model that exposes them.
RELATED_ID_KEYS: tuple[str, ...] = (
    "item_id",
    "decision_id",
    "question_id",
    "supersedes",
    "task_id",
    "approval_id",
    "meeting_id",
    "call_id",
    "cited_event_ids",
)

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
    """A search result with provenance and the event's structural links, so an agent can follow
    them (thread, item, superseded decision, source segments) without a graph (ADR-0005)."""

    text: str
    score: float = Field(ge=0)
    kind: Literal["graph", "vector", "event"]
    event_ids: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    ref: str | None = Field(
        default=None, description="Backend-specific reference (edge uuid, event id)."
    )
    event_type: str | None = None
    actor: str | None = None
    thread_id: str | None = None
    related_ids: list[str] = Field(
        default_factory=list,
        description="item/decision/question ids, supersedes, parent events, source segments.",
    )

    @classmethod
    def from_event(
        cls, event: Event, *, score: float, kind: Literal["vector", "event"]
    ) -> MemoryHit:
        p = event.payload.model_dump()
        related: list[str] = []
        for key in RELATED_ID_KEYS:
            v = p.get(key)
            if isinstance(v, str) and v:
                related.append(v)
            elif isinstance(v, list):
                related.extend(x for x in v if isinstance(x, str))
        prov = event.provenance
        related += prov.parent_event_ids + prov.tool_call_ids + prov.segment_ids
        return cls(
            text=event_summary(event),
            score=round(score, 3),
            kind=kind,
            event_ids=[event.id],
            valid_from=event.ts,
            ref=event.id,
            event_type=event.type,
            actor=event.actor.id,
            thread_id=event.thread_id,
            related_ids=list(dict.fromkeys(related)),
        )


class RecentDecision(BaseModel):
    decision_id: str
    topic: str | None
    statement: str


class ExtractionContext(BaseModel):
    """What the extractor knows about the team before reading a transcript: existing topics (so it
    reuses names instead of inventing near-duplicates) and recent decisions (so it can say which one
    a new decision replaces). This is where the graph's two real jobs now live (ADR-0005)."""

    known_topics: list[str] = Field(default_factory=list)
    recent_decisions: list[RecentDecision] = Field(default_factory=list)


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

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float] | None]:
        """One vector per input; ``None`` where the provider rejected that input."""
        ...


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

    supports_actions: bool
    """True when button clicks can reach Relay (Slack Socket Mode connected). If False, messages
    must tell the human how to act from the CLI instead of offering buttons."""

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
        self,
        transcript: Transcript,
        *,
        meeting_id: str,
        participants: Sequence[str],
        context: ExtractionContext | None = None,
    ) -> AsyncIterator[Event]: ...
