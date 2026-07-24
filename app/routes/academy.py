"""Academy practice-check endpoints (Phase 1).

A `scenario_check` mints a fresh seed-only scenario in the concept's regime,
starts a reveal-capped practice session (mode="practice" → server-authoritative
incremental reveal, exactly like contests), and grades server-side via the
mission engine. Retrying serves a NEW seed (new market, same concept), so the
answer can't be memorised. No seed or unrevealed bar ever reaches the client.
"""
import random
from flask import Blueprint, jsonify, request

from app import db, bar_provider
from app import academy
from app.models.scenario import Scenario
from app.models.session import Session
from app.engine import CURRENT_ENGINE
from app.rules import evaluate_discipline, session_context, check_mission_rules

bp = Blueprint("academy", __name__)


def _rule_labels(spec):
    return [{"type": r["type"], "label": r["label"]} for r in spec["rules"]]


@bp.route("/academy/practice/start", methods=["POST"])
def practice_start():
    """Start a concept-matched practice check. Body: {user_id, check_id | concept_tag}.
    Mints a fresh scenario in the concept's regime and returns the session + goal +
    rule set + reveal window. Never returns a seed or unrevealed bars."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "anonymous")
    check_id = body.get("check_id")
    concept = academy.concept_for_check(check_id) if check_id else body.get("concept_tag")
    spec = academy.spec_for(concept)
    if not spec:
        return jsonify({"error": "unknown concept"}), 400

    regime = random.choice(spec["regimes"])
    seed = random.randint(1, 10 ** 9)                 # fresh market every attempt
    warmup, live = spec["warmup_bars"], spec["live_bars"]
    n_bars = warmup + live

    scenario = Scenario(
        name_internal=f"practice_{concept}_{seed}",
        asset_class="synthetic",
        timeframe=spec.get("anchor_tf", "1D"),
        difficulty_tier=1,
        tags=["practice", concept, regime],
        is_active=False,                              # never listed in the scenario picker
        history_bars=warmup,
        engine_version=CURRENT_ENGINE, seed=seed,
        gen_params={"kind": "regime", "n_bars": n_bars, "regime": regime},
    )
    db.session.add(scenario)
    db.session.commit()

    # Reveal-capped session: the warm-up block is revealed immediately; live bars
    # are released one-per-advance, server-authoritative. bars_served is the last
    # revealed bar INDEX, so warmup-1 makes exactly `warmup` bars visible.
    session = Session(user_id=user_id, scenario_id=scenario.id,
                      status="in_progress", mode="practice", bars_served=warmup - 1)
    db.session.add(session)
    db.session.commit()

    return jsonify({
        "session_id": session.id,
        "scenario_id": scenario.id,
        "concept": concept,
        "goal": spec["goal"],
        "rules": _rule_labels(spec),
        "warmup_bars": warmup,
        "live_bars": live,
        "total_bars": n_bars,
        "history_bars": warmup,
        "starting_balance": session.starting_balance,
        "is_fallback": check_id in academy.FALLBACK_CHECKS if check_id else False,
    })


@bp.route("/academy/practice/<int:session_id>/status", methods=["GET"])
def practice_status(session_id):
    """Live rule HUD for an in-progress practice session — the same mission-engine
    evaluation used to grade, so the learner sees which rules they're meeting."""
    session = Session.query.get_or_404(session_id)
    scenario = Scenario.query.get_or_404(session.scenario_id)
    concept = academy.concept_of_scenario(scenario)
    spec = academy.spec_for(concept)
    if not spec:
        return jsonify({"error": "not a practice session"}), 400
    disc = evaluate_discipline(session)
    ctx = session_context(session, disc)
    passed, results = check_mission_rules(spec["rules"], ctx)
    return jsonify({"passed": passed, "results": results, "goal": spec["goal"], "concept": concept})


@bp.route("/academy/practice/<int:session_id>/grade", methods=["POST"])
def practice_grade(session_id):
    """Finalise + grade a practice session. Flattens anything still open at the last
    revealed bar (so the run counts), scores/journals it via the shared finalizer,
    then evaluates the concept's rule set. Idempotent."""
    session = Session.query.get_or_404(session_id)
    scenario = Scenario.query.get_or_404(session.scenario_id)
    concept = academy.concept_of_scenario(scenario)
    spec = academy.spec_for(concept)
    if not spec:
        return jsonify({"error": "not a practice session"}), 400

    # Flatten open/pending orders at the last revealed bar (mirrors contest submit).
    from app.routes.game import _settle_trade, _cost_model, _finalize_session
    if session.status == "in_progress":
        last = bar_provider.at(scenario, session.bars_served or 0)
        if last:
            slip = _cost_model(session)["slippage_pct"]
            for t in session.trades:
                if t.status == "open":
                    _settle_trade(t, last.bar_sequence, last.close, "manual", slip)
                elif t.status == "pending":
                    db.session.delete(t)
            db.session.commit()

    result = _finalize_session(session)              # score + journal (mode=practice)
    passed, results, disc = academy.grade(spec, session)

    return jsonify({
        "passed": passed,
        "results": results,
        "concept": concept,
        "goal": spec["goal"],
        "score_composite": result.get("score_composite"),
        "discipline": result.get("discipline"),
        "blown": result.get("blown"),
    })
