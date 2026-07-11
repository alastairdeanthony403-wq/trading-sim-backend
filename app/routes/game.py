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

@bp.route("/sessions/<int:session_id>/trades", methods=["POST"])
def open_trade(session_id):
    session = Session.query.get_or_404(session_id)
    body = request.get_json(force=True)

    direction = body["direction"]  # "long" or "short"
    size = float(body["size"])
    entry_bar_sequence = int(body["bar_sequence"])
    stop_loss = body.get("stop_loss")
    take_profit = body.get("take_profit")

    bar = ScenarioBar.query.filter_by(scenario_id=session.scenario_id, bar_sequence=entry_bar_sequence).first_or_404()

    slip = bar.close * SLIPPAGE_PCT
    entry_price = bar.close + slip if direction == "long" else bar.close - slip

    trade = Trade(
        session_id=session.id,
        bar_sequence_entered=entry_bar_sequence,
        direction=direction,
        size=size,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        commission_paid=COMMISSION_PER_TRADE,
        slippage_applied=slip,
    )
    db.session.add(trade)
    db.session.commit()

    return jsonify({"trade_id": trade.id, "entry_price": entry_price})


@bp.route("/trades/<int:trade_id>/close", methods=["POST"])
def close_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    body = request.get_json(force=True)
    exit_bar_sequence = int(body["bar_sequence"])

    bar = ScenarioBar.query.filter_by(scenario_id=trade.session.scenario_id, bar_sequence=exit_bar_sequence).first_or_404()

    slip = bar.close * SLIPPAGE_PCT
    exit_price = bar.close - slip if trade.direction == "long" else bar.close + slip

    raw_pnl = (exit_price - trade.entry_price) * trade.size if trade.direction == "long" \
        else (trade.entry_price - exit_price) * trade.size

    trade.exit_price = exit_price
    trade.bar_sequence_exited = exit_bar_sequence
    trade.pnl = raw_pnl - trade.commission_paid - (slip * trade.size)
    db.session.commit()

    return jsonify({"trade_id": trade.id, "exit_price": exit_price, "pnl": trade.pnl})


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
        "newly_unlocked_lessons": progress.unlocked_lessons,
        "newly_unlocked_tiers": progress.unlocked_scenario_tiers,
    })
