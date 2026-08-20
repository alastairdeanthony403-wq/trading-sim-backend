"""Consistency streaks, built to be leaveable.

The streak mechanic is the easiest thing in a product like this to turn into a
trap, so the shape here is chosen deliberately against that:

  * WEEKLY, not daily. The target is "be active N days this week", which is a
    real consistency signal that never demands daily attendance. A learner can
    take four days off and lose nothing.
  * REST DAYS are automatic. Two freezes a month, applied silently when a
    period is missed, and reported afterwards as a fact — never dangled
    beforehand as something to protect.
  * SOFT RESET. Out of freezes, the count drops to the highest tier below it
    (12 → 8), never to zero, and best_count is kept forever.
  * DISPLAY ONLY. Nothing may gate on this value. Not content, not XP, not
    milestones, not modes. tests/test_streaks.py proves it.

Copy rule: never reference what would be lost. "Consistent for 6 weeks" — never
"don't break your streak", never a countdown to a deadline.

All period boundaries are computed in the learner's timezone (app.engagement.day).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app import db
from app.models.engagement import ActivityDay, StreakState
from app.engagement.day import local_date


# ── tunables ───────────────────────────────────────────────────────────────
WEEKLY_ACTIVE_DAYS_TARGET = 3     # "3 active days this week"
FREEZES_PER_MONTH = 2

# Soft-reset ladder. A break drops to the highest tier strictly below the
# current count, floored at 1 — a long run is never wiped out.
TIERS = (1, 2, 4, 8, 12, 16, 24, 36, 52)

MAX_PERIODS_PER_RECOMPUTE = 260   # ~5 years; bounds a long-dormant account


# ── periods ────────────────────────────────────────────────────────────────
def period_key(unit, day):
    """Stable, sortable key for the period `day` falls in."""
    if unit == "day":
        return day.isoformat()
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def period_start(unit, day):
    if unit == "day":
        return day
    return day - timedelta(days=day.weekday())      # Monday


def next_period_start(unit, start):
    return start + timedelta(days=1 if unit == "day" else 7)


def period_days(unit, start):
    span = 1 if unit == "day" else 7
    return [start + timedelta(days=i) for i in range(span)]


# ── target ─────────────────────────────────────────────────────────────────
def target_for(unit):
    return 1 if unit == "day" else WEEKLY_ACTIVE_DAYS_TARGET


def active_days_in(user_id, unit, start):
    days = period_days(unit, start)
    rows = (ActivityDay.query
            .filter(ActivityDay.user_id == user_id,
                    ActivityDay.activity_date >= days[0],
                    ActivityDay.activity_date <= days[-1])
            .all())
    return sum(1 for r in rows if (r.xp_earned or 0) > 0)


def period_met(user_id, unit, start):
    return active_days_in(user_id, unit, start) >= target_for(unit)


# ── soft reset ─────────────────────────────────────────────────────────────
def soft_reset(count):
    """The highest tier strictly below `count`, never zero once started."""
    if count <= 0:
        return 0
    below = [t for t in TIERS if t < count]
    return max(below) if below else 1


# ── state ──────────────────────────────────────────────────────────────────
def get_or_create(user_id, unit="week"):
    state = db.session.get(StreakState, user_id)
    if state:
        return state
    state = StreakState(user_id=user_id, unit=unit,
                        freezes_available=FREEZES_PER_MONTH,
                        freezes_used={"granted_month": None, "used": []})
    db.session.add(state)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        state = db.session.get(StreakState, user_id)
    return state


def _grant_monthly_freezes(state, today):
    """Top back up to FREEZES_PER_MONTH on entering a new month.

    A top-up rather than an accumulation: rest days are there to absorb a
    normal life event, not to be hoarded into a score of their own.
    """
    ledger = dict(state.freezes_used or {})
    month = f"{today.year}-{today.month:02d}"
    if ledger.get("granted_month") != month:
        ledger["granted_month"] = month
        ledger.setdefault("used", [])
        state.freezes_used = ledger
        state.freezes_available = max(state.freezes_available or 0, FREEZES_PER_MONTH)
        return True
    return False


def recompute(user_id):
    """Fold every completed period since the last count into the streak.

    Returns a dict describing what changed, so the caller can tell the learner
    about an applied rest day AFTER the fact (never before).
    """
    from app.engagement.service import get_or_create_profile

    profile = get_or_create_profile(user_id)
    state = get_or_create(user_id)
    unit = state.unit or "week"

    today = local_date(profile.timezone)
    current_start = period_start(unit, today)
    current_key = period_key(unit, today)

    _grant_monthly_freezes(state, today)

    froze, broke = [], False

    # Where to resume from: the period after the last one counted.
    if state.last_counted_period:
        cursor = _start_after(unit, state.last_counted_period, current_start)
    else:
        cursor = current_start          # nothing counted yet — start here

    # Completed periods strictly before the current one.
    guard = 0
    while cursor < current_start and guard < MAX_PERIODS_PER_RECOMPUTE:
        guard += 1
        if period_met(user_id, unit, cursor):
            state.current_count = (state.current_count or 0) + 1
            state.last_counted_period = period_key(unit, cursor)
        elif (state.current_count or 0) > 0:
            # A missed period only matters if a run is going. Spend a rest day
            # if there is one; otherwise soften the landing.
            if (state.freezes_available or 0) > 0:
                state.freezes_available -= 1
                ledger = dict(state.freezes_used or {})
                used = list(ledger.get("used") or [])
                key = period_key(unit, cursor)
                used.append({"period": key,
                             "at": datetime.now(timezone.utc).isoformat()})
                ledger["used"] = used
                state.freezes_used = ledger
                froze.append(key)
                state.last_counted_period = key
            else:
                state.current_count = soft_reset(state.current_count)
                state.last_counted_period = period_key(unit, cursor)
                broke = True
        cursor = next_period_start(unit, cursor)

    # The current period counts the moment its target is met, so the number the
    # learner sees reflects the work they just did.
    if state.last_counted_period != current_key and period_met(user_id, unit, current_start):
        state.current_count = (state.current_count or 0) + 1
        state.last_counted_period = current_key

    state.best_count = max(state.best_count or 0, state.current_count or 0)
    db.session.commit()
    return {"froze": froze, "soft_reset": broke,
            "current": state.current_count, "best": state.best_count}


def _start_after(unit, last_key, fallback):
    """Start of the period following `last_key`, tolerant of a malformed key."""
    try:
        if unit == "day":
            last = datetime.strptime(last_key, "%Y-%m-%d").date()
            return last + timedelta(days=1)
        year, week = last_key.split("-W")
        monday = datetime.strptime(f"{year}-{int(week)}-1", "%G-%V-%u").date()
        return monday + timedelta(days=7)
    except (ValueError, TypeError):
        return fallback


# ── view ───────────────────────────────────────────────────────────────────
def view(user_id):
    """Display payload. Every string here states what HAS happened.

    There is deliberately no "expires", no countdown, no days-remaining, and no
    reference to what a miss would cost.
    """
    from app.engagement.service import get_or_create_profile

    profile = get_or_create_profile(user_id)
    state = get_or_create(user_id)
    unit = state.unit or "week"
    today = local_date(profile.timezone)
    start = period_start(unit, today)
    active = active_days_in(user_id, unit, start)
    target = target_for(unit)

    count = state.current_count or 0
    noun = "week" if unit == "week" else "day"
    if count == 0:
        label = "No run going yet."
    else:
        label = f"Consistent for {count} {noun}{'' if count == 1 else 's'}."

    return {
        "unit": unit,
        "current_count": count,
        "best_count": state.best_count or 0,
        "label": label,
        "best_label": (f"Your best is {state.best_count} {noun}"
                       f"{'' if state.best_count == 1 else 's'}."
                       if (state.best_count or 0) > 0 else None),
        "period_key": period_key(unit, today),
        "active_days_this_period": active,
        "target_days": target,
        "period_met": active >= target,
        "progress_label": f"{active} of {target} active days this week"
                          if unit == "week" else None,
        "freezes_available": state.freezes_available or 0,
        "freezes_used": list((state.freezes_used or {}).get("used") or []),
        # Everything above is presentational. Nothing consumes it.
        "display_only": True,
    }
