"""academy: record passed surprise spot checks

Revision ID: a1c4e77b9d52
Revises: f7c2a9e4d310
Create Date: 2026-07-28

Spot checks are surprise market tests that can interrupt the learning path at
any time. A passed one is recorded per triggering lesson so it doesn't reappear.
"""
from alembic import op
import sqlalchemy as sa

revision = "a1c4e77b9d52"
down_revision = "f7c2a9e4d310"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_progress",
                  sa.Column("spot_checks_done", sa.ARRAY(sa.String()), nullable=True))


def downgrade():
    op.drop_column("user_progress", "spot_checks_done")
