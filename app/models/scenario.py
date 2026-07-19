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
    # Rule 0: leading bars shown as pre-playback history on chart load (playback
    # then reveals the rest one at a time). NULL → legacy small window.
    history_bars = db.Column(db.Integer, nullable=True)

    # ── Seed-only synthetic (Phase 2) ─────────────────────────────────────
    # Generated scenarios persist NO bars — they store the seed + params and are
    # regenerated deterministically on read. engine_version pins WHICH engine
    # produced them, so future engine changes never alter existing scenarios'
    # bars (critical for active contests). NULL engine_version → row-based
    # scenario (real-market / legacy), bars live in scenario_bars.
    engine_version = db.Column(db.String(20), nullable=True)
    seed = db.Column(db.BigInteger, nullable=True)
    gen_params = db.Column(db.JSON, nullable=True)   # {kind, n_bars, regime, ...}

    # ── Multi-timeframe (Phase 2) ─────────────────────────────────────────
    # Intraday scenarios store a 1-minute base series and can be viewed on
    # several timeframes (aggregated on read). base_timeframe is the unit the
    # stored/generated bars are in; available_timeframes lists what the chart may
    # switch to. Both NULL → single-timeframe scenario (the timeframe column).
    base_timeframe = db.Column(db.String(8), nullable=True)
    available_timeframes = db.Column(db.ARRAY(db.String), nullable=True)

    bars = db.relationship("ScenarioBar", backref="scenario", lazy=True, cascade="all, delete-orphan")

    @property
    def is_generated(self):
        return self.engine_version is not None


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
