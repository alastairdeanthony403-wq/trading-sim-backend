"""End-of-session summary: what went well, one thing to work on, XP breakdown.

Two deliberate constraints:

  * Everything here reads PROCESS, never P&L. A session can lose money and
    still be all strengths — that is the point of the product.
  * Exactly one focus item. A list of six failings is a scolding, not coaching,
    and nobody acts on six things at once.

This does not build a second coaching engine. It reads the same discipline dict
(app.rules.evaluate_discipline) that scoring, missions and the coach already
read; app/coach.py keeps its richer per-trade narrative for the replay screen.
"""
from app import db
from app.models.engagement import XpEvent
from app.engagement.awards import _honoured_entry_stops
from app.engagement.feedback import feedback_payload
from app.rules import OVERSIZE_RISK_PCT


def _closed(session):
    return [t for t in session.trades if t.status == "closed"]


def session_strengths(session, discipline):
    """Process facts the learner actually demonstrated. Never outcomes."""
    closed = _closed(session)
    if not closed:
        return ["You watched a market without forcing a trade — no setup, no position."]

    out = []
    if discipline["no_stop_count"] == 0:
        out.append("A stop on every trade — your risk was defined before you were in.")
    if discipline["oversize_count"] == 0:
        out.append(f"Every position sized inside the {OVERSIZE_RISK_PCT:g}% risk cap.")
    if discipline["revenge_count"] == 0:
        out.append("No revenge trade after a loss.")
    if _honoured_entry_stops(closed):
        out.append("You honoured the stop you set at entry — no widening it mid-trade.")
    return out


def session_focus(session, discipline):
    """The single most useful thing to work on, or None if the process held."""
    closed = _closed(session)
    n = len(closed)
    if not n:
        return {"text": "To be scored on your process, you need to take a setup you believe in.",
                "lesson_id": "how_markets_work"}

    if discipline["no_stop_count"]:
        k = discipline["no_stop_count"]
        return {"text": f"{k} of your {n} trade{'s' if n != 1 else ''} had no stop. "
                        "Decide what you're willing to lose before you enter, not after.",
                "lesson_id": "risk_basics"}
    if discipline["oversize_count"]:
        return {"text": f"{discipline['oversize_count']} trade(s) risked more than "
                        f"{OVERSIZE_RISK_PCT:g}% of the account. Size is the one variable "
                        "you fully control.",
                "lesson_id": "risk_management"}
    if discipline["revenge_count"]:
        return {"text": "You sized up straight after a stop-out. That's the loss talking, "
                        "not the setup.",
                "lesson_id": "psychology_discipline"}
    if not _honoured_entry_stops(closed):
        return {"text": "A stop moved further from entry after you were in. Widening a stop "
                        "turns a planned loss into an open-ended one.",
                "lesson_id": "risk_management"}
    return None


def xp_breakdown(session):
    """Every XP event this session produced, including its mission award."""
    sid = session.id
    events = (XpEvent.query
              .filter(XpEvent.user_id == session.user_id)
              .filter(db.or_(XpEvent.idempotency_key.like(f"session:{sid}:%"),
                             XpEvent.idempotency_key.like(f"%:session:{sid}")))
              .order_by(XpEvent.awarded_at).all())
    return [{"source_type": e.source_type, "amount": e.amount, "meta": e.meta or {}}
            for e in events]


def session_summary(session, discipline):
    """The engagement block attached to every finished session."""
    breakdown = xp_breakdown(session)
    earned = sum(e["amount"] for e in breakdown)
    return {
        "strengths": session_strengths(session, discipline),
        "focus": session_focus(session, discipline),
        "xp_breakdown": breakdown,
        "xp_session_total": earned,
        "feedback": feedback_payload(session.user_id, earned),
    }
