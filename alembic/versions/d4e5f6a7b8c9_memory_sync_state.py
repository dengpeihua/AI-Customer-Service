"""memory sync state

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-09 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory_sync_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("contact_id", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("last_message_key", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("last_message_at", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_status", sa.String(length=24), nullable=False, server_default="idle"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "channel", "contact_id",
            name="uq_memory_sync_state_tenant_channel_contact",
        ),
    )
    op.create_index("ix_memory_sync_state_tenant_id", "memory_sync_state", ["tenant_id"])
    op.create_index("ix_memory_sync_state_channel", "memory_sync_state", ["channel"])
    op.create_index("ix_memory_sync_state_contact_id", "memory_sync_state", ["contact_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_sync_state_contact_id", table_name="memory_sync_state")
    op.drop_index("ix_memory_sync_state_channel", table_name="memory_sync_state")
    op.drop_index("ix_memory_sync_state_tenant_id", table_name="memory_sync_state")
    op.drop_table("memory_sync_state")
