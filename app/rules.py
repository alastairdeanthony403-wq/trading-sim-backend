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
