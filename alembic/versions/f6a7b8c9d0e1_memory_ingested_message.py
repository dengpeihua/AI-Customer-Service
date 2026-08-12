"""Add per-message ledger for selective Mem0 ingestion.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_ingested_message",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("contact_id", sa.String(length=120), nullable=False),
        sa.Column("message_key", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "channel", "contact_id", "message_key",
            name="uq_memory_ingested_message_scope_key",
        ),
    )
    op.create_index(
        "ix_memory_ingested_message_tenant_id", "memory_ingested_message", ["tenant_id"]
    )
    op.create_index(
        "ix_memory_ingested_message_channel", "memory_ingested_message", ["channel"]
    )
    op.create_index(
        "ix_memory_ingested_message_contact_id", "memory_ingested_message", ["contact_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_ingested_message_contact_id", table_name="memory_ingested_message"
    )
    op.drop_index("ix_memory_ingested_message_channel", table_name="memory_ingested_message")
    op.drop_index("ix_memory_ingested_message_tenant_id", table_name="memory_ingested_message")
    op.drop_table("memory_ingested_message")
