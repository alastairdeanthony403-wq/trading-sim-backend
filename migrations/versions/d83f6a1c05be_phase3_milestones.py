"""milestones: catalogue and per-learner unlocks

Revision ID: d83f6a1c05be
Revises: c7e1b40a92f5
Create Date: 2026-08-19

Phase 3. Two tables:

  milestone       the catalogue, upserted by `code` via POST /setup/seed-milestones.
                  threshold_type / threshold_value ARE the criteria shown to the
                  learner while a milestone is still locked, so the text can
                  never drift from what is actually evaluated.
  user_milestone  one row per learner per unlock. UNIQUE (user_id, milestone_id)
                  is the idempotency guarantee: evaluation runs after every XP
                  event, so it must be safe to run constantly and concurrently.
                  seen_at is NULL until the unlock has been shown once.

Both creates are inspector-guarded for the same reason as the Phase 1
migration: app/schema_sync.py runs db.create_all() on every boot, so on a
deployed database these tables may already exist by the time this runs.
"""
from alembic import op
import sqlalchemy as sa

revision = "d83f6a1c05be"
down_revision = "c7e1b40a92f5"
branch_labels = None
depends_on = None


def _existing_tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    existing = _existing_tables()

    if "milestone" not in existing:
        op.create_table(
            "milestone",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=16), nullable=False),
            sa.Column("threshold_type", sa.String(length=40), nullable=False),
            sa.Column("threshold_value", sa.Float(), nullable=False),
            sa.Column("icon_key", sa.String(length=32), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("code", name="uq_milestone_code"),
        )

    if "user_milestone" not in existing:
        op.create_table(
            "user_milestone",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.String(length=120), nullable=False),
            sa.Column("milestone_id", sa.Integer(), nullable=False),
            sa.Column("unlocked_at", sa.DateTime(), nullable=False),
            sa.Column("seen_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["milestone_id"], ["milestone.id"]),
            sa.UniqueConstraint("user_id", "milestone_id", name="uq_user_milestone"),
        )
        op.create_index("ix_user_milestone_user_id", "user_milestone", ["user_id"])


def downgrade():
    existing = _existing_tables()
    if "user_milestone" in existing:
        op.drop_index("ix_user_milestone_user_id", table_name="user_milestone")
        op.drop_table("user_milestone")
    if "milestone" in existing:
        op.drop_table("milestone")
