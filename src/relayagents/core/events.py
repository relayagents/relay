"""The Relay event schema. **The event log is the source of truth.**

Every meeting, transcript segment, decision, action item, tool call, approval, and agent
message is an append-only :class:`Event`. Graphs, projections, digests and dashboards are
derived from the log and can be rebuilt from it (``relay replay``).

Design notes
------------
* ``Event`` is an envelope; ``payload`` is a discriminated union keyed on ``payload.type``.
  ``Event.type`` always equals ``payload.type`` and exists on the envelope so the log can be
  filtered without parsing payloads.
* Payloads forbid extra fields. Adding a field is a schema change and gets a migration note in
  ``docs/data-model.md``.
* ``visibility`` is ``team`` or ``public``. Relay holds no private scope by design: private
  memory lives in each person's own agent (see ADR-0002).
* ``provenance`` points back at the things an event was derived from: transcript segments,
  tool calls, and parent events. Extraction never emits a decision without segment ids.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from relayagents.core.ids import new_id

# --------------------------------------------------------------------------------------------
# Shared value objects
# --------------------------------------------------------------------------------------------

ActorKind = Literal["human", "agent", "system"]
Source = Literal["meeting", "slack", "workspace", "github", "a2a", "cli", "api"]
Visibility = Literal["team", "public"]


class Actor(BaseModel):
    """Who caused the event. Agents are identified as ``<user_id>.<harness>`` (e.g. ``ada.hermes``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ActorKind
    id: str = Field(min_length=1, description="User id, agent id, or system component name.")

    @property
    def user_id(self) -> str:
        """The human this actor acts for (agents are always owned by a human)."""
        return self.id.split(".", 1)[0] if self.kind == "agent" else self.id

    @classmethod
    def human(cls, user_id: str) -> Actor:
        return cls(kind="human", id=user_id)

    @classmethod
    def agent(cls, agent_id: str) -> Actor:
        return cls(kind="agent", id=agent_id)

    @classmethod
    def system(cls, component: str = "relay") -> Actor:
        return cls(kind="system", id=component)


class Provenance(BaseModel):
    """Back-references that let a human audit where an event came from."""

    model_config = ConfigDict(extra="forbid")

    segment_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    parent_event_ids: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.segment_ids or self.tool_call_ids or self.parent_event_ids)


# --------------------------------------------------------------------------------------------
# Payloads (one class per event type)
# --------------------------------------------------------------------------------------------


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeetingStarted(Payload):
    type: Literal["meeting.started"] = "meeting.started"
    meeting_id: str
    title: str
    participants: list[str] = Field(default_factory=list, description="User ids or display names.")
    started_at: datetime
    recording_ref: str | None = Field(
        default=None, description="Opaque storage ref, never a URL to raw audio outside the node."
    )


class TranscriptSegment(Payload):
    type: Literal["transcript.segment"] = "transcript.segment"
    meeting_id: str
    segment_id: str
    speaker: str = Field(description="Diarized speaker label or resolved user id.")
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)


class DecisionMade(Payload):
    type: Literal["decision.made"] = "decision.made"
    decision_id: str
    statement: str
    topic: str | None = None
    rationale: str | None = None
    decided_by: list[str] = Field(default_factory=list)
    supersedes: str | None = Field(default=None, description="decision_id this replaces, if any.")


class ActionItemCreated(Payload):
    type: Literal["action_item.created"] = "action_item.created"
    item_id: str
    title: str
    assignee: str | None = Field(default=None, description="User id; None if unassigned.")
    due: datetime | None = None
    details: str | None = None
    meeting_id: str | None = None


class ActionItemUpdated(Payload):
    type: Literal["action_item.updated"] = "action_item.updated"
    item_id: str
    title: str | None = None
    assignee: str | None = None
    due: datetime | None = None
    status: Literal["open", "in_progress", "blocked"] | None = None
    note: str | None = None


class ActionItemClosed(Payload):
    type: Literal["action_item.closed"] = "action_item.closed"
    item_id: str
    resolution: Literal["done", "wont_do", "duplicate"] = "done"
    note: str | None = None
    links: list[str] = Field(default_factory=list)


class QuestionOpened(Payload):
    type: Literal["question.opened"] = "question.opened"
    question_id: str
    text: str
    asked_of: str | None = Field(default=None, description="User id the question is addressed to.")
    context: str | None = None


class QuestionAnswered(Payload):
    type: Literal["question.answered"] = "question.answered"
    question_id: str
    answer: str


class ReportPosted(Payload):
    """What someone (or their agent) did. Source of truth for standups and item closure."""

    type: Literal["report.posted"] = "report.posted"
    text: str
    item_id: str | None = None
    links: list[str] = Field(default_factory=list)


class ToolCalled(Payload):
    type: Literal["tool.called"] = "tool.called"
    call_id: str
    tool: str
    transport: Literal["mcp", "cli", "rest", "internal"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    target: str | None = Field(
        default=None, description="External system touched, e.g. 'github:relayagents/relay'."
    )


class ToolResult(Payload):
    type: Literal["tool.result"] = "tool.result"
    call_id: str
    tool: str
    ok: bool
    summary: str | None = None
    error: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class AgentMessage(Payload):
    """An A2A message brokered by Relay. Never peer-to-peer; always via the broker."""

    type: Literal["agent.message"] = "agent.message"
    task_id: str
    from_agent: str
    to_agent: str
    role: Literal["user", "agent"] = "user"
    text: str
    state: Literal[
        "submitted", "working", "input_required", "completed", "failed", "canceled", "rejected"
    ] = "submitted"
    surfaced_to: list[str] = Field(
        default_factory=list, description="Humans notified of this exchange."
    )


class ApprovalRequested(Payload):
    type: Literal["approval.requested"] = "approval.requested"
    approval_id: str
    action: str = Field(description="Human-readable description of what will happen if approved.")
    action_type: str = Field(
        description="Policy key such as 'github.issue.create'. See docs/permissions.md."
    )
    requested_of: str = Field(description="User id who must resolve this.")
    details: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class ApprovalResolved(Payload):
    type: Literal["approval.resolved"] = "approval.resolved"
    approval_id: str
    decision: Literal["approved", "denied", "expired"]
    resolved_by: str | None = None
    edited_action: str | None = Field(
        default=None, description="If the human edited the action text before approving."
    )
    note: str | None = None


class StandupPosted(Payload):
    type: Literal["standup.posted"] = "standup.posted"
    user_id: str
    mode: Literal["draft", "auto"]
    done: list[str] = Field(default_factory=list)
    doing: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    questions: list[str] = Field(
        default_factory=list, description="Things the agent could not source from events."
    )
    cited_event_ids: list[str] = Field(default_factory=list)
    channel: str | None = None
    message_ref: str | None = None


class DigestPosted(Payload):
    type: Literal["digest.posted"] = "digest.posted"
    window_start: datetime
    window_end: datetime
    shipped: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    decisions_needed: list[str] = Field(default_factory=list)
    cited_event_ids: list[str] = Field(default_factory=list)
    quiet: bool = Field(default=False, description="True when the digest is just 'no update'.")
    channel: str | None = None
    message_ref: str | None = None


AnyPayload = Annotated[
    MeetingStarted
    | TranscriptSegment
    | DecisionMade
    | ActionItemCreated
    | ActionItemUpdated
    | ActionItemClosed
    | QuestionOpened
    | QuestionAnswered
    | ReportPosted
    | ToolCalled
    | ToolResult
    | AgentMessage
    | ApprovalRequested
    | ApprovalResolved
    | StandupPosted
    | DigestPosted,
    Field(discriminator="type"),
]

PAYLOAD_TYPES: dict[str, type[Payload]] = {
    cls.model_fields["type"].default: cls  # type: ignore[misc]
    for cls in get_args(get_args(AnyPayload)[0])
}
EVENT_TYPES: tuple[str, ...] = tuple(PAYLOAD_TYPES)


# --------------------------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------------------------


class Event(BaseModel):
    """One immutable fact in the log."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("evt"))
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: str
    actor: Actor
    source: Source
    visibility: Visibility = "team"
    thread_id: str | None = Field(
        default=None,
        description="Groups related events: a meeting, an A2A conversation, an approval, an item.",
    )
    payload: AnyPayload
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="before")
    @classmethod
    def _fill_type(cls, data: Any) -> Any:
        if isinstance(data, dict):
            payload = data.get("payload")
            ptype = None
            if isinstance(payload, dict):
                ptype = payload.get("type")
            elif isinstance(payload, Payload):
                ptype = payload.type  # type: ignore[attr-defined]
            if ptype is not None:
                if "type" not in data or data["type"] is None:
                    data = {**data, "type": ptype}
                elif data["type"] != ptype:
                    raise ValueError(f"event.type {data['type']!r} != payload.type {ptype!r}")
        return data

    @model_validator(mode="after")
    def _check_ts_tz(self) -> Event:
        if self.ts.tzinfo is None:
            self.ts = self.ts.replace(tzinfo=UTC)
        return self

    @classmethod
    def new(
        cls,
        payload: Payload,
        *,
        actor: Actor,
        source: Source,
        thread_id: str | None = None,
        visibility: Visibility = "team",
        provenance: Provenance | None = None,
        ts: datetime | None = None,
    ) -> Event:
        return cls(
            ts=ts or datetime.now(UTC),
            type=payload.type,  # type: ignore[attr-defined]
            actor=actor,
            source=source,
            visibility=visibility,
            thread_id=thread_id,
            payload=payload,  # type: ignore[arg-type]
            provenance=provenance or Provenance(),
        )

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes) -> Event:
        return cls.model_validate_json(raw)


__all__ = [
    "EVENT_TYPES",
    "PAYLOAD_TYPES",
    "ActionItemClosed",
    "ActionItemCreated",
    "ActionItemUpdated",
    "Actor",
    "ActorKind",
    "AgentMessage",
    "AnyPayload",
    "ApprovalRequested",
    "ApprovalResolved",
    "DecisionMade",
    "DigestPosted",
    "Event",
    "MeetingStarted",
    "Payload",
    "Provenance",
    "QuestionAnswered",
    "QuestionOpened",
    "ReportPosted",
    "Source",
    "StandupPosted",
    "ToolCalled",
    "ToolResult",
    "TranscriptSegment",
    "Visibility",
]
