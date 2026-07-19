"""Replay analytics + rule-based coach (Phase C).

Everything here is deterministic and server-side. compute_replay() turns a
finished session into per-trade review data (planned vs achieved R, MAE/MFE
from the bars the trade was open), an equity curve, and chart markers.
build_findings() turns that + the discipline metrics into plain-English coaching
notes, each linked to an academy lesson.
"""
import os
import json
import requests
from app import bar_provider
from app.models.scenario import Scenario


def _bars_for(scenario_id):
    scenario = Scenario.query.get(scenario_id)
    if scenario is None:
        return {}
    return {b.bar_sequence: b for b in bar_provider.series(scenario)}


def _excursions(trade, bars):
    """Max favourable / adverse excursion (in price) while the trade was open."""
    lo, hi = None, None
    a = trade.bar_sequence_entered
    b = trade.bar_sequence_exited if trade.bar_sequence_exited is not None else a
    for seq in range(a, b + 1):
        bar = bars.get(seq)
        if bar is None:
            continue
        hi = bar.high if hi is None else max(hi, bar.high)
        lo = bar.low if lo is None else min(lo, bar.low)
    if hi is None:
        return 0.0, 0.0
    entry = trade.entry_price
    if trade.direction == "long":
        mfe = max(0.0, hi - entry)
        mae = max(0.0, entry - lo)
    else:
        mfe = max(0.0, entry - lo)
        mae = max(0.0, hi - entry)
    return mfe, mae


def compute_replay(session):
    bars = _bars_for(session.scenario_id)
    start = session.starting_balance or 1.0
    closed = sorted(
        [t for t in session.trades if t.status == "closed" and t.pnl is not None],
        key=lambda t: (t.bar_sequence_exited or 0))

    trades = []
    markers = []
    eq = start
    equity_curve = [{"bar": None, "equity": round(start, 2)}]

    for t in closed:
        risk_per_unit = (abs(t.entry_price - t.stop_loss)
                         if t.stop_loss and abs(t.entry_price - t.stop_loss) > 0 else None)
        achieved_r = (t.pnl / (risk_per_unit * t.size)) if risk_per_unit else None
        planned_r = (abs(t.take_profit - t.entry_price) / risk_per_unit
                     if (t.take_profit and risk_per_unit) else None)
        mfe, mae = _excursions(t, bars)
        trades.append({
            "trade_id": t.id, "direction": t.direction, "size": t.size,
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "bar_entered": t.bar_sequence_entered, "bar_exited": t.bar_sequence_exited,
            "stop_loss": t.stop_loss, "take_profit": t.take_profit,
            "exit_reason": t.exit_reason, "pnl": round(t.pnl, 2),
            "planned_r": round(planned_r, 2) if planned_r is not None else None,
            "achieved_r": round(achieved_r, 2) if achieved_r is not None else None,
            "mfe": round(mfe, 4), "mae": round(mae, 4),
            "mfe_r": round(mfe / risk_per_unit, 2) if risk_per_unit else None,
            "mae_r": round(mae / risk_per_unit, 2) if risk_per_unit else None,
        })
        markers.append({"bar": t.bar_sequence_entered, "price": t.entry_price,
                        "kind": "entry", "direction": t.direction})
        if t.bar_sequence_exited is not None:
            markers.append({"bar": t.bar_sequence_exited, "price": t.exit_price,
                            "kind": "exit", "reason": t.exit_reason})
        eq += t.pnl
        equity_curve.append({"bar": t.bar_sequence_exited, "equity": round(eq, 2)})

    return {"trades": trades, "markers": markers, "equity_curve": equity_curve}


def build_findings(session, discipline, replay):
    """Plain-English coaching notes, each linked to a lesson. Ordered most to
    least important."""
    trades = replay["trades"]
    n = len(trades)
    findings = []

    def add(sev, text, lesson):
        findings.append({"severity": sev, "text": text, "lesson_id": lesson})

    if n == 0:
        add("info", "You didn't take any trades this session. Reading price without "
            "trading is fine for practice — but to be scored, take a setup you believe in.",
            "how_markets_work")
        return findings

    # 1) no stops = no defined risk
    ns = discipline["no_stop_count"]
    if ns:
        add("warn", f"{ns} of your {n} trade{'s' if n != 1 else ''} had no stop-loss — "
            "you were trading with undefined risk. A stop is how you decide, in advance, "
            "what you're willing to lose.", "risk_basics")

    # 2) cutting winners early
    winners = [t for t in trades if t["pnl"] > 0 and t["achieved_r"] is not None]
    planned = [t["planned_r"] for t in trades if t["planned_r"] is not None]
    if winners and planned:
        avg_win_r = sum(t["achieved_r"] for t in winners) / len(winners)
        avg_planned = sum(planned) / len(planned)
        if avg_planned > 0 and avg_win_r < 0.5 * avg_planned:
            add("warn", f"Your average winner was +{avg_win_r:.1f}R but you planned for "
                f"about +{avg_planned:.1f}R — you're cutting winners early and leaving your "
                "edge on the table.", "risk_management")

    # 3) letting losers run past the stop
    losers = [t for t in trades if t["pnl"] < 0 and t["achieved_r"] is not None]
    if losers:
        avg_loss_r = sum(t["achieved_r"] for t in losers) / len(losers)
        if avg_loss_r < -1.2:
            add("warn", f"Your average loss was {avg_loss_r:.1f}R — bigger than the 1R a "
                "stop should cap it at. Either your stops are too loose or you're moving "
                "them. Honour the stop.", "risk_management")

    # 4) revenge trading
    if discipline["revenge_count"]:
        add("warn", f"{discipline['revenge_count']} revenge trade"
            f"{'s' if discipline['revenge_count'] != 1 else ''} — you sized up right after a "
            "stop-out. That's emotion, not edge. Step away after a loss.", "psychology_discipline")

    # 5) oversizing
    if discipline["oversize_count"]:
        add("warn", f"{discipline['oversize_count']} trade"
            f"{'s' if discipline['oversize_count'] != 1 else ''} risked more than 5% of your "
            "account. Even a great setup shouldn't be able to hurt you badly — size down.",
            "risk_management")

    # 6) overtrading
    if n > 15:
        add("info", f"{n} trades in one session is a lot — costs and marginal setups add up. "
            "Fewer, higher-conviction trades usually beat churn.", "trading_plan")

    # 7) psychology read (Phase E) — name the impulse that showed up and tie it
    # back to the in-session character voices (the hype/aggressive pull).
    from app.characters import IMPULSE_VOICE
    impulses = []
    if discipline["revenge_count"]:
        impulses.append("revenge")
    if discipline["oversize_count"]:
        impulses.append("fomo")
    if winners and planned:
        avg_win_r = sum(t["achieved_r"] for t in winners) / len(winners)
        avg_planned = sum(planned) / len(planned)
        if avg_planned > 0 and avg_win_r < 0.5 * avg_planned:
            impulses.append("fear")
    if impulses:
        labels = ", ".join(IMPULSE_VOICE[i][0] for i in impulses)
        who = IMPULSE_VOICE[impulses[0]][1]
        add("warn", f"Psychology read: signs of {labels}. That's the voice of {who} "
            "winning the moment. The fix isn't willpower — it's a written plan you follow "
            "when the loud voice shows up.", "psychology_discipline")

    # positive reinforcement when the process was sound
    if not findings and discipline["discipline_score"] >= 90:
        add("good", "Clean, disciplined session — defined risk, sensible size, no revenge. "
            "This is exactly the process to repeat.", "trade_journaling")

    return findings


# ── Optional LLM coach (v2, flag-gated; OFF by default) ────────────────────
# Enabled only when COACH_LLM=on AND ANTHROPIC_API_KEY is set. Sends an
# aggregate, non-personal session summary (no user id, no raw account data) to
# Claude for a short narrative review. The rule-based coach above is always the
# default; this is additive.
def llm_coach_enabled():
    return os.environ.get("COACH_LLM") == "on" and bool(os.environ.get("ANTHROPIC_API_KEY"))


def llm_review(session, discipline, replay):
    if not llm_coach_enabled():
        return None
    start = session.starting_balance or 1.0
    ending = session.ending_balance if session.ending_balance is not None else start
    summary = {
        "return_pct": round((ending - start) / start * 100.0, 2),
        "blown": session.status == "blown",
        "discipline_score": discipline["discipline_score"],
        "trades_total": discipline["trades_total"],
        "no_stop_count": discipline["no_stop_count"],
        "oversize_count": discipline["oversize_count"],
        "revenge_count": discipline["revenge_count"],
        "trades": [
            {"dir": t["direction"], "planned_r": t["planned_r"],
             "achieved_r": t["achieved_r"], "reason": t["exit_reason"]}
            for t in replay["trades"][:20]
        ],
    }
    system = (
        "You are a trading-practice coach for a simulator that teaches SKILL, not "
        "profit. Give a short (<=120 words), specific, honest review of this "
        "session's PROCESS and RISK DISCIPLINE. Never promise or imply "
        "profitability, never give financial or investment advice, never say the "
        "user will make money. Focus on stops, position sizing, risk per trade, "
        "cutting winners / letting losers run, and emotional patterns like revenge "
        "trading. Be encouraging but truthful; if the account blew up, be blunt "
        "about why. Address the trader as 'you'."
    )
    model = os.environ.get("COACH_LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model, "max_tokens": 320, "system": system,
                "messages": [{"role": "user",
                              "content": "Session summary (JSON):\n" + json.dumps(summary)}],
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            return None
        parts = resp.json().get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        return text or None
    except Exception:
        return None
