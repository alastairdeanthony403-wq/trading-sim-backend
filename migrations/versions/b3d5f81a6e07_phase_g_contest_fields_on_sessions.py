"""phase G: contest anti-cheat fields on sessions

Revision ID: b3d5f81a6e07
Revises: a1c7e93b0d24
Create Date: 2026-07-17 16:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3d5f81a6e07'
down_revision = 'a1c7e93b0d24'
branch_labels = None
depends_on = None


def upgrade():
    # is_contest is NOT NULL on a populated table → server_default backfills to
    # false. bars_served is nullable (only set for contest sessions).
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_contest', sa.Boolean(),
                                      server_default='false', nullable=False))
        batch_op.add_column(sa.Column('bars_served', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('bars_served')
        batch_op.drop_column('is_contest')
