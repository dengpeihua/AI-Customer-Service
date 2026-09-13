"""Remove the retired channel configuration and widen channel identifiers.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
"""

from alembic import op
import sqlalchemy as sa


revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if "wecom_config" in sa.inspect(connection).get_table_names():
        op.drop_table("wecom_config")
    with op.batch_alter_table("conversation") as batch:
        batch.alter_column(
            "channel",
            existing_type=sa.String(length=30),
            type_=sa.String(length=80),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation") as batch:
        batch.alter_column(
            "channel",
            existing_type=sa.String(length=80),
            type_=sa.String(length=30),
            existing_nullable=False,
        )
    # 已退役渠道的密钥配置不可恢复；降级不会重建该表。
