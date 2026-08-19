"""The loop: goal → action → immediate feedback → reward → next goal.

Every action that pays XP returns the same `feedback` block, so the frontend
has one shape to render and one confirmation to animate. The rules that shape
the copy in here are as binding as the ones in xp_rules.py:

  * The next goal must be achievable in ONE sitting. When today's goal is met,
    the honest next step is stopping — so that is what this returns. It never
    answers a completed goal with a bigger one.
  * No profitability language, ever. Strengths are process facts ("a stop on
    every trade"), never outcomes ("you made money").
  * Nothing here references what a learner stands to lose. There is no "don't
    break your streak", no expiry, no countdown.

`milestones_unlocked` is always present and always empty until Phase 3 seeds
the catalogue — the contract is fixed now so the client never has to branch on
its absence.
"""
from datetime import timedelta

from app.engagement.day import local_date
from app.engagement.service import (get_or_create_day, get_or_create_profile,
                                    total_xp)
from app.models.engagement import ActivityDay

GOAL_NOUNS = {
    "lessons": ("lesson", "lessons"),
    "sessions": ("scenario", "scenarios"),
    "minutes": ("minute", "minutes"),
    "xp": ("XP", "XP"),
}


def _current_for(goal_type, row):
    if goal_type == "lessons":
        return row.lessons_completed or 0
    if goal_type == "sessions":
        return row.scenarios_completed or 0
    if goal_type == "minutes":
        return int((row.active_seconds or 0) / 60)
    if goal_type == "xp":
        return row.xp_earned or 0
    return 0


def goal_progress(profile, row):
    goal_type = profile.daily_goal_type
    target = profile.daily_goal_target or 0
    current = _current_for(goal_type, row)
    singular, plural = GOAL_NOUNS.get(goal_type, ("item", "items"))
    return {
        "type": goal_type,
        "target": target,
        "current": current,
        "met": bool(row.goal_met),
        "remaining": max(0, target - current),
        "label": f"{current} of {target} {plural if target != 1 else singular} today",
    }


def next_goal(user_id, profile, row):
    """The single next concrete action — or a clean stop.

    Deliberately never chains: a met goal returns done_for_today, not a bigger
    target. Raising the goal is the learner's decision to make in settings, not
    something the product does to them mid-session.
    """
    progress_view = goal_progress(profile, row)
    if progress_view["met"]:
        return {
            "done_for_today": True,
            "label": "That's your goal for today. Good place to stop.",
            "action": None,
            "item_id": None,
            "item_type": None,
        }

    remaining = progress_view["remaining"]
    goal_type = profile.daily_goal_type
    singular, plural = GOAL_NOUNS.get(goal_type, ("item", "items"))
    noun = singular if remaining == 1 else plural

    if goal_type == "lessons":
        from app.routes.progress import compute_next_item, get_or_create_progress
        item = compute_next_item(get_or_create_progress(user_id).completed_lessons)
        if item:
            return {
                "done_for_today": False,
                "label": "Continue the learning path",
                "action": "learn",
                "item_id": item["id"],
                "item_type": item["type"],
            }
        return {"done_for_today": True, "label": "You've finished the learning path.",
                "action": None, "item_id": None, "item_type": None}

    labels = {
        "sessions": f"Play {remaining} more {noun}",
        "minutes": f"{remaining} more {noun} of practice",
        "xp": f"{remaining} more XP today",
    }
    return {
        "done_for_today": False,
        "label": labels.get(goal_type, f"{remaining} more {noun}"),
        "action": "scenario" if goal_type == "sessions" else "any",
        "item_id": None,
        "item_type": None,
    }


def feedback_payload(user_id, xp_awarded=0):
    """The block every XP-paying action returns. One shape, one confirmation."""
    profile = get_or_create_profile(user_id)
    row = get_or_create_day(user_id, local_date(profile.timezone))
    return {
        "xp_awarded": int(xp_awarded or 0),
        "xp_total": total_xp(user_id),
        "goal_progress": goal_progress(profile, row),
        "next_goal": next_goal(user_id, profile, row),
        "milestones_unlocked": [],      # Phase 3 fills this; shape fixed now
    }


# ── consistency (display only) ─────────────────────────────────────────────
def consistency(user_id, profile):
    """Active days in the learner's current local week.

    Phase 4 owns the full streak mechanic (weekly targets, rest days, soft
    reset). This is the honest minimum until then: a count of what happened,
    computed in the learner's own timezone, with nothing framed as losable and
    nothing gated on it.
    """
    today = local_date(profile.timezone)
    week_start = today - timedelta(days=today.weekday())
    rows = (ActivityDay.query
            .filter(ActivityDay.user_id == user_id,
                    ActivityDay.activity_date >= week_start,
                    ActivityDay.activity_date <= today)
            .all())
    return {
        "week_start": week_start.isoformat(),
        "active_days": sum(1 for r in rows if (r.xp_earned or 0) > 0),
        "goals_met": sum(1 for r in rows if r.goal_met),
    }
