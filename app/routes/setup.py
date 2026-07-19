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
    # Rule 0: ≥300 bars of pre-playback history, then bars to trade through.
    history_bars = int(body.get("history_bars", 300))
    playback_bars = int(body.get("playback_bars", 160))
    n_bars = history_bars + playback_bars
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
                history_bars=history_bars,
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
                            "bars": len(bars), "history_bars": history_bars,
                            "tier": spec["difficulty_tier"], "status": "created"})

    return jsonify({"status": "ok", "results": created})


@bp.route("/setup/generate-news-scenarios", methods=["POST"])
def generate_news_scenarios():
    """Mint 'Scenario Mode' scenarios with scripted news events baked into the
    price (Phase E step 2). Body (all optional):
        {"count": 3, "n_bars": 140, "asset_class": "synthetic", "seed": 123}
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    from app.models.scenario import Scenario, ScenarioBar
    from app.models.event import ScenarioEvent
    from app.synthetic import build_news_scenario

    body = request.get_json(silent=True) or {}
    count = int(body.get("count", 3))
    n_bars = int(body.get("n_bars", 140))
    asset_class = body.get("asset_class", "synthetic")
    base_seed = body.get("seed")
    base = int(base_seed) if base_seed is not None else random.randint(1, 10 ** 9)

    created = []
    for k in range(count):
        seed = base + k
        bars, events = build_news_scenario(seed=seed, n_bars=n_bars)
        scenario = Scenario(
            name_internal=f"scenario_mode_{seed}",
            asset_class=asset_class,
            timeframe="1D",
            difficulty_tier=2,
            tags=["synthetic", "scenario_mode", "news"],
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
        for e in events:
            db.session.add(ScenarioEvent(
                scenario_id=scenario.id, bar_sequence=e["bar"],
                category=e["category"], headline=e["headline"],
                detail=e["detail"], sentiment=e["sentiment"], impact=e["impact"],
            ))
        db.session.commit()
        created.append({"scenario_id": scenario.id, "bars": len(bars),
                        "events": len(events), "status": "created"})

    return jsonify({"status": "ok", "results": created})


@bp.route("/setup/generate-scam-scenarios", methods=["POST"])
def generate_scam_scenarios():
    """Mint pump-and-dump scenarios with an escalating hype feed and a rug
    (Phase E step 3). Body (all optional):
        {"count": 2, "n_bars": 120, "asset_class": "crypto", "seed": 77}
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    from app.models.scenario import Scenario, ScenarioBar
    from app.models.event import ScenarioEvent
    from app.synthetic import build_scam_scenario

    body = request.get_json(silent=True) or {}
    count = int(body.get("count", 2))
    n_bars = int(body.get("n_bars", 120))
    asset_class = body.get("asset_class", "crypto")
    base_seed = body.get("seed")
    base = int(base_seed) if base_seed is not None else random.randint(1, 10 ** 9)

    created = []
    for k in range(count):
        seed = base + k
        bars, events = build_scam_scenario(seed=seed, n_bars=n_bars)
        scenario = Scenario(
            name_internal=f"scam_{seed}",
            asset_class=asset_class,
            timeframe="1D",
            difficulty_tier=3,
            tags=["synthetic", "scam", "scenario_mode"],
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
        for e in events:
            db.session.add(ScenarioEvent(
                scenario_id=scenario.id, bar_sequence=e["bar"],
                category=e["category"], headline=e["headline"],
                detail=e["detail"], sentiment=e["sentiment"], impact=e["impact"],
            ))
        db.session.commit()
        created.append({"scenario_id": scenario.id, "bars": len(bars),
                        "events": len(events), "status": "created"})

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
