from app import db
from datetime import datetime, timezone


class Mission(db.Model):
    __tablename__ = "missions"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    title = db.Column(db.String(160), nullable=False)
    brief = db.Column(db.Text, nullable=True)          # one-line objective shown to the player
    scenario_id = db.Column(db.Integer, db.ForeignKey("scenarios.id"), nullable=True)
    difficulty_tier = db.Column(db.Integer, nullable=False, default=1)
    xp_reward = db.Column(db.Integer, nullable=False, default=50)
    # Rule set evaluated server-side. JSON list of {type, param, label}.
    #   max_risk_pct_per_trade | max_drawdown_pct | require_stop_on_all |
    #   min_return_pct | no_revenge | max_trades
    rules = db.Column(db.JSON, nullable=False, default=list)
    is_active = db.Column(db.Boolean, default=True, server_default="true")
    is_daily_pool = db.Column(db.Boolean, default=False, server_default="false")


class MissionAttempt(db.Model):
    __tablename__ = "mission_attempts"

    id = db.Column(db.Integer, primary_key=True)
    mission_id = db.Column(db.Integer, db.ForeignKey("missions.id"), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=True)
    user_id = db.Column(db.String(120), nullable=False)
    passed = db.Column(db.Boolean, nullable=False, default=False)
    violations = db.Column(db.JSON, nullable=True)     # list of failed-rule labels
    composite_score = db.Column(db.Float, nullable=True)
    is_daily = db.Column(db.Boolean, default=False, server_default="false")
    challenge_date = db.Column(db.String(10), nullable=True)   # YYYY-MM-DD for daily
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
