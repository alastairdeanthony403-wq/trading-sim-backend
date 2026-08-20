"""Milestones — progress markers with no mystery in them.

Two rules govern this module, and both are testable:

  * Every milestone states exactly what unlocks it, WHILE STILL LOCKED. There
    are no teasers, no "???", no hidden or randomised unlocks, and nothing here
    can be bought. METRICS below turns each (threshold_type, threshold_value)
    pair into a plain sentence, so criteria text can never drift from the
    criteria actually evaluated.
  * Progress is only shown where the remaining work is bounded and reachable —
    every metric here is a finite count or a percentage, so "X of Y" is always
    honest.

The catalogue is weighted toward discipline and craft: 18 of the 28 seeded
milestones reward process and screen time, not knowledge quizzes.

Paper sessions count. They stay out of the career ladder, but a milestone is a
one-time marker of something demonstrated — and discipline held in paper is
still discipline held.
"""
from app import db
from app.models.engagement import (ActivityDay, Milestone, UserMilestone,
                                   XpEvent)
from app.models.session import Session, SessionScore


# A ratio means nothing without a sample behind it. Without this floor, a
# learner who has never placed a trade reads as a 100% stop rate (the career
# metric defaults that way so new accounts aren't gated) and would unlock the
# stop-rate milestone for doing nothing at all.
MIN_TRADES_FOR_RATE = 20
MIN_SESSIONS_FOR_RATE = 10


# ── metric registry ────────────────────────────────────────────────────────
# key -> (human label, criteria template, is_percent)
# Templates use {v} for the value and {s} for a plural "s", so criteria read as
# English at 1 as well as at 40.
METRICS = {
    # learning
    "lessons_completed":       ("Lessons completed", "Complete {v} lesson{s}", False),
    "checks_passed":           ("Unit checks passed", "Pass {v} end-of-unit check{s}", False),
    "spot_checks_passed":      ("Spot checks passed", "Pass {v} surprise spot check{s}", False),
    # discipline
    "sessions_all_stops":      ("Sessions with a stop on every trade",
                                "Finish {v} session{s} with a stop on every trade", False),
    "sessions_within_risk":    ("Sessions inside the risk cap",
                                "Finish {v} session{s} without oversizing a trade", False),
    "sessions_no_revenge":     ("Sessions without a revenge trade",
                                "Finish {v} session{s} without a revenge trade", False),
    "plan_adherence_sessions": ("Sessions where the entry stop held",
                                "Finish {v} session{s} without widening a stop", False),
    "avg_discipline":          ("Average discipline score",
                                "Hold an average discipline score of {v} across at "
                                f"least {MIN_SESSIONS_FOR_RATE} sessions", False),
    "pct_trades_with_stops":   ("Share of trades with a stop",
                                "Put a stop on {v} of your trades, over at least "
                                f"{MIN_TRADES_FOR_RATE} trades", True),
    # consistency
    "active_days":             ("Days active", "Be active on {v} separate day{s}", False),
    "goals_met":               ("Daily goals met", "Meet your daily goal {v} time{s}", False),
    # craft
    "sessions_scored":         ("Sessions scored", "Finish {v} scored session{s}", False),
    "missions_passed":         ("Missions passed", "Pass {v} mission{s}", False),
    "career_level":            ("Career level", "Reach career level {v}", False),
    "total_trades":            ("Trades taken", "Take {v} trade{s}", False),
    "total_xp":                ("XP earned", "Earn {v} XP", False),
}


def criteria_text(threshold_type, threshold_value):
    """The plain-English unlock condition. Shown while locked, never hidden."""
    entry = METRICS.get(threshold_type)
    if not entry:
        return f"{threshold_type} ≥ {threshold_value}"
    _, template, is_percent = entry
    value = f"{threshold_value * 100:.0f}%" if is_percent else f"{threshold_value:g}"
    return template.format(v=value, s="" if threshold_value == 1 else "s")


def metric_label(threshold_type):
    entry = METRICS.get(threshold_type)
    return entry[0] if entry else threshold_type


# ── metric computation ─────────────────────────────────────────────────────
def compute_metrics(user_id):
    """Every milestone metric for one learner, in one pass."""
    from app.models.mission import MissionAttempt
    from app.routes.progress import (CURRICULUM, _career_level, _career_metrics,
                                     get_or_create_progress)

    progress = get_or_create_progress(user_id)
    career = _career_metrics(user_id, progress)

    lesson_ids, check_ids = set(), set()
    for unit in CURRICULUM:
        lesson_ids.update(unit["lessons"])
        check_ids.add(unit["check"])
    completed = set(progress.completed_lessons or [])

    scores = (db.session.query(SessionScore)
              .join(Session, SessionScore.session_id == Session.id)
              .filter(Session.user_id == user_id).all())

    xp_rows = db.session.query(XpEvent.source_type, XpEvent.amount).filter(
        XpEvent.user_id == user_id).all()
    days = ActivityDay.query.filter_by(user_id=user_id).all()

    return {
        "lessons_completed": len(lesson_ids & completed),
        "checks_passed": len(check_ids & completed),
        "spot_checks_passed": len(progress.spot_checks_done or []),

        "sessions_all_stops": sum(1 for s in scores if (s.no_stop_count or 0) == 0),
        "sessions_within_risk": sum(1 for s in scores if (s.oversize_count or 0) == 0),
        "sessions_no_revenge": sum(1 for s in scores if (s.revenge_count or 0) == 0),
        "plan_adherence_sessions": sum(1 for t, _ in xp_rows if t == "plan_adherence"),
        # Same reasoning as the stop rate below: one flawless session is not an
        # average, so this stays at zero until there is a sample behind it.
        "avg_discipline": (career["avg_discipline"]
                           if career["sessions_scored"] >= MIN_SESSIONS_FOR_RATE else 0.0),
        # Deliberately NOT career["pct_trades_with_stops"], which is 1.0 for a
        # learner with no trades. A rate with no sample behind it is not an
        # achievement, so it reads as zero until the floor is met.
        "pct_trades_with_stops": (
            (progress.trades_with_stops_all or 0) / (progress.total_trades_all or 1)
            if (progress.total_trades_all or 0) >= MIN_TRADES_FOR_RATE else 0.0),

        "active_days": sum(1 for d in days if (d.xp_earned or 0) > 0),
        "goals_met": sum(1 for d in days if d.goal_met),

        "sessions_scored": career["sessions_scored"],
        "missions_passed": career["missions_passed"],
        "career_level": _career_level(career)["level"],
        "total_trades": progress.total_trades_all or 0,
        "total_xp": sum(a for _, a in xp_rows),
    }


# ── evaluation ─────────────────────────────────────────────────────────────
def evaluate(user_id):
    """Unlock anything newly earned. Idempotent — the unique (user, milestone)
    constraint means a concurrent double-evaluation can't double-unlock."""
    from sqlalchemy.exc import IntegrityError

    catalogue = Milestone.query.all()
    if not catalogue:
        return []                      # nothing seeded yet

    already = {um.milestone_id for um in
               UserMilestone.query.filter_by(user_id=user_id).all()}
    pending = [m for m in catalogue if m.id not in already]
    if not pending:
        return []

    metrics = compute_metrics(user_id)
    unlocked = []
    for m in pending:
        value = metrics.get(m.threshold_type)
        if value is None or value < m.threshold_value:
            continue
        db.session.add(UserMilestone(user_id=user_id, milestone_id=m.id))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()      # lost the race; someone else unlocked it
            continue
        unlocked.append(m)
    return unlocked


def view(milestone, unlocked_row=None, metrics=None):
    """One milestone as the gallery shows it — criteria always present."""
    current = (metrics or {}).get(milestone.threshold_type)
    is_percent = METRICS.get(milestone.threshold_type, (None, None, False))[2]
    return {
        "code": milestone.code,
        "name": milestone.name,
        "description": milestone.description,
        "category": milestone.category,
        "icon_key": milestone.icon_key,
        "criteria": criteria_text(milestone.threshold_type, milestone.threshold_value),
        "metric_label": metric_label(milestone.threshold_type),
        "threshold_type": milestone.threshold_type,
        "target": milestone.threshold_value,
        # Bounded and reachable for every metric in the registry, so progress is
        # always honest to show.
        "current": round(current, 3) if isinstance(current, float) else current,
        "is_percent": is_percent,
        "unlocked": unlocked_row is not None,
        "unlocked_at": unlocked_row.unlocked_at.isoformat() if unlocked_row else None,
    }


def unseen(user_id, claim=True):
    """Milestones unlocked but not yet shown. Claiming marks them shown."""
    from datetime import datetime, timezone
    rows = (UserMilestone.query
            .filter_by(user_id=user_id, seen_at=None)
            .order_by(UserMilestone.unlocked_at).all())
    if not rows:
        return []
    out = [view(r.milestone, r) for r in rows]
    if claim:
        now = datetime.now(timezone.utc)
        for r in rows:
            r.seen_at = now
        db.session.commit()
    return out


# ── the catalogue ──────────────────────────────────────────────────────────
# (code, name, description, category, threshold_type, threshold_value, icon)
# Nothing here implies money made. Names describe what was DONE.
SEED_MILESTONES = [
    # ── learning ──
    ("first_lesson", "Opening the Book", "You started the learning path.",
     "learning", "lessons_completed", 1, "book"),
    ("five_lessons", "Groundwork", "Five lessons down.",
     "learning", "lessons_completed", 5, "book"),
    ("ten_lessons", "Half the Path", "Ten lessons down.",
     "learning", "lessons_completed", 10, "book"),
    ("curriculum_done", "Full Curriculum", "Every lesson in the path, completed.",
     "learning", "lessons_completed", 20, "book"),
    ("three_checks", "Checks Cleared", "Three end-of-unit checks passed.",
     "learning", "checks_passed", 3, "check"),
    ("spot_three", "Caught Off Guard", "Three surprise spot checks passed.",
     "learning", "spot_checks_passed", 3, "check"),

    # ── discipline ──
    ("stops_five", "Risk Defined", "Five sessions where every trade had a stop.",
     "discipline", "sessions_all_stops", 5, "shield"),
    ("stops_fifteen", "Never Undefined", "Fifteen sessions where every trade had a stop.",
     "discipline", "sessions_all_stops", 15, "shield"),
    ("stops_forty", "Risk Is a Habit", "Forty sessions where every trade had a stop.",
     "discipline", "sessions_all_stops", 40, "shield"),
    ("risk_five", "Inside the Cap", "Five sessions without oversizing a trade.",
     "discipline", "sessions_within_risk", 5, "gauge"),
    ("risk_twenty", "Size Under Control", "Twenty sessions without oversizing a trade.",
     "discipline", "sessions_within_risk", 20, "gauge"),
    ("revenge_ten", "Cool Head", "Ten sessions with no revenge trade after a loss.",
     "discipline", "sessions_no_revenge", 10, "calm"),
    ("revenge_thirty", "Unshakeable", "Thirty sessions with no revenge trade after a loss.",
     "discipline", "sessions_no_revenge", 30, "calm"),
    ("plan_five", "The Plan Held", "Five sessions where you never widened a stop.",
     "discipline", "plan_adherence_sessions", 5, "anchor"),
    ("plan_twenty", "The Plan Always Holds",
     "Twenty sessions where you never widened a stop.",
     "discipline", "plan_adherence_sessions", 20, "anchor"),
    ("disc_eighty", "Consistent Process", "An average discipline score of 80.",
     "discipline", "avg_discipline", 80, "gauge"),
    ("disc_ninety", "Process Under Pressure", "An average discipline score of 90.",
     "discipline", "avg_discipline", 90, "gauge"),
    ("stop_rate_ninety", "Stop Rate", "Nine in ten of all your trades carried a stop.",
     "discipline", "pct_trades_with_stops", 0.9, "shield"),

    # ── consistency ──
    ("active_five", "Showing Up", "Active on five separate days.",
     "consistency", "active_days", 5, "calendar"),
    ("active_twenty", "Part of the Routine", "Active on twenty separate days.",
     "consistency", "active_days", 20, "calendar"),
    ("goals_ten", "Goal Met, Ten Times", "You met the goal you set, ten times.",
     "consistency", "goals_met", 10, "target"),
    ("goals_thirty", "Goal Met, Thirty Times", "You met the goal you set, thirty times.",
     "consistency", "goals_met", 30, "target"),

    # ── craft ──
    ("first_session", "First Tape", "Your first scored session.",
     "craft", "sessions_scored", 1, "chart"),
    ("ten_sessions", "Screen Time", "Ten scored sessions.",
     "craft", "sessions_scored", 10, "chart"),
    ("fifty_sessions", "Serious Screen Time", "Fifty scored sessions.",
     "craft", "sessions_scored", 50, "chart"),
    ("missions_five", "Five Missions", "Five missions passed.",
     "craft", "missions_passed", 5, "flag"),
    ("missions_fifteen", "Fifteen Missions", "Fifteen missions passed.",
     "craft", "missions_passed", 15, "flag"),
    ("level_three", "Market Analyst", "Career level three.",
     "craft", "career_level", 3, "badge"),
    ("level_five", "Professional Trader", "Career level five.",
     "craft", "career_level", 5, "badge"),
    ("trades_hundred", "A Hundred Trades", "One hundred trades taken.",
     "craft", "total_trades", 100, "chart"),
]
