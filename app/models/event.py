from app import db


class ScenarioEvent(db.Model):
    """A scripted news event attached to a scenario (Phase E). Revealed as
    playback reaches its bar; the scenario's bars already contain the matching
    price/volatility reaction so the move is server-authoritative."""
    __tablename__ = "scenario_events"

    id = db.Column(db.Integer, primary_key=True)
    scenario_id = db.Column(db.Integer, db.ForeignKey("scenarios.id"), nullable=False)
    bar_sequence = db.Column(db.Integer, nullable=False)   # bar the headline breaks on
    category = db.Column(db.String(30), nullable=False)    # rate_decision | earnings | scandal | hype | recession
    headline = db.Column(db.String(200), nullable=False)
    detail = db.Column(db.String(400), nullable=True)
    sentiment = db.Column(db.Integer, nullable=False, default=0)   # -1 bearish | 0 neutral | +1 bullish
    impact = db.Column(db.Float, nullable=True)             # rough size of the reaction (log-return)

    scenario = db.relationship("Scenario", backref=db.backref(
        "events", lazy=True, cascade="all, delete-orphan"))
