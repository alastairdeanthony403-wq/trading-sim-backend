from app import db
from datetime import datetime, timezone

class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(120), nullable=False)
    scenario_id = db.Column(db.Integer, db.ForeignKey("scenarios.id"), nullable=False)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ended_at = db.Column(db.DateTime, nullable=True)
    starting_balance = db.Column(db.Float, nullable=False, default=10000.0)
    ending_balance = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="in_progress")
    # "standard" | "fund_manager" (client-money rules: 1% risk cap, 8% max DD)
    mode = db.Column(db.String(20), nullable=False, default="standard", server_default="standard")

    # ── Contest anti-cheat (Phase G) ──────────────────────────────────────
    # Contest sessions never receive future bars up front: the server reveals
    # bars one at a time (bars_served is the high-water bar index revealed).
    is_contest = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    bars_served = db.Column(db.Integer, nullable=True)

    trades = db.relationship("Trade", backref="session", lazy=True, cascade="all, delete-orphan")
    score = db.relationship("SessionScore", backref="session", uselist=False, cascade="all, delete-orphan")


class PaperSession(db.Model):
    """Paper-trading run metadata (Phase 2). The learner analyses a warm-up block
    first, then goes live: bars drip in over a wall-clock window governed entirely
    by the server clock (started_at + bars_per_minute), so closing and reopening
    resumes at the correct elapsed cursor — the market kept running."""
    __tablename__ = "paper_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False, unique=True)
    duration_minutes = db.Column(db.Integer, nullable=False)
    warmup_bars = db.Column(db.Integer, nullable=False)
    bars_per_minute = db.Column(db.Integer, nullable=False)
    live_bars = db.Column(db.Integer, nullable=False)
    anchor_tf = db.Column(db.String(8), nullable=False, default="15m")
    started_at = db.Column(db.DateTime, nullable=True)   # NULL until "Go Live"

    session = db.relationship("Session", backref=db.backref("paper", uselist=False,
                                                            cascade="all, delete-orphan"))


class Trade(db.Model):
    __tablename__ = "trades"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)
    bar_sequence_entered = db.Column(db.Integer, nullable=False)
    bar_sequence_exited = db.Column(db.Integer, nullable=True)
    direction = db.Column(db.String(10), nullable=False)  # long/short
    size = db.Column(db.Float, nullable=False)
    entry_price = db.Column(db.Float, nullable=False)
    exit_price = db.Column(db.Float, nullable=True)
    stop_loss = db.Column(db.Float, nullable=True)
    take_profit = db.Column(db.Float, nullable=True)
    pnl = db.Column(db.Float, nullable=True)
    commission_paid = db.Column(db.Float, nullable=True, default=0.0)
    slippage_applied = db.Column(db.Float, nullable=True, default=0.0)

    # ── Order engine (Phase A) ────────────────────────────────────────────
    # status: "pending" (resting entry, not yet filled) → "open" → "closed".
    status = db.Column(db.String(10), nullable=False, default="open", server_default="open")
    # entry order type: market | limit | stop
    order_type = db.Column(db.String(10), nullable=False, default="market", server_default="market")
    # resting entry price for limit/stop entries (NULL for market)
    entry_order_price = db.Column(db.Float, nullable=True)
    # bar the order was created on (for scanning resting/working orders)
    bar_sequence_created = db.Column(db.Integer, nullable=True)
    # trailing stop: trail this distance behind the best price; the anchor is
    # the high-water (long) / low-water (short) of closes since entry.
    trail_distance = db.Column(db.Float, nullable=True)
    trail_anchor = db.Column(db.Float, nullable=True)
    # why the position closed: manual | stop_loss | take_profit |
    # trailing_stop | liquidation
    exit_reason = db.Column(db.String(20), nullable=True)
    # leverage used (1 = cash). Margin required = notional / leverage.
    leverage = db.Column(db.Float, nullable=False, default=1.0, server_default="1")


class SessionScore(db.Model):
    __tablename__ = "session_scores"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False, unique=True)
    total_return_pct = db.Column(db.Float, nullable=True)
    sharpe_ratio = db.Column(db.Float, nullable=True)
    max_drawdown_pct = db.Column(db.Float, nullable=True)
    win_rate = db.Column(db.Float, nullable=True)
    avg_r_multiple = db.Column(db.Float, nullable=True)
    score_composite = db.Column(db.Float, nullable=True)

    # ── Discipline (Phase B) ──────────────────────────────────────────────
    discipline_score = db.Column(db.Float, nullable=True)   # 0..100
    avg_risk_pct = db.Column(db.Float, nullable=True)       # mean risk-per-trade, % of balance
    no_stop_count = db.Column(db.Integer, nullable=True)    # trades opened with no stop
    oversize_count = db.Column(db.Integer, nullable=True)   # trades risking > threshold
    revenge_count = db.Column(db.Integer, nullable=True)    # revenge-trade pattern hits
    rule_violations = db.Column(db.Integer, nullable=True)  # total discipline violations
