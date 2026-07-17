from app import db
from datetime import datetime, timezone


class Contest(db.Model):
    """A weekly async competition: one fixed scenario (same seed for everyone),
    scored by the composite+discipline system, leaderboard resets each week."""
    __tablename__ = "contests"

    id = db.Column(db.Integer, primary_key=True)
    week_start = db.Column(db.Date, nullable=False, unique=True)   # Monday of the week
    scenario_id = db.Column(db.Integer, db.ForeignKey("scenarios.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class ContestEntry(db.Model):
    """One scored attempt per user per contest (enforced by a unique index)."""
    __tablename__ = "contest_entries"
    __table_args__ = (db.UniqueConstraint("contest_id", "user_id", name="uq_contest_user"),)

    id = db.Column(db.Integer, primary_key=True)
    contest_id = db.Column(db.Integer, db.ForeignKey("contests.id"), nullable=False)
    user_id = db.Column(db.String(120), nullable=False)
    display_name = db.Column(db.String(60), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=True)
    composite_score = db.Column(db.Float, nullable=True)
    discipline_score = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
