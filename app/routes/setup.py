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

    from app.models.scenario import Scenario
    from app.synthetic import make_scenario_spec, REGIMES
    from app.engine import CURRENT_ENGINE

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
            spec = make_scenario_spec(regime, seed)
            # Seed-only: no bars persisted — stored seed + version regenerate them.
            scenario = Scenario(
                name_internal=spec["name_internal"],
                asset_class=asset_class,
                timeframe="1D",
                difficulty_tier=spec["difficulty_tier"],
                tags=spec["tags"],
                is_active=True,
                history_bars=history_bars,
                engine_version=CURRENT_ENGINE, seed=seed,
                gen_params={"kind": "regime", "n_bars": n_bars, "regime": regime},
            )
            db.session.add(scenario)
            db.session.commit()
            created.append({"regime": regime, "scenario_id": scenario.id,
                            "bars": n_bars, "history_bars": history_bars,
                            "tier": spec["difficulty_tier"], "status": "created"})

    return jsonify({"status": "ok", "results": created})


@bp.route("/setup/generate-intraday-scenarios", methods=["POST"])
def generate_intraday_scenarios():
    """Mint multi-timeframe INTRADAY scenarios (Phase 2). The stored series is
    1-minute bars; the chart can switch between 1m/5m/15m/30m/1h/4h (aggregated
    on read). Seed-only — no bars persisted. Body (all optional):
        {"regimes": ["trend_up","range","high_vol"],  # one scenario per regime*per_regime
         "per_regime": 1,
         "days": 7,                 # ~trading days of 1-minute data
         "bars_per_day": 390,       # minutes per session (390 ≈ a US equity day)
         "anchor_tf": "15m",        # timeframe the chart opens on
         "history_candles": 80,     # Rule-0 pre-playback history, in anchor_tf candles
         "asset_class": "synthetic",
         "seed": 12345}
    """
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    from app.models.scenario import Scenario
    from app.synthetic import make_scenario_spec, REGIMES
    from app.engine import CURRENT_ENGINE
    from app.bar_provider import TF_MINUTES

    body = request.get_json(silent=True) or {}
    regimes = body.get("regimes") or ["trend_up", "range", "high_vol"]
    per_regime = int(body.get("per_regime", 1))
    days = int(body.get("days", 7))
    bars_per_day = int(body.get("bars_per_day", 390))
    anchor_tf = body.get("anchor_tf", "15m")
    anchor_mult = TF_MINUTES.get(anchor_tf, 15)
    asset_class = body.get("asset_class", "synthetic")
    available = ["1m", "5m", "15m", "30m", "1h", "4h"]

    n_bars = days * bars_per_day
    # Rule 0 anchored on the anchor timeframe: history_candles anchor-TF candles,
    # expressed in BASE (1m) units and kept an exact multiple so the first anchor
    # candle is complete. Clamp to leave room for playback.
    history_candles = int(body.get("history_candles", 80))
    history_bars = min(history_candles * anchor_mult, n_bars - anchor_mult)
    history_bars = max(anchor_mult, history_bars)

    base_seed = body.get("seed")
    base = int(base_seed) if base_seed is not None else random.randint(1, 10 ** 9)

    created = []
    for regime in regimes:
        if regime not in REGIMES:
            created.append({"regime": regime, "status": "skipped", "detail": "unknown regime"})
            continue
        for k in range(per_regime):
            seed = base + hash(regime) % 100000 + k
            spec = make_scenario_spec(regime, seed)
            scenario = Scenario(
                name_internal=f"intraday_{regime}_{seed}",
                asset_class=asset_class,
                timeframe=anchor_tf,                 # display/label unit
                base_timeframe="1m",
                available_timeframes=available,
                difficulty_tier=spec["difficulty_tier"],
                tags=["synthetic", "intraday", regime],
                is_active=True,
                history_bars=history_bars,
                engine_version=CURRENT_ENGINE, seed=seed,
                gen_params={"kind": "intraday", "n_bars": n_bars, "regime": regime,
                            "days": days, "bars_per_day": bars_per_day,
                            "vol_scale": 0.15, "anchor_tf": anchor_tf},
            )
            db.session.add(scenario)
            db.session.commit()
            created.append({"regime": regime, "scenario_id": scenario.id,
                            "bars_1m": n_bars, "history_bars": history_bars,
                            "anchor_tf": anchor_tf, "timeframes": available,
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

    from app.models.scenario import Scenario
    from app.models.event import ScenarioEvent
    from app.synthetic import build_news_scenario
    from app.engine import CURRENT_ENGINE

    body = request.get_json(silent=True) or {}
    count = int(body.get("count", 3))
    n_bars = int(body.get("n_bars", 140))
    asset_class = body.get("asset_class", "synthetic")
    base_seed = body.get("seed")
    base = int(base_seed) if base_seed is not None else random.randint(1, 10 ** 9)

    created = []
    for k in range(count):
        seed = base + k
        # Events are deterministic from the seed; bars regenerate identically on
        # read, so persist the events (for headlines) but no bars.
        _bars, events = build_news_scenario(seed=seed, n_bars=n_bars)
        scenario = Scenario(
            name_internal=f"scenario_mode_{seed}",
            asset_class=asset_class,
            timeframe="1D",
            difficulty_tier=2,
            tags=["synthetic", "scenario_mode", "news"],
            is_active=True,
            engine_version=CURRENT_ENGINE, seed=seed,
            gen_params={"kind": "news", "n_bars": n_bars},
        )
        db.session.add(scenario)
        db.session.flush()
        for e in events:
            db.session.add(ScenarioEvent(
                scenario_id=scenario.id, bar_sequence=e["bar"],
                category=e["category"], headline=e["headline"],
                detail=e["detail"], sentiment=e["sentiment"], impact=e["impact"],
            ))
        db.session.commit()
        created.append({"scenario_id": scenario.id, "bars": n_bars,
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

    from app.models.scenario import Scenario
    from app.models.event import ScenarioEvent
    from app.synthetic import build_scam_scenario
    from app.engine import CURRENT_ENGINE

    body = request.get_json(silent=True) or {}
    count = int(body.get("count", 2))
    n_bars = int(body.get("n_bars", 120))
    asset_class = body.get("asset_class", "crypto")
    base_seed = body.get("seed")
    base = int(base_seed) if base_seed is not None else random.randint(1, 10 ** 9)

    created = []
    for k in range(count):
        seed = base + k
        _bars, events = build_scam_scenario(seed=seed, n_bars=n_bars)
        scenario = Scenario(
            name_internal=f"scam_{seed}",
            asset_class=asset_class,
            timeframe="1D",
            difficulty_tier=3,
            tags=["synthetic", "scam", "scenario_mode"],
            is_active=True,
            engine_version=CURRENT_ENGINE, seed=seed,
            gen_params={"kind": "scam", "n_bars": n_bars},
        )
        db.session.add(scenario)
        db.session.flush()
        for e in events:
            db.session.add(ScenarioEvent(
                scenario_id=scenario.id, bar_sequence=e["bar"],
                category=e["category"], headline=e["headline"],
                detail=e["detail"], sentiment=e["sentiment"], impact=e["impact"],
            ))
        db.session.commit()
        created.append({"scenario_id": scenario.id, "bars": n_bars,
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
