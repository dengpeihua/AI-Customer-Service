"""Add channel message id for idempotent inbound handling.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.add_column(sa.Column("source_message_id", sa.String(length=200), nullable=True))
        batch_op.create_unique_constraint(
            "uq_message_source_per_conversation",
            ["tenant_id", "conversation_id", "source_message_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_constraint("uq_message_source_per_conversation", type_="unique")
        batch_op.drop_column("source_message_id")
