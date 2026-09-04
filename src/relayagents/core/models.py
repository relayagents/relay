"""SQLAlchemy tables.

``events`` is the log. ``action_items`` and ``decisions`` are projections (rebuildable). The
rest is operational state that is not derivable from events (credentials, agent registry,
delivery bookkeeping).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# pgvector on Postgres, JSON on SQLite (tests / tiny dev). 1536 = text-embedding-3-small.
# Changing this needs a migration; the embedder validates vectors against it.
EMBEDDING_DIM = 1536
EmbeddingType = Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite")


class TZDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class EventRow(Base):
    __tablename__ = "events"

    seq: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str] = mapped_column(String(32))
    visibility: Mapped[str] = mapped_column(String(16), default="team")
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON)
    text_index: Mapped[str] = mapped_column(Text, default="", doc="Flattened searchable text.")
    embedding = mapped_column(
        EmbeddingType, nullable=True, deferred=True
    )  # loaded only by vector_search

    __table_args__ = (Index("ix_events_type_ts", "type", "ts"),)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    slack_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    github_login: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    standup_mode: Mapped[str] = mapped_column(String(8), default="draft")  # draft | auto | off
    standup_time: Mapped[str] = mapped_column(String(5), default="09:00")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)


class ApiTokenRow(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    scopes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    actor_kind: Mapped[str] = mapped_column(String(16), default="human")  # human | agent
    actor_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class DeviceCodeRow(Base):
    """``relay login`` device flow: CLI polls until a human approves in Slack (or an admin pastes)."""

    __tablename__ = "device_codes"

    device_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(128), default="cli")
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending|approved|denied|expired
    token_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    token_plain: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )  # one-shot, cleared on read
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)


class AgentRow(Base):
    """A2A AgentCard registry. One row per agent; agents are owned by users."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # e.g. "ada.hermes"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    harness: Mapped[str] = mapped_column(String(32), default="hermes")
    card: Mapped[dict[str, Any]] = mapped_column(JSON)
    push_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class A2ATaskRow(Base):
    """Store-and-forward inbox/outbox. Messages are also events; this table tracks delivery."""

    __tablename__ = "a2a_tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    context_id: Mapped[str] = mapped_column(String(64), index=True)
    from_agent: Mapped[str] = mapped_column(String(128), index=True)
    to_agent: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(24), default="submitted", index=True)
    history: Mapped[list[Any]] = mapped_column(JSON, default=list)
    artifacts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(40), index=True)
    requester_actor_kind: Mapped[str] = mapped_column(String(16))
    requester_actor_id: Mapped[str] = mapped_column(String(128))
    requested_of: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(Text)
    action_type: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    edited_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chat_ts: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class MeetingRow(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(
        String(24), default="queued"
    )  # queued|transcribing|extracting|done|failed
    audio_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    transcript_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    participants: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---- Projections (derived; rebuilt by `relay replay --rebuild-projections`) -----------------


class ActionItemRow(Base):
    __tablename__ = "action_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    assignee: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    due: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    meeting_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_event_id: Mapped[str] = mapped_column(String(40))
    last_event_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime)


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    statement: Mapped[str] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[list[Any]] = mapped_column(JSON, default=list)
    decided_at: Mapped[datetime] = mapped_column(TZDateTime)
    supersedes: Mapped[str | None] = mapped_column(String(40), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_event_id: Mapped[str] = mapped_column(String(40))
