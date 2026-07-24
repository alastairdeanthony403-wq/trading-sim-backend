import math
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from app import db, bar_provider
from app.models.scenario import Scenario, ScenarioBar
from app.models.session import Session, Trade, SessionScore
from app.models.progress import Leaderboard
from app.routes.progress import apply_score_to_progress
from app.rules import evaluate_discipline
from app.coach import compute_replay, build_findings, llm_coach_enabled, llm_review

bp = Blueprint("game", __name__)

COMMISSION_PER_TRADE = 1.0      # flat fee per trade, in $
SLIPPAGE_PCT = 0.0005           # 0.05% of price, applied on entry and exit

# Per-asset-class cost models (Phase D). Crypto has the widest spread/slippage,
# indices the tightest. Falls back to the defaults above for unknown classes.
ASSET_COST_MODELS = {
    "crypto":      {"slippage_pct": 0.0010, "commission": 1.0},
    "forex":       {"slippage_pct": 0.0002, "commission": 0.5},
    "indices":     {"slippage_pct": 0.0001, "commission": 0.5},
    "commodities": {"slippage_pct": 0.0004, "commission": 1.0},
    "stocks":      {"slippage_pct": 0.0005, "commission": 1.0},
    "equity":      {"slippage_pct": 0.0005, "commission": 1.0},
}

# Fund Manager (client-money) rules
FM_MAX_RISK_PCT = 1.0           # no single trade may risk more than 1% of the fund
FM_MAX_DRAWDOWN_PCT = 8.0       # lose more than 8% of the fund → the client fires you
CONCENTRATION_LIMIT = 0.60      # >60% of total open risk in one position = concentrated


def _cost_model(session):
    sc = Scenario.query.get(session.scenario_id)
    ac = sc.asset_class if sc else "stocks"
    return ASSET_COST_MODELS.get(ac, {"slippage_pct": SLIPPAGE_PCT, "commission": COMMISSION_PER_TRADE})


def _reveal_capped(session):
    """Sessions whose bars are released incrementally under a server-enforced cap
    (bars_served): contests, and academy practice checks (Phase 1). The client can
    never see, request or derive a bar beyond the cap."""
    return session.is_contest or session.mode in ("practice", "paper")


def _session_meta(scenario):
    """Intraday trading-session context for the client (Phase 4). The session
    schedule is public knowledge (you always know the time of day), so exposing
    it live is not a leak. Empty for non-intraday scenarios."""
    p = scenario.gen_params or {}
    if p.get("kind") != "intraday":
        return {}
    from app.synthetic import session_bands
    profile = p.get("session_profile", "equity")
    return {"session_profile": profile, "bars_per_day": p.get("bars_per_day", 390),
            "session_bands": session_bands(profile)}


def _personality_label(scenario):
    """Short asset-personality descriptor for the scenario list (Phase 6)."""
    from app.synthetic import ASSET_PROFILES
    asset = (scenario.gen_params or {}).get("asset")
    return ASSET_PROFILES[asset]["label"] if asset in ASSET_PROFILES else None


def _fm_risk_check(session, direction, entry_price, stop_loss, size):
    """Fund Manager mode gate applied at order time: client money must have a
    defined stop and may risk no more than FM_MAX_RISK_PCT of the fund. Returns
    an error string to reject with, or None when the trade is allowed."""
    if getattr(session, "mode", "standard") != "fund_manager":
        return None
    if stop_loss is None:
        return "Fund Manager mode: every trade must have a stop-loss (client money)."
    risk_amt = abs(float(entry_price) - float(stop_loss)) * size
    max_risk = FM_MAX_RISK_PCT / 100.0 * session.starting_balance
    if risk_amt > max_risk + 1e-6:
        return (f"Fund Manager mode: this trade risks "
                f"{risk_amt / session.starting_balance * 100:.2f}% of the fund; "
                f"the per-trade limit is {FM_MAX_RISK_PCT:.0f}%.")
    return None


# ======================================================================
# ORDER EXECUTION ENGINE (Phase A)
# ----------------------------------------------------------------------
# Order state lives in the DB and is evaluated server-side as bars advance,
# so results survive refresh and can't be gamed by the client. The client
# tells us how far playback has progressed (POST /sessions/<id>/advance);
# we scan the elapsed bars and fill/close orders deterministically.
# ======================================================================

def _settle_trade(trade, exit_bar, fill_price, reason, slip_pct=SLIPPAGE_PCT):
    """Close an open trade at fill_price (pre-slippage), applying adverse
    slippage + commission, and record why it closed."""
    slip = fill_price * slip_pct
    exit_price = fill_price - slip if trade.direction == "long" else fill_price + slip
    raw_pnl = (exit_price - trade.entry_price) * trade.size if trade.direction == "long" \
        else (trade.entry_price - exit_price) * trade.size
    trade.exit_price = exit_price
    trade.bar_sequence_exited = exit_bar
    trade.pnl = raw_pnl - (trade.commission_paid or 0.0) - (slip * trade.size)
    trade.status = "closed"
    trade.exit_reason = reason
    return trade


def _effective_stop(trade, anchor):
    """Combine the fixed stop-loss with the trailing stop (if any); returns
    (stop_level_or_None, is_trailing_binding)."""
    fixed = trade.stop_loss
    if trade.trail_distance is None:
        return fixed, False
    if trade.direction == "long":
        trail = anchor - trade.trail_distance
        if fixed is None or trail >= fixed:
            return trail, True
        return fixed, False
    else:
        trail = anchor + trade.trail_distance
        if fixed is None or trail <= fixed:
            return trail, True
        return fixed, False


def _exit_on_bar(trade, b):
    """Evaluate SL/TP/trailing for ONE bar. Returns (fill_price, reason) or
    None; ratchets trade.trail_anchor when the position survives the bar.
    Gap fills at the open; pessimistic stop-first on same-bar ties."""
    anchor = trade.trail_anchor if trade.trail_anchor is not None else trade.entry_price
    long = trade.direction == "long"
    stop, trailing = _effective_stop(trade, anchor)
    stop_reason = "trailing_stop" if trailing else "stop_loss"
    tp = trade.take_profit
    o, h, l, c = b.open, b.high, b.low, b.close
    if long:
        if stop is not None and o <= stop:
            return o, stop_reason
        if tp is not None and o >= tp:
            return o, "take_profit"
        if stop is not None and l <= stop:
            return stop, stop_reason
        if tp is not None and h >= tp:
            return tp, "take_profit"
        trade.trail_anchor = max(anchor, c)
    else:
        if stop is not None and o >= stop:
            return o, stop_reason
        if tp is not None and o <= tp:
            return o, "take_profit"
        if stop is not None and h >= stop:
            return stop, stop_reason
        if tp is not None and l <= tp:
            return tp, "take_profit"
        trade.trail_anchor = min(anchor, c)
    return None


def _fill_on_bar(trade, b):
    """Evaluate a resting limit/stop ENTRY for ONE bar (gap-aware). Returns the
    fill price or None."""
    p = trade.entry_order_price
    if p is None:
        return None
    long = trade.direction == "long"
    o, h, l = b.open, b.high, b.low
    if trade.order_type == "limit":
        if long and (o <= p or l <= p):
            return min(o, p)
        if not long and (o >= p or h >= p):
            return max(o, p)
    elif trade.order_type == "stop":
        if long and (o >= p or h >= p):
            return max(o, p)
        if not long and (o <= p or l <= p):
            return min(o, p)
    return None


# ---------- Margin / liquidation ----------
MAINTENANCE_FRACTION = 0.5    # maintenance margin = 50% of initial margin
MARGIN_CALL_MULT = 1.5        # warn when equity < 1.5x maintenance


def _notional(trade):
    return abs(trade.entry_price) * trade.size


def _used_margin(open_trades):
    return sum(_notional(t) / (t.leverage or 1.0) for t in open_trades)


def _maintenance_margin(open_trades):
    return _used_margin(open_trades) * MAINTENANCE_FRACTION


def _unrealised(trade, price):
    diff = price - trade.entry_price
    return diff * trade.size if trade.direction == "long" else -diff * trade.size


def _open_risk(trade):
    """$ at risk on an open position: distance to stop × size, or the full
    notional when there is no stop (the whole position can be lost)."""
    if trade.stop_loss is not None and abs(trade.entry_price - trade.stop_loss) > 0:
        return abs(trade.entry_price - trade.stop_loss) * trade.size
    return _notional(trade)


def _is_concentrated(open_trades):
    """True when a single open position carries more than CONCENTRATION_LIMIT of
    the portfolio's total open risk — a diversification warning for the client."""
    if len(open_trades) < 2:
        return False
    risks = [_open_risk(t) for t in open_trades]
    total = sum(risks)
    return total > 0 and max(risks) / total > CONCENTRATION_LIMIT


# ---------- Scenario listing ----------

@bp.route("/scenarios", methods=["GET"])
def list_scenarios():
    scenarios = Scenario.query.filter_by(is_active=True).all()
    # Optional career market-gating: when a user_id is supplied, annotate each
    # scenario with whether that user's career level has unlocked its market.
    user_id = request.args.get("user_id")
    unlocked_markets = None
    gated_markets = None
    if user_id:
        from app.routes.progress import (get_or_create_progress, _tool_level,
                                          _unlocked_markets, MARKET_UNLOCKS)
        progress = get_or_create_progress(user_id)
        _tools, level = _tool_level(progress)
        unlocked_markets = set(_unlocked_markets(level))
        gated_markets = set(MARKET_UNLOCKS)

    def is_unlocked(asset_class):
        # Only the known career-gated markets can be locked; synthetic and any
        # ungated class is always available as practice ground.
        if unlocked_markets is None or asset_class not in gated_markets:
            return True
        return asset_class in unlocked_markets

    return jsonify([
        {
            "id": s.id,
            "asset_class": s.asset_class,
            "timeframe": s.timeframe,
            "difficulty_tier": s.difficulty_tier,
            "tags": s.tags,
            "bar_count": bar_provider.count(s),
            "market_unlocked": is_unlocked(s.asset_class),
            "available_timeframes": bar_provider.available_timeframes(s),
            "base_timeframe": bar_provider.base_timeframe(s),
            "personality": _personality_label(s),
        }
        for s in scenarios
    ])


# ---------- Session lifecycle ----------

@bp.route("/scenarios/<int:scenario_id>/start", methods=["POST"])
def start_session(scenario_id):
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "anonymous")
    starting_balance = body.get("starting_balance", 10000.0)
    mode = "fund_manager" if body.get("mode") == "fund_manager" else "standard"

    scenario = Scenario.query.get_or_404(scenario_id)

    session = Session(
        user_id=user_id,
        scenario_id=scenario.id,
        starting_balance=starting_balance,
        status="in_progress",
        mode=mode,
    )
    db.session.add(session)
    db.session.commit()

    return jsonify({"session_id": session.id, "scenario_id": scenario.id,
                    "starting_balance": starting_balance, "mode": mode,
                    "history_bars": initial_window(scenario),
                    # Multi-timeframe view metadata (single-TF scenarios: one entry).
                    "base_timeframe": bar_provider.base_timeframe(scenario),
                    "available_timeframes": bar_provider.available_timeframes(scenario),
                    "anchor_tf": (scenario.gen_params or {}).get("anchor_tf")
                    if scenario.gen_params else None,
                    # Correlated benchmark overlay available? (Phase 6)
                    "has_reference": bar_provider.has_reference(scenario),
                    # Intraday session context (Phase 4): the profile + its bands +
                    # bars_per_day let the client show the live trading session.
                    **_session_meta(scenario)})


def initial_window(scenario):
    """Rule 0: how many leading bars to show as pre-playback history on load.
    Uses the scenario's history_bars (clamped to leave ≥1 bar for playback);
    falls back to a small legacy window for scenarios without it (real-market,
    pre-Rule-0 synthetic)."""
    total = bar_provider.count(scenario)
    want = scenario.history_bars if scenario.history_bars else 30
    return max(1, min(want, total - 1)) if total > 1 else total


@bp.route("/sessions/<int:session_id>/bars", methods=["GET"])
def get_bars(session_id):
    session = Session.query.get_or_404(session_id)
    up_to = request.args.get("up_to", type=int)

    # Paper mode: the reveal cap is the SERVER wall clock. Recompute it before
    # serving so the visible window matches elapsed real time (and resumes right
    # after a reopen).
    if session.mode == "paper":
        from app.routes.paper import sync_paper_clock
        sync_paper_clock(session)

    # Anti-cheat: never serve bars beyond the server-tracked high-water
    # (bars_served), no matter what up_to the client asks for. This is what
    # stops a contestant (or a practice learner) from grabbing the whole future
    # and computing perfect trades — future bars simply do not exist to them yet.
    if _reveal_capped(session):
        cap = session.bars_served if session.bars_served is not None else 0
        up_to = cap if up_to is None else min(up_to, cap)

    scenario = Scenario.query.get_or_404(session.scenario_id)

    # Multi-timeframe: ?tf=15m returns candles aggregated from the 1m base up to
    # the same reveal point (up_to is in BASE units, so the contest cap above and
    # the no-future-leak guarantee both still hold on every timeframe).
    tf = request.args.get("tf")
    if tf and tf in bar_provider.available_timeframes(scenario):
        return jsonify(bar_provider.series_tf(scenario, tf, up_to))

    bars = bar_provider.upto(scenario, up_to)
    return jsonify([
        {
            "bar_sequence": b.bar_sequence,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ])


@bp.route("/sessions/<int:session_id>/reference", methods=["GET"])
def get_reference(session_id):
    """Correlated benchmark line for the session's scenario (Phase 6), [] if none.
    Same reveal cap as bars (up_to in base units) so it stays server-authoritative
    for contests."""
    session = Session.query.get_or_404(session_id)
    up_to = request.args.get("up_to", type=int)
    if session.mode == "paper":
        from app.routes.paper import sync_paper_clock
        sync_paper_clock(session)
    if _reveal_capped(session):
        cap = session.bars_served if session.bars_served is not None else 0
        up_to = cap if up_to is None else min(up_to, cap)
    scenario = Scenario.query.get_or_404(session.scenario_id)
    return jsonify(bar_provider.reference(scenario, up_to))


@bp.route("/sessions/<int:session_id>/events", methods=["GET"])
def get_events(session_id):
    """Scripted news events for the session's scenario, ordered by bar. The
    client reveals each one as playback reaches its bar (the price reaction is
    already baked into the bars, so it stays server-authoritative)."""
    from app.models.event import ScenarioEvent
    from app.characters import voices_for_event
    session = Session.query.get_or_404(session_id)
    events = (ScenarioEvent.query
              .filter_by(scenario_id=session.scenario_id)
              .order_by(ScenarioEvent.bar_sequence).all())
    return jsonify([
        {
            "bar_sequence": e.bar_sequence,
            "category": e.category,
            "headline": e.headline,
            "detail": e.detail,
            "sentiment": e.sentiment,
            # conflicting character takes surfaced at the moment the headline breaks
            "voices": voices_for_event(e.category, e.sentiment),
        }
        for e in events
    ])


@bp.route("/sessions/<int:session_id>/scam-debrief", methods=["GET"])
def scam_debrief(session_id):
    """Post-scenario debrief for scam (pump-and-dump) scenarios. Scores whether
    the player took the bait — held a long across the rug — and returns the
    recognise-a-scam checklist. Teaches recognition, not technique."""
    from app.models.event import ScenarioEvent
    from app.synthetic import SCAM_ANATOMY
    session = Session.query.get_or_404(session_id)
    scenario = Scenario.query.get(session.scenario_id)
    tags = (scenario.tags or []) if scenario else []
    if "scam" not in tags:
        return jsonify({"is_scam": False})

    rug = (ScenarioEvent.query
           .filter_by(scenario_id=session.scenario_id, category="rug")
           .order_by(ScenarioEvent.bar_sequence).first())
    rug_bar = rug.bar_sequence if rug else None

    took_bait = False
    exited_before_rug = False
    if rug_bar is not None:
        for t in session.trades:
            if t.direction != "long" or t.status == "pending":
                continue
            entered = t.bar_sequence_entered
            exited = t.bar_sequence_exited if t.bar_sequence_exited is not None else 10 ** 9
            if entered <= rug_bar <= exited:
                took_bait = True          # long, open across the rug → caught
            elif entered < rug_bar and exited < rug_bar:
                exited_before_rug = True   # got in on the hype but out before the drop

    longs = [t for t in session.trades if t.direction == "long" and t.status != "pending"]
    if not longs:
        verdict = "stayed_out"
    elif took_bait:
        verdict = "took_bait"
    else:
        verdict = "got_out"

    return jsonify({
        "is_scam": True,
        "rug_bar": rug_bar,
        "took_bait": took_bait,
        "exited_before_rug": exited_before_rug,
        "verdict": verdict,
        "anatomy": SCAM_ANATOMY,
    })


# ---------- Trading ----------

def _trade_dict(t):
    return {
        "trade_id": t.id,
        "direction": t.direction,
        "size": t.size,
        "status": t.status,
        "order_type": t.order_type,
        "entry_order_price": t.entry_order_price,
        "entry_price": t.entry_price if t.status != "pending" else None,
        "exit_price": t.exit_price,
        "stop_loss": t.stop_loss,
        "take_profit": t.take_profit,
        "trail_distance": t.trail_distance,
        "leverage": t.leverage,
        "bar_sequence_entered": t.bar_sequence_entered,
        "bar_sequence_exited": t.bar_sequence_exited,
        "pnl": t.pnl,
        "exit_reason": t.exit_reason,
    }


@bp.route("/sessions/<int:session_id>/trades", methods=["POST"])
def open_trade(session_id):
    session = Session.query.get_or_404(session_id)
    body = request.get_json(force=True)

    direction = body["direction"]  # "long" or "short"
    size = float(body["size"])
    entry_bar_sequence = int(body["bar_sequence"])
    stop_loss = body.get("stop_loss")
    take_profit = body.get("take_profit")
    order_type = body.get("order_type", "market")
    entry_order_price = body.get("entry_order_price")
    trail_distance = body.get("trail_distance")
    leverage = max(1.0, min(float(body.get("leverage", 1.0) or 1.0), 125.0))

    scenario = Scenario.query.get_or_404(session.scenario_id)
    bar = bar_provider.at(scenario, entry_bar_sequence)
    if bar is None:
        return jsonify({"error": "bar not found"}), 404
    cm = _cost_model(session)

    if order_type == "market":
        slip = bar.close * cm["slippage_pct"]
        entry_price = bar.close + slip if direction == "long" else bar.close - slip
        # Fund Manager client-money gate (stop required, ≤1% risk).
        fm_err = _fm_risk_check(session, direction, entry_price, stop_loss, size)
        if fm_err:
            return jsonify({"error": fm_err}), 400
        # Margin check: the new position's initial margin must fit within free
        # equity. Leverage is what lets you take size beyond your cash.
        opens_now = [t for t in session.trades if t.status == "open"]
        realised_bal = session.starting_balance + sum(
            (t.pnl or 0.0) for t in session.trades if t.status == "closed")
        available = (realised_bal
                     + sum(_unrealised(t, bar.close) for t in opens_now)
                     - _used_margin(opens_now))
        if (entry_price * size) / leverage > available + 1e-6:
            return jsonify({"error": "Insufficient margin for this position size/leverage."}), 400
        trade = Trade(
            session_id=session.id,
            bar_sequence_entered=entry_bar_sequence,
            bar_sequence_created=entry_bar_sequence,
            direction=direction, size=size, entry_price=entry_price,
            stop_loss=stop_loss, take_profit=take_profit,
            order_type="market", status="open", leverage=leverage,
            trail_distance=trail_distance,
            trail_anchor=entry_price if trail_distance is not None else None,
            commission_paid=cm["commission"], slippage_applied=slip,
        )
        db.session.add(trade)
        db.session.commit()
        return jsonify({"trade_id": trade.id, "status": "open", "entry_price": entry_price})

    # Resting entry order (limit/stop) — not filled until price touches it.
    if entry_order_price is None:
        return jsonify({"error": "entry_order_price required for limit/stop orders"}), 400
    fm_err = _fm_risk_check(session, direction, float(entry_order_price), stop_loss, size)
    if fm_err:
        return jsonify({"error": fm_err}), 400
    trade = Trade(
        session_id=session.id,
        bar_sequence_entered=entry_bar_sequence,   # provisional; set on fill
        bar_sequence_created=entry_bar_sequence,
        direction=direction, size=size,
        entry_price=float(entry_order_price),      # provisional
        entry_order_price=float(entry_order_price),
        stop_loss=stop_loss, take_profit=take_profit,
        order_type=order_type, status="pending", leverage=leverage,
        trail_distance=trail_distance,
        commission_paid=cm["commission"], slippage_applied=0.0,
    )
    db.session.add(trade)
    db.session.commit()
    return jsonify({"trade_id": trade.id, "status": "pending"})


@bp.route("/trades/<int:trade_id>/close", methods=["POST"])
def close_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    body = request.get_json(force=True)
    exit_bar_sequence = int(body["bar_sequence"])

    if trade.status == "pending":
        # Cancel a resting order that hasn't filled.
        db.session.delete(trade)
        db.session.commit()
        return jsonify({"trade_id": trade_id, "status": "cancelled"})
    if trade.status == "closed":
        return jsonify({"trade_id": trade.id, "exit_price": trade.exit_price, "pnl": trade.pnl})

    scenario = Scenario.query.get_or_404(trade.session.scenario_id)
    bar = bar_provider.at(scenario, exit_bar_sequence)
    if bar is None:
        return jsonify({"error": "bar not found"}), 404
    _settle_trade(trade, exit_bar_sequence, bar.close, "manual", _cost_model(trade.session)["slippage_pct"])
    db.session.commit()
    return jsonify({"trade_id": trade.id, "exit_price": trade.exit_price, "pnl": trade.pnl})


@bp.route("/trades/<int:trade_id>", methods=["PATCH"])
def modify_trade(trade_id):
    """Adjust SL/TP/trailing on a working or open order (drag-to-adjust)."""
    trade = Trade.query.get_or_404(trade_id)
    if trade.status == "closed":
        return jsonify({"error": "trade already closed"}), 400
    body = request.get_json(force=True) or {}
    for field in ("stop_loss", "take_profit", "trail_distance", "entry_order_price"):
        if field in body:
            setattr(trade, field, body[field])
    db.session.commit()
    return jsonify(_trade_dict(trade))


@bp.route("/sessions/<int:session_id>/positions", methods=["GET"])
def list_positions(session_id):
    session = Session.query.get_or_404(session_id)
    return jsonify([_trade_dict(t) for t in session.trades])


@bp.route("/sessions/<int:session_id>/advance", methods=["POST"])
def advance(session_id):
    """Process resting/working orders up to the client's current playback bar.
    Server-authoritative and idempotent: re-calling with the same (or a lower)
    bar re-derives the same fills, since closed/filled state is persisted."""
    session = Session.query.get_or_404(session_id)
    scenario = Scenario.query.get_or_404(session.scenario_id)
    up_to = int((request.get_json(force=True) or {}).get("bar_sequence"))
    slip_pct = _cost_model(session)["slippage_pct"]

    # Paper mode: the SERVER wall clock decides how many bars exist. Sync it and
    # process orders up to that cap (no per-call increment).
    if session.mode == "paper":
        from app.routes.paper import sync_paper_clock
        sync_paper_clock(session)
        up_to = session.bars_served if session.bars_served is not None else 0

    # Reveal-capped sessions (contest / practice): the SERVER is the clock. Each
    # advance reveals exactly one new bar (ignoring whatever bar the client asked
    # for), so a player can never race ahead to see the future.
    elif _reveal_capped(session):
        total = bar_provider.count(scenario)
        served = session.bars_served if session.bars_served is not None else 0
        served = min(total - 1, served + 1)
        session.bars_served = served
        up_to = served

    bars = bar_provider.upto(scenario, up_to)
    events = []

    def realised():
        return session.starting_balance + sum(
            (t.pnl or 0.0) for t in session.trades if t.status == "closed")

    # Bar-by-bar so margin is enforced account-wide as price moves.
    for b in bars:
        if session.status in ("blown", "complete"):
            break

        # 1) fill resting entries that are eligible on this bar
        for t in session.trades:
            if t.status == "pending" and (t.bar_sequence_created or 0) <= b.bar_sequence:
                fp = _fill_on_bar(t, b)
                if fp is None:
                    continue
                slip = fp * slip_pct
                t.entry_price = fp + slip if t.direction == "long" else fp - slip
                t.slippage_applied = slip
                t.bar_sequence_entered = b.bar_sequence
                t.status = "open"
                if t.trail_distance is not None:
                    t.trail_anchor = t.entry_price
                events.append({"trade_id": t.id, "event": "filled",
                               "bar_sequence": b.bar_sequence, "entry_price": t.entry_price})

        # 2) SL/TP/trailing exits (never on the entry bar itself)
        for t in session.trades:
            if t.status == "open" and b.bar_sequence > t.bar_sequence_entered:
                hit = _exit_on_bar(t, b)
                if hit is not None:
                    price, reason = hit
                    _settle_trade(t, b.bar_sequence, price, reason, slip_pct)
                    events.append({"trade_id": t.id, "event": "closed",
                                   "bar_sequence": b.bar_sequence, "reason": reason,
                                   "exit_price": t.exit_price, "pnl": t.pnl})

        # 3) mark-to-market + margin / liquidation (account-wide)
        opens = [t for t in session.trades if t.status == "open"]
        if opens:
            equity = realised() + sum(_unrealised(t, b.close) for t in opens)
            if equity <= _maintenance_margin(opens):
                for t in opens:
                    _settle_trade(t, b.bar_sequence, b.close, "liquidation", slip_pct)
                    events.append({"trade_id": t.id, "event": "liquidated",
                                   "bar_sequence": b.bar_sequence,
                                   "exit_price": t.exit_price, "pnl": t.pnl})
                if realised() <= 0:
                    session.status = "blown"
                    session.ending_balance = realised()
                    events.append({"event": "blown", "bar_sequence": b.bar_sequence})

        # 3b) Fund Manager mandate: an 8% drawdown on client money ends it —
        # flatten everything and fail the session (the client fires you).
        if session.mode == "fund_manager" and session.status not in ("blown", "complete"):
            opens = [t for t in session.trades if t.status == "open"]
            equity_fm = realised() + sum(_unrealised(t, b.close) for t in opens)
            floor = session.starting_balance * (1 - FM_MAX_DRAWDOWN_PCT / 100.0)
            if equity_fm <= floor:
                for t in opens:
                    _settle_trade(t, b.bar_sequence, b.close, "fund_drawdown", slip_pct)
                    events.append({"trade_id": t.id, "event": "closed",
                                   "bar_sequence": b.bar_sequence, "reason": "fund_drawdown",
                                   "exit_price": t.exit_price, "pnl": t.pnl})
                session.status = "blown"
                session.ending_balance = realised()
                events.append({"event": "fund_fired", "bar_sequence": b.bar_sequence,
                               "drawdown_pct": FM_MAX_DRAWDOWN_PCT})

    # margin-call warning at the latest mark (only while still exposed)
    opens = [t for t in session.trades if t.status == "open"]
    margin_call = False
    if opens and bars:
        equity_now = realised() + sum(_unrealised(t, bars[-1].close) for t in opens)
        maint = _maintenance_margin(opens)
        margin_call = maint < equity_now <= maint * MARGIN_CALL_MULT

    # Character voices at a decision point: a losing stop-out (revenge tempation)
    # or holding with no stop. Flavour that dramatises the emotional pull.
    from app.characters import voices_for_context
    voices = []
    lost = any(e.get("event") in ("closed", "liquidated") and (e.get("pnl") or 0) < 0
               for e in events)
    if lost:
        voices = voices_for_context("after_stopout")
    elif any(t.stop_loss is None for t in opens):
        voices = voices_for_context("no_stop")

    db.session.commit()
    return jsonify({
        "events": events,
        "positions": [_trade_dict(t) for t in session.trades],
        "blown": session.status == "blown",
        "margin_call": margin_call,
        "concentrated": _is_concentrated(opens),
        "voices": voices,
        "bars_served": session.bars_served,
        "status": session.status,
    })


# ---------- Replay & coaching ----------

@bp.route("/sessions/<int:session_id>/replay", methods=["GET"])
def session_replay(session_id):
    """Post-session review: per-trade R/MAE/MFE, chart markers, equity curve,
    and rule-based coach findings. Works on any finished session."""
    session = Session.query.get_or_404(session_id)
    replay = compute_replay(session)
    disc = evaluate_discipline(session)
    findings = build_findings(session, disc, replay)
    return jsonify({
        "session_id": session.id,
        "scenario_id": session.scenario_id,
        "starting_balance": session.starting_balance,
        "ending_balance": session.ending_balance,
        "status": session.status,
        "discipline": disc,
        "coach": findings,
        "llm_coach_enabled": llm_coach_enabled(),
        **replay,
    })


@bp.route("/sessions/<int:session_id>/coach-llm", methods=["GET"])
def session_coach_llm(session_id):
    """Optional narrative LLM review — only does anything when COACH_LLM=on and
    ANTHROPIC_API_KEY is set. Otherwise returns enabled=false and the client
    just shows the rule-based coach."""
    session = Session.query.get_or_404(session_id)
    if not llm_coach_enabled():
        return jsonify({"enabled": False, "review": None})
    disc = evaluate_discipline(session)
    replay = compute_replay(session)
    review = llm_review(session, disc, replay)
    return jsonify({"enabled": True, "review": review})


# ---------- Scoring ----------

@bp.route("/sessions/<int:session_id>/end", methods=["POST"])
def end_session(session_id):
    session = Session.query.get_or_404(session_id)
    return jsonify(_finalize_session(session))


def _finalize_session(session):
    """Score + persist a finished session (idempotent: re-calling returns the
    stored result instead of double-scoring). Shared by /end and contest submit."""
    if session.score is not None:
        sc = session.score
        disc = evaluate_discipline(session)
        return {
            "session_id": session.id,
            "ending_balance": session.ending_balance,
            "total_return_pct": sc.total_return_pct,
            "sharpe_ratio": sc.sharpe_ratio,
            "max_drawdown_pct": sc.max_drawdown_pct,
            "win_rate": sc.win_rate,
            "avg_r_multiple": sc.avg_r_multiple,
            "score_composite": sc.score_composite,
            "discipline": disc,
            "blown": session.status == "blown",
            "post_mortem": _post_mortem(session),
        }

    trades = [t for t in session.trades if t.pnl is not None]

    total_pnl = sum(t.pnl for t in trades)
    ending_balance = session.starting_balance + total_pnl
    total_return_pct = (ending_balance - session.starting_balance) / session.starting_balance * 100

    returns = [t.pnl / session.starting_balance for t in trades]
    win_rate = (sum(1 for t in trades if t.pnl > 0) / len(trades) * 100) if trades else 0.0

    # Sharpe-like ratio using trade returns (simplified, no annualization)
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(variance)
        sharpe = (mean_r / std_r) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown from cumulative equity curve of closed trades
    equity = session.starting_balance
    peak = equity
    max_dd = 0.0
    for t in trades:
        equity += t.pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    avg_r_multiple = 0.0
    r_multiples = []
    for t in trades:
        if t.stop_loss and t.entry_price != t.stop_loss:
            risk_per_unit = abs(t.entry_price - t.stop_loss)
            r_multiples.append(t.pnl / (risk_per_unit * t.size)) if risk_per_unit > 0 else None
    if r_multiples:
        avg_r_multiple = sum(r_multiples) / len(r_multiples)

    # Discipline sub-score (0..100) — risk control & process, not profit.
    disc = evaluate_discipline(session)
    discipline_score = disc["discipline_score"]

    # Composite: risk-adjusted performance PLUS discipline. Discipline is
    # centred at 50 and weighted 0.4, so a perfectly disciplined session adds
    # +20 and a reckless one subtracts up to -20 — a reckless profit scores
    # worse than a disciplined smaller gain (the core design principle).
    #   composite = 40*Sharpe + 0.5*return% - 0.5*maxDD + 0.2*winRate
    #             + 0.4*(discipline - 50)
    composite = ((sharpe * 40) + (total_return_pct * 0.5) - (max_dd * 0.5)
                 + (win_rate * 0.2) + (discipline_score - 50) * 0.4)

    # Bankruptcy overrides everything: a blown account scores 0 no matter what
    # paper profit or discipline came before it. This is the flagship lesson.
    blown = session.status == "blown" or ending_balance <= 0
    if blown:
        composite = 0.0

    score = SessionScore(
        session_id=session.id,
        total_return_pct=total_return_pct,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd,
        win_rate=win_rate,
        avg_r_multiple=avg_r_multiple,
        score_composite=composite,
        discipline_score=discipline_score,
        avg_risk_pct=disc["avg_risk_pct"],
        no_stop_count=disc["no_stop_count"],
        oversize_count=disc["oversize_count"],
        revenge_count=disc["revenge_count"],
        rule_violations=disc["rule_violations"],
    )
    db.session.add(score)

    session.ending_balance = ending_balance
    session.status = "blown" if blown else "complete"
    session.ended_at = datetime.now(timezone.utc)
    db.session.commit()

    # Paper trading is deliberately LOW-STAKES: it is not career-gated, so it does
    # not touch career progression or the discipline aggregates (Phase 2). The
    # score is still stored and the coach/replay still read discipline live off the
    # session, so findings surface without paper inflating the career ladder.
    if session.mode != "paper":
        progress = apply_score_to_progress(session.user_id, composite)
        # Discipline aggregates for the career system (Phase D).
        progress.total_trades_all = (progress.total_trades_all or 0) + disc["trades_total"]
        progress.trades_with_stops_all = (progress.trades_with_stops_all or 0) + disc["trades_with_stops"]
        progress.blown_count = (progress.blown_count or 0) + (1 if blown else 0)
        progress.sessions_scored = (progress.sessions_scored or 0) + 1
        progress.discipline_sum = (progress.discipline_sum or 0.0) + discipline_score
        db.session.add(progress)

    # Practice checks and paper runs use throwaway per-attempt scenarios, so they
    # don't post to a per-scenario leaderboard.
    if session.mode not in ("practice", "paper"):
        db.session.add(Leaderboard(
            scenario_id=session.scenario_id,
            user_id=session.user_id,
            composite_score=composite,
            achieved_at=datetime.now(timezone.utc),
        ))
    db.session.commit()

    return {
        "session_id": session.id,
        "ending_balance": ending_balance,
        "total_return_pct": total_return_pct,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_dd,
        "win_rate": win_rate,
        "avg_r_multiple": avg_r_multiple,
        "score_composite": composite,
        "discipline": disc,
        "blown": blown,
        "post_mortem": _post_mortem(session),
        # Paper runs aren't career-gated, so there are no tier unlocks to report.
        "newly_unlocked_tiers": progress.unlocked_scenario_tiers if session.mode != "paper" else [],
    }


def _post_mortem(session):
    """Educational breakdown for the results / ACCOUNT BLOWN screen: the equity
    curve, per-trade risk history, the trades that did the damage, and a
    'what if every trade risked 1%' counterfactual."""
    start = session.starting_balance
    closed = sorted(
        [t for t in session.trades if t.status == "closed" and t.pnl is not None],
        key=lambda t: (t.bar_sequence_exited or 0))

    def trade_risk(t):
        if t.stop_loss and abs(t.entry_price - t.stop_loss) > 0:
            return abs(t.entry_price - t.stop_loss) * t.size, True
        return _notional(t), False   # no stop → the whole position is at risk

    eq = start
    equity_curve = [{"bar": None, "equity": round(start, 2)}]
    risk_history = []
    disciplined = start
    budget = 0.01 * start            # 1% of starting balance per trade
    for t in closed:
        eq += t.pnl
        equity_curve.append({"bar": t.bar_sequence_exited, "equity": round(eq, 2)})
        risk_amt, had_stop = trade_risk(t)
        risk_history.append({
            "bar": t.bar_sequence_exited,
            "risk_pct": round(risk_amt / start * 100, 2) if start else 0,
            "had_stop": had_stop,
            "pnl": round(t.pnl, 2),
        })
        if risk_amt > 0:
            disciplined += (t.pnl / risk_amt) * budget

    worst = [t for t in sorted(closed, key=lambda t: t.pnl) if t.pnl < 0][:3]
    worst_trades = [{
        "bar_entered": t.bar_sequence_entered,
        "bar_exited": t.bar_sequence_exited,
        "direction": t.direction, "size": t.size, "leverage": t.leverage,
        "pnl": round(t.pnl, 2), "exit_reason": t.exit_reason,
    } for t in worst]

    return {
        "equity_curve": equity_curve,
        "risk_history": risk_history,
        "worst_trades": worst_trades,
        "disciplined_ending_balance": round(disciplined, 2),
        "disciplined_note": "If every trade had risked just 1% of your starting balance",
    }
