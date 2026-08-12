"""bot_config.tone_level

Revision ID: b7e1c0a9d2f3
Revises: 4563f8ba71f0
Create Date: 2026-07-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e1c0a9d2f3'
down_revision: Union[str, Sequence[str], None] = '4563f8ba71f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('bot_config', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('tone_level', sa.String(length=16), nullable=False, server_default='warm')
        )


def downgrade() -> None:
    with op.batch_alter_table('bot_config', schema=None) as batch_op:
        batch_op.drop_column('tone_level')
