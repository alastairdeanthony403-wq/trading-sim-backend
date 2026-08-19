"""The single write path for XP, and the daily activity rollup behind it.

Nothing outside this module may INSERT an XpEvent. Every award goes through
award_xp(), which enforces the two integrity guarantees:

  * idempotency — the unique idempotency_key means a replayed request (a
    double-tapped button, a retried POST, a re-submitted mission) awards once
    and reports the replay as a duplicate rather than an error.
  * local-day truth — the activity rollup lands on the learner's own calendar
    date, computed from their stored timezone (see app.engagement.day).

Amounts come from app.engagement.xp_rules, which is profit-blind by design.
"""
from datetime import datetime, timezone, timedelta, time

from sqlalchemy.exc import IntegrityError

from app import db
from app.models.engagement import (ActivityDay, EngagementProfile, XpEvent,
                                   GOAL_TYPES, SOURCE_TYPES)
from app.engagement import xp_rules
from app.engagement.day import local_date, zone_for


# ── profile ────────────────────────────────────────────────────────────────
def get_or_create_profile(user_id):
    profile = db.session.get(EngagementProfile, user_id)
    if profile:
        return profile
    profile = EngagementProfile(user_id=user_id)
    db.session.add(profile)
    try:
        db.session.commit()
    except IntegrityError:
        # Get-or-create race — a concurrent request inserted first. Use theirs.
        db.session.rollback()
        profile = db.session.get(EngagementProfile, user_id)
    return profile


# ── the write path ─────────────────────────────────────────────────────────
def award_xp(user_id, source_type, source_id, amount, idempotency_key, meta=None):
    """Award XP once. Returns {awarded, duplicate, total_xp, activity_date}.

    A non-positive amount is a no-op (so callers can compute a value and hand it
    over without branching). An unknown source_type is a programming error and
    raises — the ledger's vocabulary is fixed in app.models.engagement.
    """
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown xp source_type: {source_type}")

    amount = int(amount or 0)
    if amount <= 0:
        return {"awarded": 0, "duplicate": False,
                "total_xp": total_xp(user_id), "activity_date": None}

    if XpEvent.query.filter_by(idempotency_key=idempotency_key).first():
        return {"awarded": 0, "duplicate": True,
                "total_xp": total_xp(user_id), "activity_date": None}

    profile = get_or_create_profile(user_id)
    awarded_at = datetime.now(timezone.utc)

    event = XpEvent(
        user_id=user_id,
        source_type=source_type,
        source_id=str(source_id) if source_id is not None else None,
        amount=amount,
        idempotency_key=idempotency_key,
        awarded_at=awarded_at,
        meta=meta or {},
    )
    db.session.add(event)
    try:
        db.session.commit()
    except IntegrityError:
        # Lost an idempotency race with a concurrent identical award.
        db.session.rollback()
        return {"awarded": 0, "duplicate": True,
                "total_xp": total_xp(user_id), "activity_date": None}

    day = local_date(profile.timezone, awarded_at)
    _roll_up(profile, day, xp=amount, source_type=source_type)

    return {"awarded": amount, "duplicate": False,
            "total_xp": total_xp(user_id), "activity_date": day.isoformat()}


def total_xp(user_id):
    """Always re-derived from the ledger — there is no cached total to drift."""
    return int(db.session.query(db.func.coalesce(db.func.sum(XpEvent.amount), 0))
               .filter(XpEvent.user_id == user_id).scalar() or 0)


def record_active_time(user_id, seconds):
    """Add engaged time to today's row (drives the `minutes` daily goal)."""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return
    profile = get_or_create_profile(user_id)
    _roll_up(profile, local_date(profile.timezone), active_seconds=seconds)


# ── daily rollup ───────────────────────────────────────────────────────────
def get_or_create_day(user_id, day):
    row = ActivityDay.query.filter_by(user_id=user_id, activity_date=day).first()
    if row:
        return row
    row = ActivityDay(user_id=user_id, activity_date=day)
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        row = ActivityDay.query.filter_by(user_id=user_id, activity_date=day).first()
    return row


def _roll_up(profile, day, xp=0, source_type=None, active_seconds=0):
    row = get_or_create_day(profile.user_id, day)
    row.xp_earned = (row.xp_earned or 0) + xp
    row.active_seconds = (row.active_seconds or 0) + active_seconds

    # Only the headline source for each activity increments its counter, so a
    # session that emits several process awards still counts as one session.
    if source_type == "lesson_complete":
        row.lessons_completed = (row.lessons_completed or 0) + 1
    elif source_type == "scenario_complete":
        row.scenarios_completed = (row.scenarios_completed or 0) + 1

    row.goal_met = evaluate_goal(profile, row)
    db.session.commit()
    return row


def evaluate_goal(profile, row):
    """Did this day meet the learner's chosen goal? Purely factual — it counts
    what happened. `weekday_only` shapes which days a streak *asks* for, which
    is Phase 4's business, so it deliberately does not fake a met goal here."""
    target = profile.daily_goal_target or 0
    if target <= 0:
        return False
    goal = profile.daily_goal_type
    if goal == "lessons":
        return (row.lessons_completed or 0) >= target
    if goal == "sessions":
        return (row.scenarios_completed or 0) >= target
    if goal == "minutes":
        return (row.active_seconds or 0) >= target * 60
    if goal == "xp":
        return (row.xp_earned or 0) >= target
    return False


# ── local-day queries (used by the paper cap, and by Phases 2/4) ───────────
def local_day_bounds_utc(tz_name, day):
    """[start, end) in naive UTC for a learner's local calendar date."""
    zone = zone_for(tz_name)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def events_on_local_day(user_id, tz_name, day):
    start, end = local_day_bounds_utc(tz_name, day)
    return (XpEvent.query
            .filter(XpEvent.user_id == user_id,
                    XpEvent.awarded_at >= start, XpEvent.awarded_at < end)
            .all())


def paper_xp_remaining_today(user_id, tz_name=None):
    """How much more paper-sourced XP this learner may earn today.

    Paper trading is self-serve and unlimited, so without a ceiling the optimal
    strategy is grinding five-minute runs. Everything else is uncapped.
    """
    profile = get_or_create_profile(user_id)
    tz_name = tz_name or profile.timezone
    spent = sum(e.amount for e in events_on_local_day(user_id, tz_name, local_date(tz_name))
                if (e.meta or {}).get("mode") == "paper")
    return max(0, xp_rules.PAPER_DAILY_XP_CAP - spent)
