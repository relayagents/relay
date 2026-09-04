"""Input/output models for every tool. These are the contract; the generators derive
JSON Schema (MCP), CLI options, and OpenAPI from them."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from relayagents.core.events import Event
from relayagents.core.protocols import MemoryHit


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- recall ---------------------------------------------------------------------------------


class RecallInput(ToolInput):
    query: str = Field(description="Natural-language question or keywords.")
    limit: int = Field(default=10, ge=1, le=50)
    kinds: list[Literal["graph", "vector", "event"]] | None = Field(
        default=None, description="Restrict to some legs of hybrid search."
    )


class RecallOutput(ToolOutput):
    hits: list[MemoryHit]


# ---- items ----------------------------------------------------------------------------------

ItemStatus = Literal["open", "in_progress", "blocked", "closed", "all"]


class ActionItem(BaseModel):
    id: str
    title: str
    assignee: str | None
    status: str
    due: datetime | None = None
    details: str | None = None
    meeting_id: str | None = None
    source_event_id: str
    updated_at: datetime


class MyItemsInput(ToolInput):
    status: ItemStatus = "open"
    limit: int = Field(default=50, ge=1, le=200)


class ItemsInput(ToolInput):
    assignee: str | None = Field(default=None, description="User id, or 'me'. Omit for everyone.")
    status: ItemStatus = "open"
    limit: int = Field(default=50, ge=1, le=200)


class ItemsOutput(ToolOutput):
    items: list[ActionItem]


# ---- events ---------------------------------------------------------------------------------


class EventsInput(ToolInput):
    since: str | None = Field(
        default="24h", description="ISO timestamp or relative window like 24h, 7d."
    )
    type: list[str] | None = Field(
        default=None, description="Event types to include, e.g. decision.made."
    )
    thread: str | None = Field(default=None, description="thread_id to filter by.")
    actor: str | None = Field(
        default=None, description="Actor id, or 'me' for you and your agents."
    )
    text: str | None = Field(default=None, description="Substring filter on event text.")
    include_tool_calls: bool = Field(
        default=False, description="Include tool.called/tool.result audit events."
    )
    limit: int = Field(default=50, ge=1, le=500)


class EventsOutput(ToolOutput):
    events: list[Event]


# ---- report ---------------------------------------------------------------------------------


class ReportInput(ToolInput):
    text: str = Field(description="What you did, in one or two sentences.")
    item_id: str | None = Field(default=None, description="Action item this relates to.")
    link: str | None = Field(default=None, description="PR, commit, doc, or issue URL.")
    close_item: bool = Field(default=False, description="Also close the action item.")


class ReportOutput(ToolOutput):
    event_id: str
    closed_item_id: str | None = None


# ---- ask ------------------------------------------------------------------------------------


class AskInput(ToolInput):
    user: str = Field(description="Teammate to ask (their user id, with or without a leading @).")
    question: str
    thread_id: str | None = Field(default=None, description="Continue an existing conversation.")
    wait_s: int = Field(
        default=0,
        ge=0,
        le=600,
        description="Block up to this long for an answer; 0 returns immediately.",
    )


class AskOutput(ToolOutput):
    task_id: str
    thread_id: str
    question_id: str
    to_agent: str
    state: str
    answer: str | None = None


# ---- request_approval -----------------------------------------------------------------------


class RequestApprovalInput(ToolInput):
    action: str = Field(description="Plain-language description of what will happen if approved.")
    action_type: str = Field(
        default="generic",
        description="Policy key, e.g. github.issue.create (see docs/permissions.md).",
    )
    details: dict[str, Any] = Field(default_factory=dict)
    wait: bool = Field(default=True, description="Block until resolved or timeout.")
    timeout_s: int = Field(
        default=3600,
        ge=1,
        le=3600,
        description="How long to wait; the approval itself expires at the same time.",
    )


class RequestApprovalOutput(ToolOutput):
    approval_id: str
    status: Literal["pending", "approved", "denied", "expired"]
    edited_action: str | None = None
    requested_of: str
    notified: bool = Field(
        description="Whether the human was reached in Slack. If false, they must resolve via the CLI or REST."
    )


# ---- decisions ------------------------------------------------------------------------------


class Decision(BaseModel):
    id: str
    statement: str
    topic: str | None
    rationale: str | None = None
    decided_by: list[str] = Field(default_factory=list)
    decided_at: datetime
    supersedes: str | None = None
    superseded_by: str | None = None
    source_event_id: str


class DecisionsInput(ToolInput):
    topic: str | None = Field(default=None, description="Substring match on topic or statement.")
    since: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class DecisionsOutput(ToolOutput):
    decisions: list[Decision]


# ---- post -----------------------------------------------------------------------------------


class PostInput(ToolInput):
    text: str
    channel: str | None = Field(
        default=None, description="Channel id; defaults to the team channel."
    )
    as_agent: bool = Field(
        default=True,
        description="Attribute the post to your agent ('posted by X's agent'). Posting as the human is forbidden.",
    )


class PostOutput(ToolOutput):
    channel: str
    ts: str
    event_id: str
