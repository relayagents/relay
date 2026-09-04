"""users.slack_user_id must be unique: it is the identity that gates approvals in Slack

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-03

Existing duplicates (possible before this revision) are resolved by keeping the binding on the
earliest-created user and clearing it on the others; those users re-bind with `relay me`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dupes = (
        bind.execute(
            sa.text(
                "SELECT slack_user_id FROM users WHERE slack_user_id IS NOT NULL "
                "GROUP BY slack_user_id HAVING COUNT(*) > 1"
            )
        )
        .scalars()
        .all()
    )
    for slack_id in dupes:
        keep = bind.execute(
            sa.text(
                "SELECT id FROM users WHERE slack_user_id = :s ORDER BY created_at, id LIMIT 1"
            ),
            {"s": slack_id},
        ).scalar_one()
        bind.execute(
            sa.text(
                "UPDATE users SET slack_user_id = NULL WHERE slack_user_id = :s AND id != :keep"
            ),
            {"s": slack_id, "keep": keep},
        )
        print(
            f"0002: slack_user_id {slack_id} was bound to several users; kept {keep}, cleared the rest"
        )
    op.drop_index("ix_users_slack_user_id", table_name="users")
    op.create_index("ix_users_slack_user_id", "users", ["slack_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_slack_user_id", table_name="users")
    op.create_index("ix_users_slack_user_id", "users", ["slack_user_id"])
