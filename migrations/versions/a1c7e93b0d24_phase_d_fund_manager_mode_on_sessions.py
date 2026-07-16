"""phase D: fund_manager mode on sessions

Revision ID: a1c7e93b0d24
Revises: 2ddcb1a56eb7
Create Date: 2026-07-14 21:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c7e93b0d24'
down_revision = '2ddcb1a56eb7'
branch_labels = None
depends_on = None


def upgrade():
    # New NOT NULL column on a populated table needs a server_default so existing
    # rows backfill to the standard (non-client-money) session mode.
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mode', sa.String(length=20),
                                      server_default='standard', nullable=False))


def downgrade():
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('mode')
