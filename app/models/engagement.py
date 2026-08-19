"""Engagement layer (Phase 1) — XP ledger, per-learner preferences, daily activity.

Three tables, one job each:

  EngagementProfile  the learner's own settings — timezone (every day boundary
                     in this layer is computed in it), daily goal, and the
                     accessibility/notification switches later phases read.
  XpEvent            an append-only ledger. Every point a learner has ever
                     earned is one row with an idempotency_key, so a replayed
                     request can never award twice and the total is always
                     re-derivable by summing the ledger.
  ActivityDay        one row per learner per LOCAL date, rolled up as events
                     land. Cheap reads for goal/streak surfaces without
                     re-scanning the ledger.

Enum-ish columns are String + a validated tuple, matching how the rest of this
codebase models Session.mode / Session.status / Trade.order_type.
"""
from app import db
from datetime import datetime, timezone


# ── validated value sets (kept here so models and routes agree) ────────────
GOAL_TYPES = ("lessons", "sessions", "minutes", "xp")

SOURCE_TYPES = (
    "lesson_complete",      # a curriculum lesson marked complete
    "topic_check",          # an end-of-unit knowledge check passed
    "scenario_complete",    # a scored trading session (career or paper)
    "plan_adherence",       # the stop declared at entry was honoured
    "risk_discipline",      # process rules satisfied within a mission
    "journal_entry",        # reserved — no journalling feature exists yet
    "review_completed",     # reserved — nothing records a review being read
    "milestone",            # Phase 3
    "legacy_import",        # one-shot migration of pre-server localStorage XP
)

DEFAULT_TIMEZONE = "Europe/London"


class EngagementProfile(db.Model):
    __tablename__ = "engagement_profile"

    # user_id is the PK. There is no users table in this product — identity is
    # the client-held guest id every other table already keys on — so this is
    # deliberately not a foreign key.
    user_id = db.Column(db.String(120), primary_key=True)

    timezone = db.Column(db.String(64), nullable=False,
                         default=DEFAULT_TIMEZONE, server_default=DEFAULT_TIMEZONE)

    daily_goal_type = db.Column(db.String(16), nullable=False,
                                default="lessons", server_default="lessons")
    daily_goal_target = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    weekday_only = db.Column(db.Boolean, nullable=False, default=False, server_default="false")

    # Accessibility / feel. Sound is opt-in and off by default; the motion
    # override is nullable so "unset" means "follow prefers-reduced-motion".
    sound_enabled = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    reduced_motion_override = db.Column(db.Boolean, nullable=True)
    theme = db.Column(db.String(32), nullable=True)

    # Notifications (Phase 6). Opt-in, off by default. Quiet hours are local
    # hours-of-day 0..23 interpreted in `timezone`.
    nudges_opt_in = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    quiet_hours_start = db.Column(db.Integer, nullable=True)
    quiet_hours_end = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))


class XpEvent(db.Model):
    """Append-only. Never updated, never deleted — the total is SUM(amount)."""
    __tablename__ = "xp_event"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), nullable=False, index=True)
    source_type = db.Column(db.String(32), nullable=False)
    # What earned it: a lesson id, session id, mission id... free-form by design,
    # since the sources live in different tables (and some, like lessons, are
    # string ids that exist only in the frontend curriculum).
    source_id = db.Column(db.String(120), nullable=True)
    amount = db.Column(db.Integer, nullable=False)
    # The integrity guarantee: unique, so a replayed award is a no-op insert.
    idempotency_key = db.Column(db.String(200), nullable=False, unique=True)
    awarded_at = db.Column(db.DateTime, nullable=False,
                           default=lambda: datetime.now(timezone.utc))
    meta = db.Column(db.JSON, nullable=True)


class ActivityDay(db.Model):
    """One row per learner per LOCAL date (see app.engagement.day)."""
    __tablename__ = "activity_day"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), nullable=False)
    activity_date = db.Column(db.Date, nullable=False)
    xp_earned = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    lessons_completed = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    scenarios_completed = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    active_seconds = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    goal_met = db.Column(db.Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        db.UniqueConstraint("user_id", "activity_date", name="uq_activity_day_user_date"),
    )
