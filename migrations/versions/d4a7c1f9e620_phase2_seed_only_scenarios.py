"""phase 2: seed-only synthetic scenarios (engine_version, seed, gen_params)

Revision ID: d4a7c1f9e620
Revises: c9f2a4e10b83
Create Date: 2026-07-19 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4a7c1f9e620'
down_revision = 'c9f2a4e10b83'
branch_labels = None
depends_on = None


def upgrade():
    # All nullable: existing scenarios stay row-based (engine_version NULL);
    # generated scenarios carry the seed/params to regenerate their bars.
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('engine_version', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('seed', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('gen_params', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.drop_column('gen_params')
        batch_op.drop_column('seed')
        batch_op.drop_column('engine_version')
