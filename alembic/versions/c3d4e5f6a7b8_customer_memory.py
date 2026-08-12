"""customer memory

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-07 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customer_memory",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("contact_id", sa.String(length=120), nullable=False),
        sa.Column("memory_type", sa.String(length=24), nullable=False, server_default="note"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="manual"),
        sa.Column("source_key", sa.String(length=120), nullable=True),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "channel", "contact_id", "source_key",
            name="uq_customer_memory_source",
        ),
    )
    op.create_index("ix_customer_memory_tenant_id", "customer_memory", ["tenant_id"])
    op.create_index("ix_customer_memory_channel", "customer_memory", ["channel"])
    op.create_index("ix_customer_memory_contact_id", "customer_memory", ["contact_id"])
    op.create_index("ix_customer_memory_memory_type", "customer_memory", ["memory_type"])


def downgrade() -> None:
    op.drop_index("ix_customer_memory_memory_type", table_name="customer_memory")
    op.drop_index("ix_customer_memory_contact_id", table_name="customer_memory")
    op.drop_index("ix_customer_memory_channel", table_name="customer_memory")
    op.drop_index("ix_customer_memory_tenant_id", table_name="customer_memory")
    op.drop_table("customer_memory")
