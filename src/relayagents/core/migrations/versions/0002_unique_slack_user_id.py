"""users.slack_user_id must be unique: it is the identity that gates approvals in Slack

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_users_slack_user_id", table_name="users")
    op.create_index("ix_users_slack_user_id", "users", ["slack_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_slack_user_id", table_name="users")
    op.create_index("ix_users_slack_user_id", "users", ["slack_user_id"])
