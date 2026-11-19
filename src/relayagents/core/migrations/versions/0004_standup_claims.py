"""standup_claims: the scheduler claims (user, local day) before a standup goes out

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19

Two overlapping cron ticks could both see "not yet sent" and both post. The primary key makes
the claim atomic: only the tick whose insert succeeds delivers. A new, empty table; no data moves.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "standup_claims",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("local_date", sa.Date(), primary_key=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("standup_claims")
