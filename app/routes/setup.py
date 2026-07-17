import os
import random
from flask import Blueprint, jsonify, request
from app import db

bp = Blueprint("setup", __name__)


def _authorized():
    provided_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("SETUP_KEY")
    return bool(expected_key) and provided_key == expected_key


@bp.route("/setup/create-tables", methods=["POST"])
def create_tables():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    db.create_all()
    return jsonify({"status": "ok", "message": "tables created"})


@bp.route("/setup/db-stamp-baseline", methods=["POST"])
def db_stamp_baseline():
    """One-time transition to Alembic for a database whose tables already
    exist (built via create-tables + manual ALTERs). Marks the schema as
    already at the baseline revision WITHOUT recreating anything. Run this
    exactly once, right after the Alembic release deploys, before applying
    any further migrations."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    from flask_migrate import stamp
    revision = (request.get_json(silent=True) or {}).get("revision", "head")
    try:
        stamp(revision=revision)
        return jsonify({"status": "ok", "message": f"stamped {revision}"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@bp.route("/setup/db-upgrade", methods=["POST"])
def db_upgrade():
    """Apply all pending Alembic migrations up to head. Safe to call after
    every deploy that ships new migrations (idempotent — a no-op when already
    current)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    from flask_migrate import upgrade
    try:
        upgrade()
        return jsonify({"status": "ok", "message": "migrated to head"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@bp.route("/setup/generate-scenarios", methods=["POST"])
def generate_scenarios():
    """Mint synthetic regime-switching scenarios (Phase E). No external API, so
    this is unlimited and safe to re-run. Body (all optional):
        {"regimes": ["crash","range",...],  # defaults to all regimes
         "per_regime": 2,                    # how many of each
         "n_bars": 120,
         "asset_class": "synthetic",
         "seed": 12345}                       # base seed for reproducibility
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    from app.models.scenario import Scenario, ScenarioBar
    from app.synthetic import generate_series, make_scenario_spec, REGIMES

    body = request.get_json(silent=True) or {}
    regimes = body.get("regimes") or REGIMES
    per_regime = int(body.get("per_regime", 2))
    n_bars = int(body.get("n_bars", 120))
    asset_class = body.get("asset_class", "synthetic")
    base_seed = body.get("seed")
    base = int(base_seed) if base_seed is not None else random.randint(1, 10 ** 9)

    created = []
    for regime in regimes:
        if regime not in REGIMES:
            created.append({"regime": regime, "status": "skipped", "detail": "unknown regime"})
            continue
        for k in range(per_regime):
            seed = base + hash(regime) % 100000 + k
            bars = generate_series(regime=regime, n_bars=n_bars, seed=seed)
            spec = make_scenario_spec(regime, seed)
            scenario = Scenario(
                name_internal=spec["name_internal"],
                asset_class=asset_class,
                timeframe="1D",
                difficulty_tier=spec["difficulty_tier"],
                tags=spec["tags"],
                is_active=True,
            )
            db.session.add(scenario)
            db.session.flush()
            for i, b in enumerate(bars):
                db.session.add(ScenarioBar(
                    scenario_id=scenario.id, bar_sequence=i,
                    open=b["open"], high=b["high"], low=b["low"],
                    close=b["close"], volume=b["volume"],
                ))
            db.session.commit()
            created.append({"regime": regime, "scenario_id": scenario.id,
                            "bars": len(bars), "tier": spec["difficulty_tier"],
                            "status": "created"})

    return jsonify({"status": "ok", "results": created})


@bp.route("/setup/migrate-completed-lessons", methods=["POST"])
def migrate_completed_lessons():
    provided_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("SETUP_KEY")
    if not expected_key or provided_key != expected_key:
        return jsonify({"error": "unauthorized"}), 401

    from sqlalchemy import text
    try:
        db.session.execute(text(
            "ALTER TABLE user_progress ADD COLUMN IF NOT EXISTS completed_lessons VARCHAR[] DEFAULT '{}'"
        ))
        db.session.commit()
        return jsonify({"status": "ok", "message": "completed_lessons column added"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500
