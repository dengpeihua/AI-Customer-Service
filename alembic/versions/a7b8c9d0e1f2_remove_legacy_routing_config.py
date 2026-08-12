"""Remove legacy semantic routing configuration.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bot_config") as batch_op:
        batch_op.drop_column("handoff_rules")
        batch_op.drop_column("auto_reply_threshold")


def downgrade() -> None:
    with op.batch_alter_table("bot_config") as batch_op:
        batch_op.add_column(sa.Column("auto_reply_threshold", sa.Float(), nullable=False,
                                      server_default="0.6"))
        batch_op.add_column(sa.Column("handoff_rules", sa.JSON(), nullable=True))
