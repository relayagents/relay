"""initial schema: events log, users/tokens, agents, a2a tasks, approvals, meetings, projections

Revision ID: 0001
Revises:
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts(nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    embedding_type = Vector(1536) if is_pg else sa.JSON()
    seq_type = sa.BigInteger() if is_pg else sa.Integer()

    op.create_table(
        "events",
        sa.Column("seq", seq_type, primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("text_index", sa.Text(), nullable=False),
        sa.Column("embedding", embedding_type, nullable=True),
    )
    op.create_index("ix_events_id", "events", ["id"], unique=True)
    op.create_index("ix_events_ts", "events", ["ts"])
    op.create_index("ix_events_type", "events", ["type"])
    op.create_index("ix_events_actor_id", "events", ["actor_id"])
    op.create_index("ix_events_thread_id", "events", ["thread_id"])
    op.create_index("ix_events_type_ts", "events", ["type", "ts"])
    if is_pg:
        op.execute(
            "CREATE INDEX ix_events_text_fts ON events USING gin (to_tsvector('english', text_index))"
        )

    op.create_table(
        "users",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("email", sa.String(256), nullable=True),
        sa.Column("slack_user_id", sa.String(32), nullable=True),
        sa.Column("github_login", sa.String(64), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("standup_mode", sa.String(8), nullable=False),
        sa.Column("standup_time", sa.String(5), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_slack_user_id", "users", ["slack_user_id"])

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)

    op.create_table(
        "device_codes",
        sa.Column("device_code", sa.String(64), primary_key=True),
        sa.Column("user_code", sa.String(16), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("token_id", sa.String(40), nullable=True),
        sa.Column("token_plain", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_device_codes_user_code", "device_codes", ["user_code"], unique=True)

    op.create_table(
        "agents",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("harness", sa.String(32), nullable=False),
        sa.Column("card", sa.JSON(), nullable=False),
        sa.Column("push_url", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agents_user_id", "agents", ["user_id"])

    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("context_id", sa.String(64), nullable=False),
        sa.Column("from_agent", sa.String(128), nullable=False),
        sa.Column("to_agent", sa.String(128), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    for col in ("context_id", "from_agent", "to_agent", "state"):
        op.create_index(f"ix_a2a_tasks_{col}", "a2a_tasks", [col])

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("event_id", sa.String(40), nullable=False),
        sa.Column("requester_actor_kind", sa.String(16), nullable=False),
        sa.Column("requester_actor_id", sa.String(128), nullable=False),
        sa.Column("requested_of", sa.String(64), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("edited_action", sa.Text(), nullable=True),
        sa.Column("chat_channel", sa.String(64), nullable=True),
        sa.Column("chat_ts", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    for col in ("event_id", "requested_of", "status"):
        op.create_index(f"ix_approvals_{col}", "approvals", [col])

    op.create_table(
        "meetings",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("audio_path", sa.String(512), nullable=True),
        sa.Column("transcript_path", sa.String(512), nullable=True),
        sa.Column("participants", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )

    # Projections (derived from events; safe to truncate and rebuild)
    op.create_table(
        "action_items",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("assignee", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("due", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("meeting_id", sa.String(40), nullable=True),
        sa.Column("source_event_id", sa.String(40), nullable=False),
        sa.Column("last_event_id", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_action_items_assignee", "action_items", ["assignee"])
    op.create_index("ix_action_items_status", "action_items", ["status"])

    op.create_table(
        "decisions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("topic", sa.String(128), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.JSON(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes", sa.String(40), nullable=True),
        sa.Column("superseded_by", sa.String(40), nullable=True),
        sa.Column("source_event_id", sa.String(40), nullable=False),
    )
    op.create_index("ix_decisions_topic", "decisions", ["topic"])


def downgrade() -> None:
    for table in (
        "decisions",
        "action_items",
        "meetings",
        "approvals",
        "a2a_tasks",
        "agents",
        "device_codes",
        "api_tokens",
        "users",
        "events",
    ):
        op.drop_table(table)
