from app import db

class UserProgress(db.Model):
    __tablename__ = "user_progress"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), nullable=False, unique=True)
    unlocked_lessons = db.Column(db.ARRAY(db.String), default=list)
    completed_lessons = db.Column(db.ARRAY(db.String), default=list)
    unlocked_scenario_tiers = db.Column(db.ARRAY(db.Integer), default=list)
    total_scenarios_completed = db.Column(db.Integer, default=0)
    best_composite_score = db.Column(db.Float, nullable=True)

    # ── Discipline aggregates (Phase B; consumed by Phase D career gates) ──
    total_trades_all = db.Column(db.Integer, default=0, server_default="0")
    trades_with_stops_all = db.Column(db.Integer, default=0, server_default="0")
    blown_count = db.Column(db.Integer, default=0, server_default="0")
    sessions_scored = db.Column(db.Integer, default=0, server_default="0")
    discipline_sum = db.Column(db.Float, default=0.0, server_default="0")  # running total for avg


class Leaderboard(db.Model):
    __tablename__ = "leaderboard"

    id = db.Column(db.Integer, primary_key=True)
    scenario_id = db.Column(db.Integer, db.ForeignKey("scenarios.id"), nullable=False)
    user_id = db.Column(db.String(120), nullable=False)
    composite_score = db.Column(db.Float, nullable=False)
    rank = db.Column(db.Integer, nullable=True)
    achieved_at = db.Column(db.DateTime, nullable=True)
