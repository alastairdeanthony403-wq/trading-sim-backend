"""Paper Trading mode (Phase 2) — timed practice on the synthetic intraday engine.

Flow: start → analyse a warm-up block across timeframes → Go Live → bars drip in
over a wall-clock window governed entirely by the SERVER clock. When time is up
(or the learner ends early) it flows into the normal results → replay → coach
pipeline, tagged mode="paper".

The reveal is server-authoritative: the number of live bars visible is derived
from (now - started_at) * bars_per_minute, never from anything the client sends,
so pulling the client cannot reveal future bars and closing/reopening resumes at
the correct elapsed cursor — the market kept running while you were away.
"""
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

from app import db, bar_provider
from app.models.scenario import Scenario
from app.models.session import Session, PaperSession
from app.engine import CURRENT_ENGINE

bp = Blueprint("paper", __name__)

# ── Config (tunable, not hard-coded inline) ────────────────────────────────
DURATION_OPTIONS = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]  # minutes
BARS_PER_MINUTE = 20        # 1-minute bars revealed per real minute → 5 min = 100 bars,
                            # 60 min = 1200 bars: tradeable at every duration, never absurd.
WARMUP_BARS = 150           # 1m warm-up block ≈ 10 candles at the 15m anchor — enough to
                            # read structure before going live. Bumped above the ~60 example
                            # for a readable 15m chart.
ANCHOR_TF = "15m"
SESSION_PROFILE = "equity"
AVAILABLE_TFS = ["1m", "5m", "15m", "30m", "1h", "4h"]


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def paper_reveal_index(session, meta=None):
    """The last revealed bar INDEX for a paper session, from the server clock.
    Warm-up phase (not yet live) → the warm-up window only."""
    meta = meta or PaperSession.query.filter_by(session_id=session.id).first()
    if meta is None:
        return session.bars_served or 0
    if meta.started_at is None:
        return meta.warmup_bars - 1                      # analysis phase
    elapsed = (datetime.now(timezone.utc) - _aware(meta.started_at)).total_seconds()
    live = int(elapsed * meta.bars_per_minute / 60.0)
    total = meta.warmup_bars + meta.live_bars
    return min(total - 1, (meta.warmup_bars - 1) + max(0, live))


def sync_paper_clock(session):
    """Advance a paper session's bars_served to the clock-derived cap (called on
    every bar read/advance). Monotonic: never rewinds a revealed bar."""
    if session.mode != "paper":
        return
    cap = paper_reveal_index(session)
    if session.bars_served is None or cap > session.bars_served:
        session.bars_served = cap
        db.session.commit()


@bp.route("/paper/config", methods=["GET"])
def paper_config():
    return jsonify({"durations": DURATION_OPTIONS, "bars_per_minute": BARS_PER_MINUTE,
                    "warmup_bars": WARMUP_BARS, "anchor_tf": ANCHOR_TF})


@bp.route("/paper/start", methods=["POST"])
def paper_start():
    """Create a paper session in the ANALYSIS phase: the full 1m series is generated
    and the warm-up block is revealed immediately; no live bars move until Go Live."""
    import random
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "anonymous")
    duration = int(body.get("duration_minutes", 15))
    if duration not in DURATION_OPTIONS:
        return jsonify({"error": "invalid duration"}), 400

    live_bars = duration * BARS_PER_MINUTE
    total = WARMUP_BARS + live_bars
    seed = int(body.get("seed", random.randint(1, 10 ** 9)))
    regime = body.get("regime", "range")

    scenario = Scenario(
        name_internal=f"paper_{seed}",
        asset_class="synthetic",
        timeframe=ANCHOR_TF,
        base_timeframe="1m",
        available_timeframes=AVAILABLE_TFS,
        difficulty_tier=1,
        tags=["paper", "intraday", regime],
        is_active=False,
        history_bars=WARMUP_BARS,
        engine_version=CURRENT_ENGINE, seed=seed,
        gen_params={"kind": "intraday", "n_bars": total, "regime": regime,
                    "days": 1, "bars_per_day": total, "vol_scale": 0.15,
                    "session_profile": SESSION_PROFILE, "anchor_tf": ANCHOR_TF},
    )
    db.session.add(scenario)
    db.session.commit()

    session = Session(user_id=user_id, scenario_id=scenario.id,
                      status="in_progress", mode="paper", bars_served=WARMUP_BARS - 1)
    db.session.add(session)
    db.session.commit()

    meta = PaperSession(session_id=session.id, duration_minutes=duration,
                        warmup_bars=WARMUP_BARS, bars_per_minute=BARS_PER_MINUTE,
                        live_bars=live_bars, anchor_tf=ANCHOR_TF, started_at=None)
    db.session.add(meta)
    db.session.commit()

    return jsonify({
        "session_id": session.id, "scenario_id": scenario.id,
        "phase": "analysis",
        "duration_minutes": duration, "warmup_bars": WARMUP_BARS,
        "live_bars": live_bars, "total_bars": total, "bars_per_minute": BARS_PER_MINUTE,
        "history_bars": WARMUP_BARS,
        "base_timeframe": "1m", "available_timeframes": AVAILABLE_TFS,
        "anchor_tf": ANCHOR_TF, "session_profile": SESSION_PROFILE,
        "bars_per_day": total,
        "starting_balance": session.starting_balance,
    })


@bp.route("/paper/<int:session_id>/go-live", methods=["POST"])
def paper_go_live(session_id):
    """Start the wall clock. From here the reveal is time-driven and irreversible."""
    session = Session.query.get_or_404(session_id)
    meta = PaperSession.query.filter_by(session_id=session.id).first_or_404()
    if meta.started_at is None:
        meta.started_at = datetime.now(timezone.utc)
        db.session.commit()
    return jsonify(_clock_view(session, meta))


@bp.route("/paper/<int:session_id>/clock", methods=["GET"])
def paper_clock(session_id):
    """Poll the server clock: how many bars are revealed and how long remains."""
    session = Session.query.get_or_404(session_id)
    meta = PaperSession.query.filter_by(session_id=session.id).first_or_404()
    sync_paper_clock(session)
    return jsonify(_clock_view(session, meta))


def _clock_view(session, meta):
    total = meta.warmup_bars + meta.live_bars
    revealed = paper_reveal_index(session, meta)
    live = meta.started_at is not None
    remaining = None
    if live:
        elapsed = (datetime.now(timezone.utc) - _aware(meta.started_at)).total_seconds()
        remaining = max(0, meta.duration_minutes * 60 - int(elapsed))
    return {
        "phase": "live" if live else "analysis",
        "bars_served": revealed,
        "total_bars": total,
        "live_done": live and revealed >= total - 1,
        "remaining_seconds": remaining,
        "duration_minutes": meta.duration_minutes,
    }


@bp.route("/paper/<int:session_id>/end", methods=["POST"])
def paper_end(session_id):
    """Finish a paper session (timer elapsed or ended early): flatten open orders at
    the last revealed bar, then score/journal via the shared finalizer (mode=paper).
    Flows into the normal results → replay → coach pipeline."""
    session = Session.query.get_or_404(session_id)
    scenario = Scenario.query.get_or_404(session.scenario_id)
    sync_paper_clock(session)

    from app.routes.game import _settle_trade, _cost_model, _finalize_session
    if session.status == "in_progress":
        last = bar_provider.at(scenario, session.bars_served or 0)
        if last:
            slip = _cost_model(session)["slippage_pct"]
            for t in session.trades:
                if t.status == "open":
                    _settle_trade(t, last.bar_sequence, last.close, "manual", slip)
                elif t.status == "pending":
                    db.session.delete(t)
            db.session.commit()

    return jsonify(_finalize_session(session))
