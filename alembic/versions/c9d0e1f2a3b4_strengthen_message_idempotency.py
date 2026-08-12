"""Strengthen channel message idempotency across conversation transitions.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
"""
from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT m.id, m.tenant_id, m.source_message_id, c.channel, c.contact_id "
        "FROM message AS m JOIN conversation AS c ON c.id = m.conversation_id "
        "WHERE m.source_message_id IS NOT NULL ORDER BY m.id"
    )).mappings().all()
    seen: set[tuple[int, str]] = set()
    for row in rows:
        digest = hashlib.sha256(
            f"{row['channel']}\0{row['contact_id']}\0{row['source_message_id']}".encode("utf-8")
        ).hexdigest()
        key = (int(row["tenant_id"]), digest)
        # 旧约束允许同一渠道消息跨 conversation 重复；保留最早一条作为幂等真相。
        value = None if key in seen else digest
        seen.add(key)
        bind.execute(
            sa.text("UPDATE message SET source_message_id = :value WHERE id = :id"),
            {"value": value, "id": int(row["id"])},
        )
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_constraint("uq_message_source_per_conversation", type_="unique")
        batch_op.create_unique_constraint(
            "uq_message_source_per_tenant",
            ["tenant_id", "source_message_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_constraint("uq_message_source_per_tenant", type_="unique")
        batch_op.create_unique_constraint(
            "uq_message_source_per_conversation",
            ["tenant_id", "conversation_id", "source_message_id"],
        )
