"""customer tags —— tag + customer_tag 表

照 WeiClaw 自定义标签体系自研：标签带 color/description/group/ai_muted，客户↔标签多对多。
迁移线性化：down_revision = a1b2c3d4e5f6（F2 provenance），避免与其并列成双 head。

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-27 20:30:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tag',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('color', sa.String(length=20), nullable=False, server_default=''),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('group_name', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('ai_muted', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_tag_tenant_name'),
    )
    op.create_index('ix_tag_tenant_id', 'tag', ['tenant_id'])

    op.create_table(
        'customer_tag',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.Column('channel', sa.String(length=30), nullable=False),
        sa.Column('contact_id', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id']),
        sa.ForeignKeyConstraint(['tag_id'], ['tag.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'tag_id', 'channel', 'contact_id', name='uq_customer_tag_unique'),
    )
    op.create_index('ix_customer_tag_tenant_id', 'customer_tag', ['tenant_id'])
    op.create_index('ix_customer_tag_tag_id', 'customer_tag', ['tag_id'])


def downgrade() -> None:
    op.drop_index('ix_customer_tag_tag_id', table_name='customer_tag')
    op.drop_index('ix_customer_tag_tenant_id', table_name='customer_tag')
    op.drop_table('customer_tag')
    op.drop_index('ix_tag_tenant_id', table_name='tag')
    op.drop_table('tag')
