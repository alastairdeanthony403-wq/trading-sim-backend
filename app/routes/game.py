import math
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from app import db
from app.models.scenario import Scenario, ScenarioBar
from app.models.session import Session, Trade, SessionScore
from app.models.progress import Leaderboard
from app.routes.progress import apply_score_to_progress

bp = Blueprint("game", __name__)

COMMISSION_PER_TRADE = 1.0      # flat fee per trade, in $
SLIPPAGE_PCT = 0.0005           # 0.05% of price, applied on entry and exit


# ======================================================================
# ORDER EXECUTION ENGINE (Phase A)
# ----------------------------------------------------------------------
# Order state lives in the DB and is evaluated server-side as bars advance,
# so results survive refresh and can't be gamed by the client. The client
# tells us how far playback has progressed (POST /sessions/<id>/advance);
# we scan the elapsed bars and fill/close orders deterministically.
# ======================================================================

def _exit_slippage(price):
    return price * SLIPPAGE_PCT


def _settle_trade(trade, exit_bar, fill_price, reason):
    """Close an open trade at fill_price (pre-slippage), applying adverse
    slippage + commission, and record why it closed."""
    slip = _exit_slippage(fill_price)
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


def _scan_open_exit(trade, bars):
    """Scan bars (ascending, each with .bar_sequence/.open/.high/.low/.close,
    only those AFTER entry and up to the playback point) for the first SL/TP/
    trailing hit. Honours gap fills at the open. Returns (bar_seq, fill_price,
    reason) or None; mutates trade.trail_anchor as it ratchets."""
    anchor = trade.trail_anchor if trade.trail_anchor is not None else trade.entry_price
    long = trade.direction == "long"
    for b in bars:
        stop, trailing = _effective_stop(trade, anchor)
        stop_reason = "trailing_stop" if trailing else "stop_loss"
        tp = trade.take_profit
        o, h, l, c = b.open, b.high, b.low, b.close

        if long:
            # gap through a level at the open
            if stop is not None and o <= stop:
                return b.bar_sequence, o, stop_reason
            if tp is not None and o >= tp:
                return b.bar_sequence, o, "take_profit"
            hit_stop = stop is not None and l <= stop
            hit_tp = tp is not None and h >= tp
            if hit_stop:                       # pessimistic: stop wins ties
                trade.trail_anchor = anchor
                return b.bar_sequence, stop, stop_reason
            if hit_tp:
                trade.trail_anchor = anchor
                return b.bar_sequence, tp, "take_profit"
            anchor = max(anchor, c)
        else:
            if stop is not None and o >= stop:
                return b.bar_sequence, o, stop_reason
            if tp is not None and o <= tp:
                return b.bar_sequence, o, "take_profit"
            hit_stop = stop is not None and h >= stop
            hit_tp = tp is not None and l <= tp
            if hit_stop:
                trade.trail_anchor = anchor
                return b.bar_sequence, stop, stop_reason
            if hit_tp:
                trade.trail_anchor = anchor
                return b.bar_sequence, tp, "take_profit"
            anchor = min(anchor, c)
    trade.trail_anchor = anchor               # persist ratchet for next advance
    return None


def _scan_pending_fill(trade, bars):
    """Scan bars for the fill of a resting limit/stop ENTRY order. Honours gap
    fills at the open (better for limits, worse for stops). Returns
    (bar_seq, fill_price) or None."""
    p = trade.entry_order_price
    if p is None:
        return None
    long = trade.direction == "long"
    for b in bars:
        o, h, l = b.open, b.high, b.low
        if trade.order_type == "limit":
            # buy limit fills when price trades down to p (or gaps below)
            if long and (o <= p or l <= p):
                return b.bar_sequence, min(o, p)
            if not long and (o >= p or h >= p):
                return b.bar_sequence, max(o, p)
        elif trade.order_type == "stop":
            # buy stop fills when price trades up to p (or gaps above)
            if long and (o >= p or h >= p):
                return b.bar_sequence, max(o, p)
            if not long and (o <= p or l <= p):
                return b.bar_sequence, min(o, p)
    return None


# ---------- Scenario listing ----------

@bp.route("/scenarios", methods=["GET"])
def list_scenarios():
    scenarios = Scenario.query.filter_by(is_active=True).all()
    return jsonify([
        {
            "id": s.id,
            "asset_class": s.asset_class,
            "timeframe": s.timeframe,
            "difficulty_tier": s.difficulty_tier,
            "tags": s.tags,
            "bar_count": len(s.bars),
        }
        for s in scenarios
    ])


# ---------- Session lifecycle ----------

@bp.route("/scenarios/<int:scenario_id>/start", methods=["POST"])
def start_session(scenario_id):
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "anonymous")
    starting_balance = body.get("starting_balance", 10000.0)

    scenario = Scenario.query.get_or_404(scenario_id)

    session = Session(
        user_id=user_id,
        scenario_id=scenario.id,
        starting_balance=starting_balance,
        status="in_progress",
    )
    db.session.add(session)
    db.session.commit()

    return jsonify({"session_id": session.id, "scenario_id": scenario.id, "starting_balance": starting_balance})


@bp.route("/sessions/<int:session_id>/bars", methods=["GET"])
def get_bars(session_id):
    session = Session.query.get_or_404(session_id)
    up_to = request.args.get("up_to", type=int)

    query = ScenarioBar.query.filter_by(scenario_id=session.scenario_id).order_by(ScenarioBar.bar_sequence)
    if up_to is not None:
        query = query.filter(ScenarioBar.bar_sequence <= up_to)

    bars = query.all()
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

    bar = ScenarioBar.query.filter_by(scenario_id=session.scenario_id, bar_sequence=entry_bar_sequence).first_or_404()

    if order_type == "market":
        slip = bar.close * SLIPPAGE_PCT
        entry_price = bar.close + slip if direction == "long" else bar.close - slip
        trade = Trade(
            session_id=session.id,
            bar_sequence_entered=entry_bar_sequence,
            bar_sequence_created=entry_bar_sequence,
            direction=direction, size=size, entry_price=entry_price,
            stop_loss=stop_loss, take_profit=take_profit,
            order_type="market", status="open",
            trail_distance=trail_distance,
            trail_anchor=entry_price if trail_distance is not None else None,
            commission_paid=COMMISSION_PER_TRADE, slippage_applied=slip,
        )
        db.session.add(trade)
        db.session.commit()
        return jsonify({"trade_id": trade.id, "status": "open", "entry_price": entry_price})

    # Resting entry order (limit/stop) — not filled until price touches it.
    if entry_order_price is None:
        return jsonify({"error": "entry_order_price required for limit/stop orders"}), 400
    trade = Trade(
        session_id=session.id,
        bar_sequence_entered=entry_bar_sequence,   # provisional; set on fill
        bar_sequence_created=entry_bar_sequence,
        direction=direction, size=size,
        entry_price=float(entry_order_price),      # provisional
        entry_order_price=float(entry_order_price),
        stop_loss=stop_loss, take_profit=take_profit,
        order_type=order_type, status="pending",
        trail_distance=trail_distance,
        commission_paid=COMMISSION_PER_TRADE, slippage_applied=0.0,
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

    bar = ScenarioBar.query.filter_by(scenario_id=trade.session.scenario_id, bar_sequence=exit_bar_sequence).first_or_404()
    _settle_trade(trade, exit_bar_sequence, bar.close, "manual")
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
    up_to = int((request.get_json(force=True) or {}).get("bar_sequence"))

    bars = (ScenarioBar.query
            .filter_by(scenario_id=session.scenario_id)
            .filter(ScenarioBar.bar_sequence <= up_to)
            .order_by(ScenarioBar.bar_sequence).all())
    by_seq = {b.bar_sequence: b for b in bars}
    events = []

    for trade in session.trades:
        # 1) fill resting entry orders
        if trade.status == "pending":
            scan = [b for b in bars if b.bar_sequence >= (trade.bar_sequence_created or 0)]
            fill = _scan_pending_fill(trade, scan)
            if fill is None:
                continue
            fill_bar, fill_price = fill
            slip = fill_price * SLIPPAGE_PCT
            trade.entry_price = fill_price + slip if trade.direction == "long" else fill_price - slip
            trade.slippage_applied = slip
            trade.bar_sequence_entered = fill_bar
            trade.status = "open"
            if trade.trail_distance is not None:
                trade.trail_anchor = trade.entry_price
            events.append({"trade_id": trade.id, "event": "filled",
                           "bar_sequence": fill_bar, "entry_price": trade.entry_price})

        # 2) evaluate SL/TP/trailing on open positions
        if trade.status == "open":
            scan = [b for b in bars if b.bar_sequence > trade.bar_sequence_entered]
            exit_hit = _scan_open_exit(trade, scan)
            if exit_hit is not None:
                exit_bar, fill_price, reason = exit_hit
                _settle_trade(trade, exit_bar, fill_price, reason)
                events.append({"trade_id": trade.id, "event": "closed",
                               "bar_sequence": exit_bar, "reason": reason,
                               "exit_price": trade.exit_price, "pnl": trade.pnl})

    db.session.commit()
    return jsonify({"events": events, "positions": [_trade_dict(t) for t in session.trades]})


# ---------- Scoring ----------

@bp.route("/sessions/<int:session_id>/end", methods=["POST"])
def end_session(session_id):
    session = Session.query.get_or_404(session_id)
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

    # Composite score: weighted toward risk-adjusted performance, not raw return
    composite = (sharpe * 40) + (total_return_pct * 0.5) - (max_dd * 0.5) + (win_rate * 0.2)

    score = SessionScore(
        session_id=session.id,
        total_return_pct=total_return_pct,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd,
        win_rate=win_rate,
        avg_r_multiple=avg_r_multiple,
        score_composite=composite,
    )
    db.session.add(score)

    session.ending_balance = ending_balance
    session.status = "complete"
    session.ended_at = datetime.now(timezone.utc)
    db.session.commit()

    progress = apply_score_to_progress(session.user_id, composite)

    db.session.add(Leaderboard(
        scenario_id=session.scenario_id,
        user_id=session.user_id,
        composite_score=composite,
        achieved_at=datetime.now(timezone.utc),
    ))
    db.session.commit()

    return jsonify({
        "session_id": session.id,
        "ending_balance": ending_balance,
        "total_return_pct": total_return_pct,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_dd,
        "win_rate": win_rate,
        "avg_r_multiple": avg_r_multiple,
        "score_composite": composite,
        "newly_unlocked_tiers": progress.unlocked_scenario_tiers,
    })
