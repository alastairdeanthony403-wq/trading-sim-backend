from flask import Blueprint, jsonify, request
from app import db
from app.models.progress import UserProgress, Leaderboard
from app.models.scenario import Scenario

bp = Blueprint("progress", __name__)

# The curriculum: an ordered list of units, each with ordered lessons.
# Lessons unlock by completing the previous lesson (a proper learning path),
# not by trading score. Each unit ends with a knowledge check.
CURRICULUM = [
    {
        "unit": 1,
        "title": "Foundations",
        "lessons": ["how_markets_work", "order_types"],
        "check": "check_foundations",
    },
    {
        "unit": 2,
        "title": "Reading the chart",
        "lessons": ["chart_reading_basics", "support_resistance", "trends_conditions"],
        "check": "check_reading",
    },
    {
        "unit": 3,
        "title": "Structure & zones",
        "lessons": ["market_structure", "core_indicators", "supply_demand_zones", "liquidity_concepts"],
        "check": "check_structure",
    },
    {
        "unit": 4,
        "title": "Risk & process",
        "lessons": ["risk_basics", "risk_management", "session_concepts", "advanced_confluence", "fundamentals_news"],
        "check": "check_risk",
    },
    {
        "unit": 5,
        "title": "Discipline",
        "lessons": ["trade_journaling", "trading_plan", "psychology_discipline"],
        "check": "check_discipline",
    },
]

# Flattened ordered list of all learning-path items (lessons + checks interleaved)
def ordered_path():
    path = []
    for unit in CURRICULUM:
        for lesson_id in unit["lessons"]:
            path.append({"type": "lesson", "id": lesson_id, "unit": unit["unit"]})
        path.append({"type": "check", "id": unit["check"], "unit": unit["unit"]})
    return path


TIER_UNLOCKS = [
    {"tier": 1, "threshold": 0},
    {"tier": 2, "threshold": 20},
]


# Progressive interface complexity: which simulator tools are unlocked at each
# level. Server-side so it's tunable, and so unlocks can't be forged by the
# client. Interim gating is keyed off scenarios completed; Phase D's career
# system will replace `_tool_level` with the real requirement matrix while
# keeping this same tool vocabulary + endpoint contract.
#   sl_tp | limit_stop | trailing | multi_position | leverage
TOOL_TIERS = [
    {"level": 1, "min_scenarios": 0,  "tools": []},
    {"level": 2, "min_scenarios": 1,  "tools": ["sl_tp"]},
    {"level": 3, "min_scenarios": 3,  "tools": ["sl_tp", "limit_stop"]},
    {"level": 4, "min_scenarios": 5,  "tools": ["sl_tp", "limit_stop", "trailing"]},
    {"level": 5, "min_scenarios": 8,  "tools": ["sl_tp", "limit_stop", "trailing", "multi_position"]},
    {"level": 6, "min_scenarios": 12, "tools": ["sl_tp", "limit_stop", "trailing", "multi_position", "leverage"]},
]


def _tool_level(progress):
    """(unlocked_tools, level) for a user's progress."""
    n = (progress.total_scenarios_completed or 0)
    tools, level = [], 1
    for tier in TOOL_TIERS:
        if n >= tier["min_scenarios"]:
            tools, level = tier["tools"], tier["level"]
    return tools, level


def get_or_create_progress(user_id):
    progress = UserProgress.query.filter_by(user_id=user_id).first()
    if not progress:
        progress = UserProgress(
            user_id=user_id,
            unlocked_lessons=[],
            completed_lessons=[],
            unlocked_scenario_tiers=[TIER_UNLOCKS[0]["tier"]],
            total_scenarios_completed=0,
            best_composite_score=None,
        )
        db.session.add(progress)
        db.session.commit()
    return progress


def compute_next_item(completed):
    """Return the first item in the ordered path that isn't completed yet."""
    completed_set = set(completed or [])
    for item in ordered_path():
        if item["id"] not in completed_set:
            return item
    return None  # everything done


@bp.route("/progress/<string:user_id>", methods=["GET"])
def get_progress(user_id):
    progress = get_or_create_progress(user_id)
    completed = progress.completed_lessons or []
    next_item = compute_next_item(completed)
    unlocked_tools, tool_level = _tool_level(progress)

    return jsonify({
        "user_id": progress.user_id,
        "completed_lessons": completed,
        "next_item": next_item,
        "curriculum": CURRICULUM,
        "ordered_path": ordered_path(),
        "unlocked_scenario_tiers": progress.unlocked_scenario_tiers,
        "total_scenarios_completed": progress.total_scenarios_completed,
        "best_composite_score": progress.best_composite_score,
        "all_tiers": TIER_UNLOCKS,
        "unlocked_tools": unlocked_tools,
        "tool_level": tool_level,
    })


@bp.route("/config/tools/<string:user_id>", methods=["GET"])
def get_tools(user_id):
    """Server-authoritative list of unlocked simulator tools for this user."""
    progress = get_or_create_progress(user_id)
    unlocked_tools, tool_level = _tool_level(progress)
    return jsonify({
        "user_id": user_id,
        "unlocked_tools": unlocked_tools,
        "tool_level": tool_level,
        "tool_tiers": TOOL_TIERS,
    })


@bp.route("/progress/<string:user_id>/complete", methods=["POST"])
def mark_complete(user_id):
    progress = get_or_create_progress(user_id)
    body = request.get_json(force=True)
    item_id = body["item_id"]

    completed = set(progress.completed_lessons or [])
    completed.add(item_id)
    progress.completed_lessons = list(completed)
    db.session.commit()

    next_item = compute_next_item(progress.completed_lessons)
    return jsonify({
        "completed_lessons": progress.completed_lessons,
        "next_item": next_item,
    })


def apply_score_to_progress(user_id, composite_score):
    """Called after a trading session ends. Updates score + scenario tier unlocks."""
    progress = get_or_create_progress(user_id)
    progress.total_scenarios_completed = (progress.total_scenarios_completed or 0) + 1

    if progress.best_composite_score is None or composite_score > progress.best_composite_score:
        progress.best_composite_score = composite_score

    unlocked_tiers = set(progress.unlocked_scenario_tiers or [])
    for tier in TIER_UNLOCKS:
        if progress.best_composite_score is not None and progress.best_composite_score >= tier["threshold"]:
            unlocked_tiers.add(tier["tier"])
    progress.unlocked_scenario_tiers = list(unlocked_tiers)

    db.session.commit()
    return progress


@bp.route("/scenarios/<int:scenario_id>/leaderboard", methods=["GET"])
def get_leaderboard(scenario_id):
    Scenario.query.get_or_404(scenario_id)
    entries = (
        Leaderboard.query
        .filter_by(scenario_id=scenario_id)
        .order_by(Leaderboard.composite_score.desc())
        .limit(10)
        .all()
    )
    return jsonify([
        {
            "rank": i + 1,
            "user_id": e.user_id,
            "composite_score": e.composite_score,
            "achieved_at": e.achieved_at.isoformat() if e.achieved_at else None,
        }
        for i, e in enumerate(entries)
    ])
