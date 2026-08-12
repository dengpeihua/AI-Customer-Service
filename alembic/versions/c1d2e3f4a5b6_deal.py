"""deal table

Revision ID: c1d2e3f4a5b6
Revises: b7e1c0a9d2f3
Create Date: 2026-07-22 12:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'b7e1c0a9d2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deal',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('contact_id', sa.String(length=120), nullable=False),
        sa.Column('channel', sa.String(length=30), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_followup', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id']),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('conversation_id', name='uq_deal_conversation'),
    )
    op.create_index('ix_deal_tenant_id', 'deal', ['tenant_id'])
    op.create_index('ix_deal_conversation_id', 'deal', ['conversation_id'])


def downgrade() -> None:
    op.drop_index('ix_deal_conversation_id', table_name='deal')
    op.drop_index('ix_deal_tenant_id', table_name='deal')
    op.drop_table('deal')
