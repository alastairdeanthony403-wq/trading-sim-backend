"""Engagement layer endpoints (Phase 1).

Deliberately small: the XP ledger's total, the learner's own settings, and the
one-shot import of XP that predates the server knowing about it. The richer
goal/streak/next-goal payload is Phase 2's `/engagement/summary`.

There is ONE progression authority: career level (app.routes.progress), which
is derived from process metrics and gates tools, markets and modes. XP is the
visible currency earned alongside it — it is reported here with the career tier
it sits in, so the UI never shows two disagreeing "levels".
"""
from flask import Blueprint, jsonify, request

from app import db
from app.models.engagement import (EngagementProfile, GOAL_TYPES, Milestone,
                                   UserMilestone, XpEvent)
from app.engagement.awards import import_legacy_xp
from app.engagement.day import local_date
from app.engagement.feedback import consistency, goal_progress, next_goal
from app.engagement import streaks
from app.engagement.service import (get_or_create_profile, get_or_create_day,
                                    total_xp)

bp = Blueprint("engagement", __name__)

MAX_RECENT_EVENTS = 20


def _profile_view(p):
    return {
        "user_id": p.user_id,
        "timezone": p.timezone,
        "daily_goal_type": p.daily_goal_type,
        "daily_goal_target": p.daily_goal_target,
        "weekday_only": p.weekday_only,
        "sound_enabled": p.sound_enabled,
        "reduced_motion_override": p.reduced_motion_override,
        "theme": p.theme,
        "nudges_opt_in": p.nudges_opt_in,
        "quiet_hours_start": p.quiet_hours_start,
        "quiet_hours_end": p.quiet_hours_end,
    }


def _day_view(row):
    return {
        "date": row.activity_date.isoformat(),
        "xp_earned": row.xp_earned,
        "lessons_completed": row.lessons_completed,
        "scenarios_completed": row.scenarios_completed,
        "active_seconds": row.active_seconds,
        "goal_met": row.goal_met,
    }


@bp.route("/engagement/xp/<string:user_id>", methods=["GET"])
def get_xp(user_id):
    from app.routes.progress import (_career_level, _career_metrics, _level_name,
                                     _next_tier, _requirements_view,
                                     get_or_create_progress)

    profile = get_or_create_profile(user_id)
    today = get_or_create_day(user_id, local_date(profile.timezone))
    recent = (XpEvent.query.filter_by(user_id=user_id)
              .order_by(XpEvent.awarded_at.desc()).limit(MAX_RECENT_EVENTS).all())

    # Career level is the one progression authority; XP rides alongside it. The
    # bar is the share of the NEXT tier's requirements already met — a real,
    # bounded fraction, not an XP threshold that gates nothing.
    metrics = _career_metrics(user_id, get_or_create_progress(user_id))
    level = _career_level(metrics)["level"]
    nxt = _next_tier(level)
    requirements = _requirements_view(nxt, metrics) if nxt else []
    met = sum(1 for r in requirements if r["met"])

    return jsonify({
        "user_id": user_id,
        "total_xp": total_xp(user_id),
        "career_level": level,
        "career_level_name": _level_name(level),
        "next_level_name": nxt["name"] if nxt else None,
        "career_progress": (met / len(requirements)) if requirements else 1.0,
        "requirements": requirements,
        "today": _day_view(today),
        "recent": [{"source_type": e.source_type, "source_id": e.source_id,
                    "amount": e.amount, "awarded_at": e.awarded_at.isoformat(),
                    "meta": e.meta} for e in recent],
    })


@bp.route("/engagement/summary/<string:user_id>", methods=["GET"])
def get_summary(user_id):
    """Everything the loop surfaces in one read: today's goal and how far into
    it the learner is, the single next concrete action, XP, career standing and
    what remains for the next tier, plus this week's consistency.

    Note the path is /engagement/summary rather than /api/engagement/summary —
    no blueprint in this app carries an /api prefix.
    """
    from app.routes.progress import (_career_level, _career_metrics, _level_name,
                                     _next_tier, _requirements_view,
                                     get_or_create_progress)

    profile = get_or_create_profile(user_id)
    today = get_or_create_day(user_id, local_date(profile.timezone))

    metrics = _career_metrics(user_id, get_or_create_progress(user_id))
    level = _career_level(metrics)["level"]
    nxt = _next_tier(level)
    requirements = _requirements_view(nxt, metrics) if nxt else []
    met = sum(1 for r in requirements if r["met"])

    return jsonify({
        "user_id": user_id,
        "total_xp": total_xp(user_id),
        "career_level": level,
        "career_level_name": _level_name(level),
        "next_level_name": nxt["name"] if nxt else None,
        "career_progress": (met / len(requirements)) if requirements else 1.0,
        "requirements": requirements,
        "goal": goal_progress(profile, today),
        "next_goal": next_goal(user_id, profile, today),
        "consistency": consistency(user_id, profile),
        "streak": streaks.view(user_id),
        "today": _day_view(today),
        "profile": _profile_view(profile),
    })


@bp.route("/engagement/milestones/<string:user_id>", methods=["GET"])
def get_milestones(user_id):
    """The gallery. Locked milestones ship their REAL criteria and the learner's
    live progress toward them — there are no teasers or mystery entries here,
    so the payload is identical in shape whether locked or not."""
    from app.engagement import milestones as ms

    catalogue = Milestone.query.order_by(Milestone.sort_order, Milestone.id).all()
    owned = {um.milestone_id: um for um in
             UserMilestone.query.filter_by(user_id=user_id).all()}
    metrics = ms.compute_metrics(user_id)

    items = [ms.view(m, owned.get(m.id), metrics) for m in catalogue]
    by_category = {}
    for item in items:
        by_category.setdefault(item["category"], []).append(item)

    return jsonify({
        "user_id": user_id,
        "unlocked_count": sum(1 for i in items if i["unlocked"]),
        "total_count": len(items),
        "categories": by_category,
        "milestones": items,
    })


@bp.route("/engagement/xp/import", methods=["POST"])
def import_xp():
    """One-shot migration of the pre-server localStorage XP total.

    Clamped and single-fire (see awards.import_legacy_xp). A second call, or a
    learner who has already imported, is reported as a duplicate — not an error.
    """
    body = request.get_json(force=True) or {}
    user_id = body.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    result = import_legacy_xp(user_id, body.get("xp"))
    return jsonify({"imported": result["awarded"], "duplicate": result["duplicate"],
                    "total_xp": result["total_xp"]})


@bp.route("/engagement/profile/<string:user_id>", methods=["GET"])
def get_profile(user_id):
    return jsonify(_profile_view(get_or_create_profile(user_id)))


@bp.route("/engagement/profile/<string:user_id>", methods=["PATCH"])
def update_profile(user_id):
    """Every field here is the learner's to change, in either direction and at
    any time. Lowering a daily goal is as valid as raising it."""
    profile = get_or_create_profile(user_id)
    body = request.get_json(force=True) or {}

    if "timezone" in body:
        from zoneinfo import ZoneInfo
        tz = str(body["timezone"] or "")
        try:
            ZoneInfo(tz)
        except Exception:
            return jsonify({"error": f"unknown timezone: {tz}"}), 400
        profile.timezone = tz

    if "daily_goal_type" in body:
        if body["daily_goal_type"] not in GOAL_TYPES:
            return jsonify({"error": f"daily_goal_type must be one of {list(GOAL_TYPES)}"}), 400
        profile.daily_goal_type = body["daily_goal_type"]

    if "daily_goal_target" in body:
        try:
            target = int(body["daily_goal_target"])
        except (TypeError, ValueError):
            return jsonify({"error": "daily_goal_target must be an integer"}), 400
        if target < 1:
            return jsonify({"error": "daily_goal_target must be at least 1"}), 400
        profile.daily_goal_target = target

    for field in ("weekday_only", "sound_enabled", "nudges_opt_in"):
        if field in body:
            setattr(profile, field, bool(body[field]))

    if "reduced_motion_override" in body:
        val = body["reduced_motion_override"]
        profile.reduced_motion_override = None if val is None else bool(val)

    if "theme" in body:
        profile.theme = (str(body["theme"])[:32] if body["theme"] else None)

    for field in ("quiet_hours_start", "quiet_hours_end"):
        if field in body:
            val = body[field]
            if val is None:
                setattr(profile, field, None)
                continue
            try:
                hour = int(val)
            except (TypeError, ValueError):
                return jsonify({"error": f"{field} must be an hour 0-23"}), 400
            if not 0 <= hour <= 23:
                return jsonify({"error": f"{field} must be an hour 0-23"}), 400
            setattr(profile, field, hour)

    db.session.commit()
    return jsonify(_profile_view(profile))
