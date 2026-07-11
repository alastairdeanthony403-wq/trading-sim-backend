from app import db

class Scenario(db.Model):
    __tablename__ = "scenarios"

    id = db.Column(db.Integer, primary_key=True)
    name_internal = db.Column(db.String(120), nullable=False)
    asset_class = db.Column(db.String(50), nullable=False)
    timeframe = db.Column(db.String(20), nullable=False)
    difficulty_tier = db.Column(db.Integer, nullable=False, default=1)
    tags = db.Column(db.ARRAY(db.String), nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    bars = db.relationship("ScenarioBar", backref="scenario", lazy=True, cascade="all, delete-orphan")


class ScenarioBar(db.Model):
    __tablename__ = "scenario_bars"

    id = db.Column(db.Integer, primary_key=True)
    scenario_id = db.Column(db.Integer, db.ForeignKey("scenarios.id"), nullable=False)
    bar_sequence = db.Column(db.Integer, nullable=False)
    open = db.Column(db.Float, nullable=False)
    high = db.Column(db.Float, nullable=False)
    low = db.Column(db.Float, nullable=False)
    close = db.Column(db.Float, nullable=False)
    volume = db.Column(db.Float, nullable=True)
