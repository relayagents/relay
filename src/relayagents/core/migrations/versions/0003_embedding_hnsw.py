"""HNSW index for the pgvector leg of recall (Postgres only)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_embedding_hnsw ON events USING hnsw (embedding vector_cosine_ops)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_events_embedding_hnsw")
