"""Performance analytics (Phase 7) — the trade stats a review should show.

Aggregates a finished session's trades into the standard performance metrics:
win rate, expectancy (average R), profit factor, payoff ratio, average win/loss,
best/worst trade, average hold time and max drawdown. Everything is derived from
the replay trades, so it's deterministic and needs no storage.

Framing is deliberately profit-agnostic where it teaches: EXPECTANCY (average R
per trade) and PROFIT FACTOR are skill metrics — you can have a losing session
with a sound process, or a winning one built on luck. The simulator scores skill,
and these numbers make the process legible.
"""


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _max_drawdown_pct(trades, starting_balance):
    """Peak-to-trough drawdown of the closed-trade equity curve, in %."""
    equity = peak = float(starting_balance or 1.0)
    max_dd = 0.0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    return max_dd


def performance(trades, starting_balance):
    """Compute the performance summary from replay trade dicts (each with pnl,
    achieved_r, bar_entered, bar_exited). Returns zero-filled stats for a session
    with no closed trades. profit_factor / payoff_ratio / expectancy_r are None
    when undefined (e.g. no losing trades), so the UI can show '—' honestly."""
    closed = [t for t in trades if t.get("pnl") is not None]
    n = len(closed)
    base = {
        "trades": n, "wins": 0, "losses": 0, "win_rate": 0.0,
        "total_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
        "profit_factor": None, "expectancy_r": None,
        "avg_win_r": None, "avg_loss_r": None, "payoff_ratio": None,
        "avg_win": 0.0, "avg_loss": 0.0, "largest_win": 0.0, "largest_loss": 0.0,
        "avg_hold_bars": 0.0, "max_drawdown_pct": 0.0,
    }
    if n == 0:
        return base

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)        # reported positive
    rs = [t["achieved_r"] for t in closed if t.get("achieved_r") is not None]
    win_rs = [t["achieved_r"] for t in wins if t.get("achieved_r") is not None]
    loss_rs = [t["achieved_r"] for t in losses if t.get("achieved_r") is not None]
    holds = [t["bar_exited"] - t["bar_entered"] for t in closed
             if t.get("bar_exited") is not None and t.get("bar_entered") is not None]
    avg_win = _mean([t["pnl"] for t in wins]) if wins else 0.0
    avg_loss = _mean([t["pnl"] for t in losses]) if losses else 0.0

    base.update({
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100.0, 1),
        "total_pnl": round(sum(t["pnl"] for t in closed), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy_r": round(_mean(rs), 2) if rs else None,
        "avg_win_r": round(_mean(win_rs), 2) if win_rs else None,
        "avg_loss_r": round(_mean(loss_rs), 2) if loss_rs else None,
        "payoff_ratio": round(avg_win / -avg_loss, 2) if (wins and losses and avg_loss < 0) else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "largest_win": round(max((t["pnl"] for t in wins), default=0.0), 2),
        "largest_loss": round(min((t["pnl"] for t in losses), default=0.0), 2),
        "avg_hold_bars": round(_mean(holds), 1) if holds else 0.0,
        "max_drawdown_pct": round(_max_drawdown_pct(closed, starting_balance), 2),
    })
    return base
