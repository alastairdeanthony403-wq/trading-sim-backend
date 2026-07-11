from flask import Blueprint, jsonify
from app import db
from app.models.progress import UserProgress, Leaderboard
from app.models.scenario import Scenario

bp = Blueprint("progress", __name__)

# Composite score thresholds that unlock new lesson content and scenario tiers.
# Kept simple and hardcoded for MVP -- easy to expand later.
LESSON_UNLOCKS = [
    {"id": "risk_basics", "threshold": 0},
    {"id": "risk_management", "threshold": 15},
    {"id": "session_concepts", "threshold": 35},
    {"id": "advanced_confluence", "threshold": 60},
]

TIER_UNLOCKS = [
    {"tier": 1, "threshold": 0},
    {"tier": 2, "threshold": 20},
]


def get_or_create_progress(user_id):
    progress = UserProgress.query.filter_by(user_id=user_id).first()
    if not progress:
        progress = UserProgress(
            user_id=user_id,
            unlocked_lessons=[LESSON_UNLOCKS[0]["id"]],
            unlocked_scenario_tiers=[TIER_UNLOCKS[0]["tier"]],
            total_scenarios_completed=0,
            best_composite_score=None,
        )
        db.session.add(progress)
        db.session.commit()
    return progress


def apply_score_to_progress(user_id, composite_score):
    """Called after a session ends. Updates progress and unlocks."""
    progress = get_or_create_progress(user_id)
    progress.total_scenarios_completed = (progress.total_scenarios_completed or 0) + 1

    if progress.best_composite_score is None or composite_score > progress.best_composite_score:
        progress.best_composite_score = composite_score

    unlocked_lessons = set(progress.unlocked_lessons or [])
    for lesson in LESSON_UNLOCKS:
        if progress.best_composite_score is not None and progress.best_composite_score >= lesson["threshold"]:
            unlocked_lessons.add(lesson["id"])
    progress.unlocked_lessons = list(unlocked_lessons)

    unlocked_tiers = set(progress.unlocked_scenario_tiers or [])
    for tier in TIER_UNLOCKS:
        if progress.best_composite_score is not None and progress.best_composite_score >= tier["threshold"]:
            unlocked_tiers.add(tier["tier"])
    progress.unlocked_scenario_tiers = list(unlocked_tiers)

    db.session.commit()
    return progress


@bp.route("/progress/<string:user_id>", methods=["GET"])
def get_progress(user_id):
    progress = get_or_create_progress(user_id)
    return jsonify({
        "user_id": progress.user_id,
        "unlocked_lessons": progress.unlocked_lessons,
        "unlocked_scenario_tiers": progress.unlocked_scenario_tiers,
        "total_scenarios_completed": progress.total_scenarios_completed,
        "best_composite_score": progress.best_composite_score,
        "all_lessons": LESSON_UNLOCKS,
        "all_tiers": TIER_UNLOCKS,
    })


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
