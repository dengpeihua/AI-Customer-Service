"""Persist which delivery attempt recovered an expired lease.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.add_column(sa.Column(
            "delivery_recovered_attempt_id", sa.String(length=64), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_column("delivery_recovered_attempt_id")
