"""message.provenance —— 消息 AI 溯源打标

给 message 表加权威来源字段，替代挂件聊天页脆弱的"事后文本猜测"。
值域：customer | ai | handoff | human | broadcast。
不动既有 sender 列（app/admin/metrics.py 硬编码 sender.in_(("ai","agent"))，改值会踩坏销售看板）。

Revision ID: a1b2c3d4e5f6
Revises: c1d2e3f4a5b6
Create Date: 2026-07-27 20:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('message', sa.Column('provenance', sa.String(length=16), nullable=True))
    # 回填历史消息：customer→customer / ai→ai / agent→handoff（agent=转人工草稿）。
    op.execute(
        "UPDATE message SET provenance = CASE sender "
        "WHEN 'ai' THEN 'ai' WHEN 'agent' THEN 'handoff' WHEN 'customer' THEN 'customer' "
        "ELSE NULL END"
    )


def downgrade() -> None:
    op.drop_column('message', 'provenance')
