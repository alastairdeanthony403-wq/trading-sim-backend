"""phase 2 (Rule 0): history_bars on scenarios

Revision ID: c9f2a4e10b83
Revises: b3d5f81a6e07
Create Date: 2026-07-19 10:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9f2a4e10b83'
down_revision = 'b3d5f81a6e07'
branch_labels = None
depends_on = None


def upgrade():
    # Nullable: existing scenarios keep the legacy small window (NULL); newly
    # generated synthetic scenarios set it to the pre-playback history length.
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('history_bars', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.drop_column('history_bars')
