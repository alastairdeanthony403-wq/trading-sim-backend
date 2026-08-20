"""consistency streaks: weekly periods, rest days, soft reset

Revision ID: e4b90d27ac13
Revises: d83f6a1c05be
Create Date: 2026-08-20

Phase 4. One table, `streak_state`, keyed by user_id.

The column set encodes the humane design directly:

  unit                 "week" by default, so the target is "N active days this
                       week" rather than daily attendance.
  freezes_available    rest days, topped up monthly and spent automatically
                       when a period is missed.
  freezes_used         jsonb ledger of the monthly grant and every rest day
                       spent, so a learner can be told AFTER the fact.
  best_count           preserved forever; a soft reset never touches it.
  last_counted_period  the last period folded into current_count, so recompute
                       is idempotent and can run on every activity rollup.

This value is display-only. Nothing in the product gates on it, and
tests/test_streaks.py asserts that.

Inspector-guarded, like the Phase 1 and Phase 3 migrations, because
app/schema_sync.py runs db.create_all() on every boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "e4b90d27ac13"
down_revision = "d83f6a1c05be"
branch_labels = None
depends_on = None


def upgrade():
    if "streak_state" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "streak_state",
            sa.Column("user_id", sa.String(length=120), primary_key=True),
            sa.Column("current_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("best_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unit", sa.String(length=8), nullable=False, server_default="week"),
            sa.Column("last_counted_period", sa.String(length=16), nullable=True),
            sa.Column("freezes_available", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("freezes_used", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )


def downgrade():
    if "streak_state" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("streak_state")
