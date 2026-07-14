"""Shared rule/discipline engine (Phase B).

Server-side evaluation of trading discipline from a session's trades. Used by
session scoring (the Discipline sub-score) and, later, by the mission engine —
both ask the same questions: was risk defined, was it sized sanely, was it
revenge trading?
"""

# ── tunable thresholds ────────────────────────────────────────────────────
OVERSIZE_RISK_PCT = 5.0     # a defined-risk trade risking more than this % of
                            # the starting balance is "oversized"
REVENGE_BARS = 5            # a new trade within this many bars of a stop-out ...
REVENGE_SIZE_MULT = 1.5     # ... at more than this multiple of the stopped size
                            # is a revenge trade
PEN_NO_STOP = 15.0          # discipline penalties (points off 100)
PEN_OVERSIZE = 15.0
PEN_REVENGE = 20.0

STOP_OUT_REASONS = ("stop_loss", "trailing_stop", "liquidation")


def trade_risk_amount(trade):
    """(risk_in_$, had_stop). With a stop, risk is the distance to it × size;
    without one, the whole position is at risk."""
    if trade.stop_loss and abs(trade.entry_price - trade.stop_loss) > 0:
        return abs(trade.entry_price - trade.stop_loss) * trade.size, True
    return abs(trade.entry_price) * trade.size, False


def evaluate_discipline(session):
    """Compute discipline metrics + a 0..100 sub-score for a session."""
    start = session.starting_balance or 1.0
    closed = sorted(
        [t for t in session.trades if t.status == "closed"],
        key=lambda t: (t.bar_sequence_entered if t.bar_sequence_entered is not None else 0))

    no_stop = 0
    oversize = 0
    revenge = 0
    risk_pcts = []

    for t in closed:
        risk_amt, had_stop = trade_risk_amount(t)
        risk_pct = risk_amt / start * 100.0
        if not had_stop:
            no_stop += 1
        else:
            risk_pcts.append(risk_pct)
            if risk_pct > OVERSIZE_RISK_PCT:
                oversize += 1

    # revenge: a bigger trade opened just after a stop-out
    for i, prev in enumerate(closed):
        if prev.exit_reason not in STOP_OUT_REASONS or prev.bar_sequence_exited is None:
            continue
        for nxt in closed[i + 1:]:
            entered = nxt.bar_sequence_entered
            if entered is None:
                continue
            if prev.bar_sequence_exited < entered <= prev.bar_sequence_exited + REVENGE_BARS:
                if nxt.size > REVENGE_SIZE_MULT * prev.size:
                    revenge += 1
                    break

    score = max(0.0, min(100.0,
                         100.0 - no_stop * PEN_NO_STOP
                         - oversize * PEN_OVERSIZE
                         - revenge * PEN_REVENGE))
    avg_risk_pct = (sum(risk_pcts) / len(risk_pcts)) if risk_pcts else 0.0

    return {
        "discipline_score": round(score, 1),
        "avg_risk_pct": round(avg_risk_pct, 2),
        "no_stop_count": no_stop,
        "oversize_count": oversize,
        "revenge_count": revenge,
        "rule_violations": no_stop + oversize + revenge,
        "trades_total": len(closed),
        "trades_with_stops": sum(1 for t in closed if trade_risk_amount(t)[1]),
    }


# ── Mission rule evaluation ────────────────────────────────────────────────
# Rules are plain dicts: {"type": ..., "param": ..., "label": ...}. Evaluation
# is pure so it can run live (HUD) on an in-progress session and finally on the
# ended session. Unknown rule types never fail the player.
def check_mission_rules(rules, ctx):
    """ctx = {trades (closed), starting_balance, total_return_pct, max_dd,
    discipline (dict), blown}. Returns (passed_bool, [{label, passed, type}])."""
    trades = ctx["trades"]
    start = ctx["starting_balance"] or 1.0
    disc = ctx["discipline"]
    results = []

    def worst_risk_pct():
        w = 0.0
        for t in trades:
            amt, _ = trade_risk_amount(t)
            w = max(w, amt / start * 100.0)
        return w

    for r in (rules or []):
        typ, p = r.get("type"), r.get("param")
        label = r.get("label") or typ
        if typ == "max_risk_pct_per_trade":
            ok = worst_risk_pct() <= p if trades else True
        elif typ == "max_drawdown_pct":
            ok = ctx["max_dd"] <= p
        elif typ == "require_stop_on_all":
            ok = disc["no_stop_count"] == 0
        elif typ == "min_return_pct":
            ok = ctx["total_return_pct"] >= p
        elif typ == "no_revenge":
            ok = disc["revenge_count"] == 0
        elif typ == "max_trades":
            ok = len(trades) <= p
        elif typ == "min_trades":
            ok = len(trades) >= p
        else:
            ok = True
        results.append({"label": label, "type": typ, "passed": bool(ok)})

    # a blown account fails every mission, regardless of the individual rules
    passed = (not ctx.get("blown")) and all(x["passed"] for x in results)
    return passed, results


def session_context(session, discipline):
    """Build the ctx dict check_mission_rules() needs from a session."""
    start = session.starting_balance or 1.0
    closed = [t for t in session.trades if t.status == "closed" and t.pnl is not None]
    total_pnl = sum(t.pnl for t in closed)
    ending = start + total_pnl
    total_return_pct = (ending - start) / start * 100.0
    eq = start
    peak = start
    max_dd = 0.0
    for t in sorted(closed, key=lambda t: (t.bar_sequence_exited or 0)):
        eq += t.pnl
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100.0)
    return {
        "trades": closed,
        "starting_balance": start,
        "total_return_pct": total_return_pct,
        "max_dd": max_dd,
        "discipline": discipline,
        "blown": session.status == "blown" or ending <= 0,
    }
