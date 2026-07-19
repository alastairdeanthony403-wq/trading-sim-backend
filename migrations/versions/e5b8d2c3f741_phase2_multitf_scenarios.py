"""phase 2: multi-timeframe intraday scenarios (base_timeframe, available_timeframes)

Revision ID: e5b8d2c3f741
Revises: d4a7c1f9e620
Create Date: 2026-07-19 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5b8d2c3f741'
down_revision = 'd4a7c1f9e620'
branch_labels = None
depends_on = None


def upgrade():
    # Both nullable: existing scenarios stay single-timeframe (columns NULL);
    # intraday scenarios carry a 1m base and the list of viewable timeframes.
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('base_timeframe', sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column('available_timeframes', sa.ARRAY(sa.String()), nullable=True))


def downgrade():
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.drop_column('available_timeframes')
        batch_op.drop_column('base_timeframe')
