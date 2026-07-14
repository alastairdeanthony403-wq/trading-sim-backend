import os
import hashlib
from datetime import date, timedelta
from flask import Blueprint, jsonify, request
from app import db
from app.models.mission import Mission, MissionAttempt
from app.models.session import Session
from app.rules import evaluate_discipline, session_context, check_mission_rules

bp = Blueprint("missions", __name__)


def _mission_dict(m):
    return {
        "id": m.id, "slug": m.slug, "title": m.title, "brief": m.brief,
        "scenario_id": m.scenario_id, "difficulty_tier": m.difficulty_tier,
        "xp_reward": m.xp_reward, "rules": m.rules,
    }


def _today():
    return date.today().isoformat()


def _daily_mission():
    pool = (Mission.query
            .filter_by(is_active=True, is_daily_pool=True)
            .order_by(Mission.id).all())
    if not pool:
        return None
    idx = int(hashlib.sha256(_today().encode()).hexdigest(), 16) % len(pool)
    return pool[idx]


def _daily_streak(user_id):
    """Consecutive days (ending today or yesterday) with a passed daily attempt."""
    rows = (db.session.query(MissionAttempt.challenge_date)
            .filter(MissionAttempt.user_id == user_id,
                    MissionAttempt.is_daily.is_(True),
                    MissionAttempt.passed.is_(True))
            .distinct().all())
    days = {r[0] for r in rows if r[0]}
    if not days:
        return 0
    streak = 0
    cur = date.today()
    if cur.isoformat() not in days:          # allow the streak to still count through yesterday
        cur = cur - timedelta(days=1)
        if cur.isoformat() not in days:
            return 0
    while cur.isoformat() in days:
        streak += 1
        cur = cur - timedelta(days=1)
    return streak


@bp.route("/missions", methods=["GET"])
def list_missions():
    missions = Mission.query.filter_by(is_active=True).order_by(Mission.difficulty_tier, Mission.id).all()
    return jsonify([_mission_dict(m) for m in missions])


@bp.route("/missions/daily", methods=["GET"])
def daily_mission():
    m = _daily_mission()
    user_id = request.args.get("user_id", "guest")
    if not m:
        return jsonify({"mission": None, "date": _today(), "streak": _daily_streak(user_id)})
    return jsonify({"mission": _mission_dict(m), "date": _today(), "streak": _daily_streak(user_id)})


@bp.route("/missions/daily/leaderboard", methods=["GET"])
def daily_leaderboard():
    rows = (MissionAttempt.query
            .filter_by(is_daily=True, challenge_date=_today(), passed=True)
            .order_by(MissionAttempt.composite_score.desc().nullslast())
            .limit(10).all())
    return jsonify([
        {"rank": i + 1, "user_id": a.user_id, "composite_score": a.composite_score}
        for i, a in enumerate(rows)
    ])


@bp.route("/sessions/<int:session_id>/mission/<int:mission_id>/status", methods=["GET"])
def mission_status(session_id, mission_id):
    """Live evaluation for the rules HUD (works on an in-progress session)."""
    session = Session.query.get_or_404(session_id)
    mission = Mission.query.get_or_404(mission_id)
    disc = evaluate_discipline(session)
    ctx = session_context(session, disc)
    passed, results = check_mission_rules(mission.rules, ctx)
    return jsonify({"passed": passed, "results": results, "blown": ctx["blown"]})


@bp.route("/missions/<int:mission_id>/submit", methods=["POST"])
def submit_mission(mission_id):
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    user_id = body.get("user_id", "guest")
    is_daily = bool(body.get("is_daily"))

    mission = Mission.query.get_or_404(mission_id)
    session = Session.query.get_or_404(session_id)
    disc = evaluate_discipline(session)
    ctx = session_context(session, disc)
    passed, results = check_mission_rules(mission.rules, ctx)
    composite = session.score.score_composite if session.score else None

    attempt = MissionAttempt(
        mission_id=mission.id, session_id=session.id, user_id=user_id,
        passed=passed, violations=[r["label"] for r in results if not r["passed"]],
        composite_score=composite, is_daily=is_daily,
        challenge_date=_today() if is_daily else None,
    )
    db.session.add(attempt)
    db.session.commit()

    return jsonify({
        "passed": passed,
        "results": results,
        "xp_awarded": mission.xp_reward if passed else 0,
        "mission": _mission_dict(mission),
    })


# ── Seed (protected) ───────────────────────────────────────────────────────
SEED_MISSIONS = [
    {"slug": "first-stops", "title": "Always Have a Stop", "difficulty_tier": 1, "xp_reward": 40,
     "brief": "Close the session in profit with a stop-loss on every trade.",
     "daily": True,
     "rules": [
         {"type": "require_stop_on_all", "label": "A stop-loss on every trade"},
         {"type": "min_return_pct", "param": 0.0, "label": "Finish at break-even or better"},
     ]},
    {"slug": "small-risk", "title": "Risk Discipline", "difficulty_tier": 1, "xp_reward": 50,
     "brief": "Never risk more than 2% of your equity on a single trade.",
     "daily": True,
     "rules": [
         {"type": "max_risk_pct_per_trade", "param": 2.0, "label": "≤ 2% risk on any trade"},
         {"type": "require_stop_on_all", "label": "A stop on every trade"},
     ]},
    {"slug": "survive-drawdown", "title": "Protect the Downside", "difficulty_tier": 2, "xp_reward": 60,
     "brief": "Keep your max drawdown under 10% for the whole session.",
     "daily": True,
     "rules": [{"type": "max_drawdown_pct", "param": 10.0, "label": "Max drawdown under 10%"}]},
    {"slug": "no-revenge", "title": "Stay Cool", "difficulty_tier": 2, "xp_reward": 60,
     "brief": "No revenge trades — don't size up right after a stop-out.",
     "daily": True,
     "rules": [{"type": "no_revenge", "label": "No revenge trades"}]},
    {"slug": "green-and-controlled", "title": "Green and Controlled", "difficulty_tier": 2, "xp_reward": 70,
     "brief": "Finish at least +3% while risking ≤ 2% per trade.",
     "daily": True,
     "rules": [
         {"type": "min_return_pct", "param": 3.0, "label": "Finish ≥ +3%"},
         {"type": "max_risk_pct_per_trade", "param": 2.0, "label": "≤ 2% risk on any trade"},
     ]},
    {"slug": "patience", "title": "Patience Pays", "difficulty_tier": 2, "xp_reward": 55,
     "brief": "Take no more than 5 trades and still finish in profit.",
     "daily": True,
     "rules": [
         {"type": "max_trades", "param": 5, "label": "No more than 5 trades"},
         {"type": "min_return_pct", "param": 0.0, "label": "Finish in profit"},
     ]},
    {"slug": "tight-and-green", "title": "Tight and Green", "difficulty_tier": 3, "xp_reward": 80,
     "brief": "+5% with drawdown under 8% and a stop on every trade.",
     "daily": True,
     "rules": [
         {"type": "min_return_pct", "param": 5.0, "label": "Finish ≥ +5%"},
         {"type": "max_drawdown_pct", "param": 8.0, "label": "Max drawdown under 8%"},
         {"type": "require_stop_on_all", "label": "A stop on every trade"},
     ]},
    {"slug": "one-percent-club", "title": "The 1% Club", "difficulty_tier": 3, "xp_reward": 90,
     "brief": "Risk ≤ 1% per trade and still finish green.",
     "daily": True,
     "rules": [
         {"type": "max_risk_pct_per_trade", "param": 1.0, "label": "≤ 1% risk on any trade"},
         {"type": "min_return_pct", "param": 0.0, "label": "Finish in profit"},
     ]},
    {"slug": "active-but-safe", "title": "Active but Safe", "difficulty_tier": 3, "xp_reward": 75,
     "brief": "Take at least 6 trades, keep drawdown under 12%, no revenge.",
     "daily": True,
     "rules": [
         {"type": "min_trades", "param": 6, "label": "At least 6 trades"},
         {"type": "max_drawdown_pct", "param": 12.0, "label": "Max drawdown under 12%"},
         {"type": "no_revenge", "label": "No revenge trades"},
     ]},
    {"slug": "textbook", "title": "Textbook Session", "difficulty_tier": 4, "xp_reward": 120,
     "brief": "+5%, ≤1% risk per trade, drawdown under 6%, no revenge, stop on every trade.",
     "daily": True,
     "rules": [
         {"type": "min_return_pct", "param": 5.0, "label": "Finish ≥ +5%"},
         {"type": "max_risk_pct_per_trade", "param": 1.0, "label": "≤ 1% risk on any trade"},
         {"type": "max_drawdown_pct", "param": 6.0, "label": "Max drawdown under 6%"},
         {"type": "no_revenge", "label": "No revenge trades"},
         {"type": "require_stop_on_all", "label": "A stop on every trade"},
     ]},
]


@bp.route("/setup/seed-missions", methods=["POST"])
def seed_missions():
    if not os.environ.get("SETUP_KEY") or request.headers.get("X-Setup-Key") != os.environ.get("SETUP_KEY"):
        return jsonify({"error": "unauthorized"}), 401
    created, updated = 0, 0
    for spec in SEED_MISSIONS:
        m = Mission.query.filter_by(slug=spec["slug"]).first()
        if m is None:
            m = Mission(slug=spec["slug"])
            created += 1
        else:
            updated += 1
        m.title = spec["title"]
        m.brief = spec["brief"]
        m.difficulty_tier = spec["difficulty_tier"]
        m.xp_reward = spec["xp_reward"]
        m.rules = spec["rules"]
        m.is_active = True
        m.is_daily_pool = spec.get("daily", False)
        db.session.add(m)
    db.session.commit()
    return jsonify({"status": "ok", "created": created, "updated": updated,
                    "total": Mission.query.count()})
