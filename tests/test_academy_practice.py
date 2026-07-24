"""Academy × engine merge — concept-tagged practice checks (Phase 1).

A scenario_check mints a concept-matched market, reveals it incrementally under a
server cap, and grades server-side via the mission engine. These tests verify the
concept→regime match, the no-future-leak reveal, server-authoritative grading
(a faked client pass is rejected), and fresh-seed retries.

Requires a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../db python tests/test_academy_practice.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, bar_provider
from app import academy
from app.models.scenario import Scenario
from app.models.session import Session

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _start(check_id):
    return client.post("/academy/practice/start",
                       json={"user_id": "learner", "check_id": check_id}).get_json()


def test_start_returns_goal_rules_and_no_secrets():
    r = _start("check_reading")
    check("start returns a session + goal + rule set",
          "session_id" in r and r["goal"] and len(r["rules"]) >= 1)
    check("warm-up and live windows are returned",
          r["warmup_bars"] > 0 and r["live_bars"] > 0)
    check("NO seed is ever returned to the client", "seed" not in r)


def test_concept_matches_the_taught_regime():
    r = _start("check_reading")           # support_resistance → range
    with app.app_context():
        sc = Scenario.query.get(r["scenario_id"])
        spec = academy.spec_for("support_resistance")
        regime = next((t for t in sc.tags if t in ("range", "trend_up", "trend_down", "high_vol", "low_vol")), None)
        check("the minted scenario's regime is one the concept requires",
              regime in spec["regimes"])
        check("scenario is not listed in the public picker", sc.is_active is False)


def test_reveal_is_capped_no_future_leak():
    r = _start("check_risk")
    sid = r["session_id"]
    warmup = r["warmup_bars"]
    # ask for the whole future explicitly — must be capped to the warm-up window
    bars = client.get(f"/sessions/{sid}/bars?up_to=999999").get_json()
    check("only the warm-up block is served (no future bars leak)",
          all(b["bar_sequence"] < warmup for b in bars) and len(bars) == warmup)
    # advancing reveals exactly one more bar, server-authoritative
    client.post(f"/sessions/{sid}/advance", json={"bar_sequence": 999999})
    bars2 = client.get(f"/sessions/{sid}/bars?up_to=999999").get_json()
    check("advance reveals exactly one more bar", len(bars2) == warmup + 1)


def test_retry_serves_a_fresh_market():
    a = _start("check_structure")
    b = _start("check_structure")
    with app.app_context():
        sa = Scenario.query.get(a["scenario_id"])
        sb = Scenario.query.get(b["scenario_id"])
        check("a retry mints a new scenario", sa.id != sb.id)
        check("with a different seed (new market, not the same answer)", sa.seed != sb.seed)


def test_grading_is_server_side_disciplined_pass():
    r = _start("check_risk")              # rules: stop-on-all, max_risk 2%, no_revenge, min_trades 1
    sid = r["session_id"]
    entry = r["warmup_bars"] - 1
    price = client.get(f"/sessions/{sid}/bars").get_json()[entry]["close"]
    # a single trade WITH a tight stop → satisfies the rule set
    client.post(f"/sessions/{sid}/trades", json={
        "direction": "long", "size": 10, "bar_sequence": entry,
        "stop_loss": round(price * 0.99, 4)})
    client.post(f"/sessions/{sid}/advance", json={"bar_sequence": 999999})
    g = client.post(f"/academy/practice/{sid}/grade").get_json()
    check("a disciplined run passes", g["passed"] is True)
    check("grade reports the concept + per-rule results",
          g["concept"] == "risk_stops" and len(g["results"]) == len(r["rules"]))


def test_grading_rejects_a_no_stop_run():
    r = _start("check_risk")
    sid = r["session_id"]
    entry = r["warmup_bars"] - 1
    # a trade with NO stop → require_stop_on_all must fail server-side
    client.post(f"/sessions/{sid}/trades", json={
        "direction": "long", "size": 10, "bar_sequence": entry})
    client.post(f"/sessions/{sid}/advance", json={"bar_sequence": 999999})
    g = client.post(f"/academy/practice/{sid}/grade").get_json()
    check("a no-stop run fails, no matter what the client claims", g["passed"] is False)
    check("the failing rule is the stop requirement",
          any(x["type"] == "require_stop_on_all" and not x["passed"] for x in g["results"]))


def test_practice_does_not_post_to_a_leaderboard():
    from app.models.progress import Leaderboard
    r = _start("check_discipline")
    sid = r["session_id"]
    entry = r["warmup_bars"] - 1
    price = client.get(f"/sessions/{sid}/bars").get_json()[entry]["close"]
    client.post(f"/sessions/{sid}/trades", json={"direction": "long", "size": 10,
                "bar_sequence": entry, "stop_loss": round(price * 0.99, 4)})
    client.post(f"/sessions/{sid}/advance", json={"bar_sequence": 999999})
    client.post(f"/academy/practice/{sid}/grade")
    with app.app_context():
        sc = Session.query.get(sid)
        rows = Leaderboard.query.filter_by(scenario_id=sc.scenario_id).count()
        check("a practice scenario never posts to the leaderboard", rows == 0)


def test_all_curriculum_checks_map_to_a_valid_concept():
    from app.routes.progress import CURRICULUM
    ok = True
    fallbacks = []
    for unit in CURRICULUM:
        cid = unit["check"]
        concept = academy.concept_for_check(cid)
        spec = academy.spec_for(concept)
        if spec is None or not spec["regimes"]:
            ok = False
        if cid in academy.FALLBACK_CHECKS:
            fallbacks.append(cid)
    check("every unit check maps to a concept with a valid regime", ok)
    check("fallback checks are declared (logged for review)",
          set(fallbacks) == academy.FALLBACK_CHECKS)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        print(t.__name__)
        try:
            t()
        except AssertionError:
            failed += 1
        except Exception as e:
            print(f"  ERROR {e}"); failed += 1
    print(f"\n{'ALL PASSED' if failed == 0 else str(failed) + ' FAILED'} ({len(TESTS)} tests)")
    sys.exit(1 if failed else 0)
