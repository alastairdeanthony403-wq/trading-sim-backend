"""engagement layer: profiles, XP ledger, daily activity

Revision ID: c7e1b40a92f5
Revises: a1c4e77b9d52
Create Date: 2026-08-19

Phase 1 of the engagement layer. Three new tables plus one column:

  engagement_profile  per-learner timezone (every day boundary in this layer is
                      computed in it), daily goal, and the accessibility /
                      notification switches later phases read.
  xp_event            append-only XP ledger. idempotency_key is UNIQUE — that
                      constraint IS the no-double-award guarantee, so a replayed
                      request loses the insert race rather than paying twice.
  activity_day        one row per learner per LOCAL date, unique on the pair.
  trades.entry_stop_loss
                      the stop declared when a position was opened, frozen
                      thereafter, so "did they honour the risk they declared?"
                      is answerable without a full audit trail.

Backfill: every user_id already known to user_progress gets a profile row with
defaults, so existing learners don't need a first-write to have settings.
"""
from alembic import op
import sqlalchemy as sa

revision = "c7e1b40a92f5"
down_revision = "a1c4e77b9d52"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "engagement_profile",
        sa.Column("user_id", sa.String(length=120), primary_key=True),
        sa.Column("timezone", sa.String(length=64), nullable=False,
                  server_default="Europe/London"),
        sa.Column("daily_goal_type", sa.String(length=16), nullable=False,
                  server_default="lessons"),
        sa.Column("daily_goal_target", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("weekday_only", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sound_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reduced_motion_override", sa.Boolean(), nullable=True),
        sa.Column("theme", sa.String(length=32), nullable=True),
        sa.Column("nudges_opt_in", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("quiet_hours_start", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_end", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "xp_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(length=120), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=120), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("awarded_at", sa.DateTime(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_xp_event_idempotency_key"),
    )
    op.create_index("ix_xp_event_user_id", "xp_event", ["user_id"])

    op.create_table(
        "activity_day",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(length=120), nullable=False),
        sa.Column("activity_date", sa.Date(), nullable=False),
        sa.Column("xp_earned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lessons_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scenarios_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("goal_met", sa.Boolean(), nullable=False, server_default="false"),
        sa.UniqueConstraint("user_id", "activity_date", name="uq_activity_day_user_date"),
    )

    op.add_column("trades", sa.Column("entry_stop_loss", sa.Float(), nullable=True))

    # Backfill profiles for learners who already exist.
    op.execute("""
        INSERT INTO engagement_profile (user_id, created_at, updated_at)
        SELECT DISTINCT user_id, NOW(), NOW() FROM user_progress
        ON CONFLICT (user_id) DO NOTHING
    """)


def downgrade():
    op.drop_column("trades", "entry_stop_loss")
    op.drop_table("activity_day")
    op.drop_index("ix_xp_event_user_id", table_name="xp_event")
    op.drop_table("xp_event")
    op.drop_table("engagement_profile")
