"""Add persistent memory operation outbox.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_operation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("contact_id", sa.String(length=120), nullable=False),
        sa.Column("operation_id", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_memory_operation_id"),
    )
    op.create_index("ix_memory_operation_tenant_id", "memory_operation", ["tenant_id"])
    op.create_index("ix_memory_operation_channel", "memory_operation", ["channel"])
    op.create_index("ix_memory_operation_contact_id", "memory_operation", ["contact_id"])
    op.create_index("ix_memory_operation_operation_id", "memory_operation", ["operation_id"])
    op.create_index("ix_memory_operation_status", "memory_operation", ["status"])


def downgrade() -> None:
    op.drop_index("ix_memory_operation_status", table_name="memory_operation")
    op.drop_index("ix_memory_operation_operation_id", table_name="memory_operation")
    op.drop_index("ix_memory_operation_contact_id", table_name="memory_operation")
    op.drop_index("ix_memory_operation_channel", table_name="memory_operation")
    op.drop_index("ix_memory_operation_tenant_id", table_name="memory_operation")
    op.drop_table("memory_operation")
