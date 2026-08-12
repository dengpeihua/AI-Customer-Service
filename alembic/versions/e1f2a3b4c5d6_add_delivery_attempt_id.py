"""Add idempotent delivery-attempt ownership.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.add_column(sa.Column("delivery_attempt_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_column("delivery_attempt_id")
