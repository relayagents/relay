"""HNSW index for the pgvector leg of recall (Postgres only)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-03

Built CONCURRENTLY outside the migration transaction so an upgrade never blocks writes or api
readiness on a node that already holds many embeddings.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_embedding_hnsw "
            "ON events USING hnsw (embedding vector_cosine_ops)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_embedding_hnsw")
