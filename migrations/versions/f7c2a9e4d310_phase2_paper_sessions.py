"""phase 2: paper-trading sessions (wall-clock timed practice)

Revision ID: f7c2a9e4d310
Revises: e5b8d2c3f741
Create Date: 2026-07-24 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7c2a9e4d310'
down_revision = 'e5b8d2c3f741'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'paper_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('duration_minutes', sa.Integer(), nullable=False),
        sa.Column('warmup_bars', sa.Integer(), nullable=False),
        sa.Column('bars_per_minute', sa.Integer(), nullable=False),
        sa.Column('live_bars', sa.Integer(), nullable=False),
        sa.Column('anchor_tf', sa.String(length=8), nullable=False, server_default='15m'),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id'),
    )


def downgrade():
    op.drop_table('paper_sessions')
