"""Store delivery status in an atomically updateable column.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.add_column(sa.Column("delivery_status", sa.String(length=16), nullable=True))
    op.execute(sa.text(
        "UPDATE message SET delivery_status = json_extract(meta, '$.delivery_status') "
        "WHERE direction = 'out' AND meta IS NOT NULL"
    ))


def downgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_column("delivery_status")
