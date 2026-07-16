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


# ═══════════════════════════════════════════════════════════════════════════
# CAREER SYSTEM (Phase D) — the primary progression. SKILL-gated, never profit.
# One server-side config drives career level, which in turn drives which tools
# and which markets are unlocked. Requirements combine academy completion,
# missions passed, and discipline aggregates (see UserProgress) — deliberately
# NOT total P&L, so a reckless profitable run does not advance a career.
# ═══════════════════════════════════════════════════════════════════════════
CAREER_LEVELS = [
    {"level": 1, "key": "market_rookie",       "name": "Market Rookie",       "requires": {}},
    {"level": 2, "key": "junior_trader",       "name": "Junior Trader",       "requires": {"sessions_scored": 3, "missions_passed": 1}},
    {"level": 3, "key": "market_analyst",      "name": "Market Analyst",      "requires": {"lessons_completed": 6, "missions_passed": 3, "pct_trades_with_stops": 0.70}},
    {"level": 4, "key": "portfolio_trader",    "name": "Portfolio Trader",    "requires": {"missions_passed": 6, "avg_discipline": 70, "pct_trades_with_stops": 0.80}},
    {"level": 5, "key": "professional_trader", "name": "Professional Trader", "requires": {"lessons_completed": 12, "missions_passed": 9, "avg_discipline": 80, "pct_trades_with_stops": 0.90}},
    {"level": 6, "key": "fund_manager",        "name": "Fund Manager",        "requires": {"missions_passed": 12, "avg_discipline": 85, "pct_trades_with_stops": 0.90}},
    {"level": 7, "key": "market_strategist",   "name": "Market Strategist",   "requires": {"lessons_completed": 17, "missions_passed": 15, "avg_discipline": 90, "pct_trades_with_stops": 0.95}},
]

REQ_LABELS = {
    "sessions_scored": "Sessions completed",
    "missions_passed": "Missions passed",
    "lessons_completed": "Lessons completed",
    "pct_trades_with_stops": "Share of trades with a stop",
    "avg_discipline": "Average discipline score",
}

# tools unlocked at each career level (cumulative)
TOOL_UNLOCKS_BY_LEVEL = {
    1: [],
    2: ["sl_tp"],
    3: ["sl_tp", "limit_stop"],
    4: ["sl_tp", "limit_stop", "trailing", "multi_position"],
    5: ["sl_tp", "limit_stop", "trailing", "multi_position", "leverage"],
}

# asset class → minimum career level to trade it. "equity" and "stocks" are the
# same entry-level market (ingest tags equities as "equity").
MARKET_UNLOCKS = {"stocks": 1, "equity": 1, "crypto": 2, "forex": 3, "indices": 4, "commodities": 5}


def _career_metrics(user_id, progress):
    from app.models.mission import MissionAttempt
    lesson_ids = set()
    for unit in CURRICULUM:
        lesson_ids.update(unit["lessons"])
    completed = set(progress.completed_lessons or [])
    passed_missions = {a.mission_id for a in
                       MissionAttempt.query.filter_by(user_id=user_id, passed=True).all()}
    total_trades = progress.total_trades_all or 0
    sessions = progress.sessions_scored or 0
    return {
        "lessons_completed": len(lesson_ids & completed),
        "missions_passed": len(passed_missions),
        "sessions_scored": sessions,
        "pct_trades_with_stops": ((progress.trades_with_stops_all or 0) / total_trades)
                                 if total_trades else 1.0,
        "avg_discipline": ((progress.discipline_sum or 0.0) / sessions) if sessions else 0.0,
        "blown_count": progress.blown_count or 0,
    }


def _level_met(reqs, metrics):
    return all(metrics.get(k, 0) >= target for k, target in reqs.items())


def _career_level(metrics):
    level = CAREER_LEVELS[0]
    for tier in CAREER_LEVELS[1:]:
        if _level_met(tier["requires"], metrics):
            level = tier
        else:
            break   # requirements are monotonic — stop at the first unmet tier
    return level


def _next_tier(level):
    for tier in CAREER_LEVELS:
        if tier["level"] == level + 1:
            return tier
    return None


def _tools_for_level(level):
    tools = []
    for lvl in range(1, level + 1):
        if lvl in TOOL_UNLOCKS_BY_LEVEL:
            tools = TOOL_UNLOCKS_BY_LEVEL[lvl]
    return tools


def _unlocked_markets(level):
    return [m for m, req in MARKET_UNLOCKS.items() if level >= req]


def _requirements_view(tier, metrics):
    out = []
    for k, target in tier["requires"].items():
        cur = metrics.get(k, 0)
        out.append({
            "key": k, "label": REQ_LABELS.get(k, k),
            "current": round(cur, 2) if isinstance(cur, float) else cur,
            "target": target, "met": cur >= target,
        })
    return out


def _level_unlocks(level):
    return {"tools": _tools_for_level(level), "markets": _unlocked_markets(level)}


def _tool_level(progress):
    """(unlocked_tools, career_level) — tool gating now follows career level."""
    metrics = _career_metrics(progress.user_id, progress)
    level = _career_level(metrics)["level"]
    return _tools_for_level(level), level


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
        "unlocked_markets": _unlocked_markets(tool_level),
    })


@bp.route("/career/<string:user_id>", methods=["GET"])
def get_career(user_id):
    """Career level, the checklist to the next level, and what each level
    unlocks. Server-authoritative and skill-gated."""
    progress = get_or_create_progress(user_id)
    metrics = _career_metrics(user_id, progress)
    current = _career_level(metrics)
    nxt = _next_tier(current["level"])
    return jsonify({
        "user_id": user_id,
        "level": current["level"], "key": current["key"], "name": current["name"],
        "metrics": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in metrics.items()},
        "unlocked_tools": _tools_for_level(current["level"]),
        "unlocked_markets": _unlocked_markets(current["level"]),
        "next": None if not nxt else {
            "level": nxt["level"], "name": nxt["name"],
            "requirements": _requirements_view(nxt, metrics),
            "unlocks": _level_unlocks(nxt["level"]),
        },
        "all_levels": [
            {"level": t["level"], "name": t["name"], "key": t["key"],
             "unlocks": _level_unlocks(t["level"])}
            for t in CAREER_LEVELS
        ],
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
