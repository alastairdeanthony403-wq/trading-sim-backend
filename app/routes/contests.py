"""Weekly async competitions (Phase G).

One fixed scenario per ISO week (same synthetic seed for everyone), scored by
the shared composite+discipline system, one scored attempt per user, leaderboard
resets each week. Contest sessions are anti-cheat: bars are revealed one at a
time server-side (see game.advance / game.get_bars).
"""
import random
import string
from datetime import date, datetime, timezone, timedelta
from flask import Blueprint, jsonify, request
from app import db
from app.models.scenario import Scenario, ScenarioBar
from app.models.session import Session
from app.models.competition import Contest, ContestEntry, League, LeagueMember
from app.synthetic import generate_series, REGIMES
from app.routes.game import _finalize_session

bp = Blueprint("contests", __name__)

CONTEST_INITIAL_BARS = 20   # warm-up bars visible at the start
CONTEST_BARS = 140


def _week_start(d=None):
    d = d or date.today()
    return d - timedelta(days=d.weekday())   # Monday of this week


def _ensure_current_contest():
    """Get (or lazily create) this week's contest. The scenario is synthetic and
    seeded deterministically from the week, so everyone gets the same market."""
    ws = _week_start()
    c = Contest.query.filter_by(week_start=ws).first()
    if c:
        return c
    seed = int(ws.strftime("%Y%m%d"))
    regime = REGIMES[seed % len(REGIMES)]
    bars = generate_series(regime=regime, n_bars=CONTEST_BARS, seed=seed)
    sc = Scenario(name_internal=f"contest_{ws.isoformat()}", asset_class="synthetic",
                  timeframe="1D", difficulty_tier=2,
                  tags=["contest", "synthetic", regime], is_active=True)
    db.session.add(sc)
    db.session.flush()
    for i, b in enumerate(bars):
        db.session.add(ScenarioBar(scenario_id=sc.id, bar_sequence=i,
                                   open=b["open"], high=b["high"], low=b["low"],
                                   close=b["close"], volume=b["volume"]))
    c = Contest(week_start=ws, scenario_id=sc.id, is_active=True,
                title=f"Weekly Challenge — {regime.replace('_', ' ').title()}")
    db.session.add(c)
    db.session.commit()
    return c


def _entry_view(e, rank=None):
    return {"rank": rank, "user_id": e.user_id, "display_name": e.display_name,
            "composite_score": round(e.composite_score, 2) if e.composite_score is not None else None,
            "discipline_score": round(e.discipline_score, 2) if e.discipline_score is not None else None,
            "achieved_at": e.created_at.isoformat() if e.created_at else None}


@bp.route("/contests/current", methods=["GET"])
def current_contest():
    c = _ensure_current_contest()
    user_id = request.args.get("user_id")
    your = None
    if user_id:
        e = ContestEntry.query.filter_by(contest_id=c.id, user_id=user_id).first()
        if e:
            your = _entry_view(e)
    bar_count = ScenarioBar.query.filter_by(scenario_id=c.scenario_id).count()
    return jsonify({
        "contest_id": c.id,
        "week_start": c.week_start.isoformat(),
        "title": c.title,
        "scenario_id": c.scenario_id,
        "bar_count": bar_count,
        "entry_count": ContestEntry.query.filter_by(contest_id=c.id).count(),
        "your_entry": your,
    })


@bp.route("/contests/current/start", methods=["POST"])
def start_contest_session():
    """Start a contest session. Guests may practise; only a scored SUBMIT needs a
    display name and is limited to one per user."""
    c = _ensure_current_contest()
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "anonymous")

    session = Session(user_id=user_id, scenario_id=c.scenario_id,
                      starting_balance=body.get("starting_balance", 10000.0),
                      status="in_progress", is_contest=True,
                      bars_served=min(CONTEST_INITIAL_BARS, CONTEST_BARS - 1))
    db.session.add(session)
    db.session.commit()

    already = ContestEntry.query.filter_by(contest_id=c.id, user_id=user_id).first()
    return jsonify({"session_id": session.id, "contest_id": c.id,
                    "scenario_id": c.scenario_id, "bars_served": session.bars_served,
                    "already_entered": already is not None})


@bp.route("/contests/<int:contest_id>/submit", methods=["POST"])
def submit_contest(contest_id):
    """Finalise a contest session into a scored entry. Requires a display name;
    one scored attempt per user (re-submitting returns the existing entry)."""
    contest = Contest.query.get_or_404(contest_id)
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")
    user_id = body.get("user_id", "anonymous")
    display_name = (body.get("display_name") or "").strip()

    if not display_name:
        return jsonify({"error": "A display name is required to enter the contest."}), 400

    session = Session.query.get_or_404(int(session_id))
    if session.scenario_id != contest.scenario_id or not session.is_contest:
        return jsonify({"error": "That session is not part of this contest."}), 400

    # Flatten anything still open at the last revealed bar, so the score reflects
    # the actual run (mirrors the normal end-of-session flatten on the client).
    if session.status == "in_progress":
        from app.routes.game import _settle_trade, _cost_model
        last = (ScenarioBar.query
                .filter_by(scenario_id=session.scenario_id, bar_sequence=session.bars_served or 0)
                .first())
        if last:
            slip = _cost_model(session)["slippage_pct"]
            for t in session.trades:
                if t.status == "open":
                    _settle_trade(t, last.bar_sequence, last.close, "manual", slip)
                elif t.status == "pending":
                    db.session.delete(t)
            db.session.commit()

    result = _finalize_session(session)

    existing = ContestEntry.query.filter_by(contest_id=contest.id, user_id=user_id).first()
    if existing:
        # one scored attempt — keep the first, report the rank
        return jsonify({"already_entered": True, "entry": _entry_view(existing),
                        "rank": _rank_of(contest.id, existing), "result": result})

    entry = ContestEntry(contest_id=contest.id, user_id=user_id,
                         display_name=display_name[:60], session_id=session.id,
                         composite_score=result["score_composite"],
                         discipline_score=result["discipline"]["discipline_score"])
    db.session.add(entry)
    db.session.commit()
    return jsonify({"already_entered": False, "entry": _entry_view(entry),
                    "rank": _rank_of(contest.id, entry), "result": result})


def _rank_of(contest_id, entry):
    better = (ContestEntry.query.filter_by(contest_id=contest_id)
              .filter(ContestEntry.composite_score > (entry.composite_score or 0)).count())
    return better + 1


@bp.route("/contests/<int:contest_id>/leaderboard", methods=["GET"])
def contest_leaderboard(contest_id):
    Contest.query.get_or_404(contest_id)
    entries = (ContestEntry.query.filter_by(contest_id=contest_id)
               .order_by(ContestEntry.composite_score.desc().nullslast())
               .limit(50).all())
    return jsonify([_entry_view(e, i + 1) for i, e in enumerate(entries)])


# ── Private leagues (Phase G step 2) ──────────────────────────────────────

def _new_invite_code():
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = "".join(random.choice(alphabet) for _ in range(6))
        if not League.query.filter_by(invite_code=code).first():
            return code
    # extremely unlikely fallback
    return "".join(random.choice(alphabet) for _ in range(8))


def _league_view(league):
    return {"league_id": league.id, "name": league.name,
            "invite_code": league.invite_code, "owner_user_id": league.owner_user_id,
            "member_count": LeagueMember.query.filter_by(league_id=league.id).count()}


@bp.route("/leagues", methods=["POST"])
def create_league():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    user_id = body.get("user_id", "anonymous")
    display_name = (body.get("display_name") or "").strip()
    if not name or not display_name:
        return jsonify({"error": "A league name and your display name are required."}), 400

    league = League(name=name[:80], invite_code=_new_invite_code(), owner_user_id=user_id)
    db.session.add(league)
    db.session.flush()
    db.session.add(LeagueMember(league_id=league.id, user_id=user_id,
                                display_name=display_name[:60]))
    db.session.commit()
    return jsonify(_league_view(league))


@bp.route("/leagues/join", methods=["POST"])
def join_league():
    body = request.get_json(force=True) or {}
    code = (body.get("invite_code") or "").strip().upper()
    user_id = body.get("user_id", "anonymous")
    display_name = (body.get("display_name") or "").strip()
    if not display_name:
        return jsonify({"error": "A display name is required to join."}), 400

    league = League.query.filter_by(invite_code=code).first()
    if not league:
        return jsonify({"error": "No league found for that invite code."}), 404

    member = LeagueMember.query.filter_by(league_id=league.id, user_id=user_id).first()
    if not member:
        db.session.add(LeagueMember(league_id=league.id, user_id=user_id,
                                    display_name=display_name[:60]))
        db.session.commit()
    return jsonify(_league_view(league))


@bp.route("/leagues/mine", methods=["GET"])
def my_leagues():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify([])
    memberships = LeagueMember.query.filter_by(user_id=user_id).all()
    leagues = [League.query.get(m.league_id) for m in memberships]
    return jsonify([_league_view(l) for l in leagues if l])


@bp.route("/leagues/<int:league_id>/leaderboard", methods=["GET"])
def league_leaderboard(league_id):
    """Aggregate each member's weekly contest results into a season table."""
    League.query.get_or_404(league_id)
    members = LeagueMember.query.filter_by(league_id=league_id).all()
    rows = []
    for m in members:
        entries = ContestEntry.query.filter_by(user_id=m.user_id).all()
        played = len(entries)
        total = sum((e.composite_score or 0.0) for e in entries)
        best = max((e.composite_score or 0.0) for e in entries) if entries else 0.0
        rows.append({"user_id": m.user_id, "display_name": m.display_name,
                     "contests_played": played, "total_score": round(total, 2),
                     "best_score": round(best, 2)})
    rows.sort(key=lambda r: r["total_score"], reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return jsonify(rows)
