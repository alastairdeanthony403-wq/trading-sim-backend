"""Setup quality grading A/B/C (Phase 5).

Grades are earned by confluence at entry (defined risk + trend + reward:risk +
location + trigger), derived server-side — a bad setup that happened to win is
still a C. These tests check the grader on crafted series and the replay/coach
integration.

Requires a Postgres DATABASE_URL + SETUP_KEY:
    DATABASE_URL=postgresql://.../db SETUP_KEY=testkey python tests/test_setups.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, bar_provider
from app import setups
from app.bar_provider import Bar
from app.coach import build_findings

app = create_app()
client = app.test_client()
KEY = {"X-Setup-Key": os.environ.get("SETUP_KEY", "")}


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _uptrend(n=60, step=0.3):
    return [Bar(i, 100 + i * step, 100 + i * step + 0.2, 100 + i * step - 0.2,
               100 + i * step, 1000) for i in range(n)]


def _trade(direction, entry, idx, stop=None, planned_r=None):
    return {"direction": direction, "entry_price": entry, "bar_entered": idx,
            "stop_loss": stop, "planned_r": planned_r}


def test_a_grade_with_trend_stop_and_rr():
    s = _uptrend()
    t = _trade("long", s[40].close, 40, stop=s[40].close - 1.0, planned_r=2.0)
    g = setups.grade_trade(t, s, {"levels": [], "sweeps": []})
    check("with-trend, stopped, 2R long grades A", g["grade"] == "A")
    check("trend alignment counted", any(f["name"] == "Trend alignment" and f["met"] for f in g["factors"]))


def test_counter_trend_no_stop_is_c():
    s = _uptrend()
    t = _trade("short", s[40].close, 40, stop=None, planned_r=None)
    g = setups.grade_trade(t, s, {"levels": [], "sweeps": []})
    check("counter-trend, no-stop short grades C", g["grade"] == "C")


def test_no_stop_never_grades_a():
    s = _uptrend()
    # with-trend + great R:R but NO stop → capped below A
    t = _trade("long", s[40].close, 40, stop=None, planned_r=3.0)
    g = setups.grade_trade(t, s, {"levels": [], "sweeps": []})
    check("no stop can never be an A setup", g["grade"] != "A")


def test_sweep_trigger_boosts_grade():
    s = _uptrend()
    entry_idx = 41
    structure = {"levels": [], "sweeps": [{"bar_sequence": 40, "price": s[40].low,
                                           "side": "low", "penetration": 0.3}]}
    t = _trade("long", s[entry_idx].close, entry_idx, stop=s[entry_idx].close - 1.0, planned_r=1.5)
    g = setups.grade_trade(t, s, structure)
    check("a favourable sweep just before entry is credited",
          any(f["name"] == "Trigger" and f["met"] for f in g["factors"]))
    check("the trigger lifts the setup to A", g["grade"] == "A")


def test_location_into_resistance_penalized():
    s = _uptrend()
    entry = s[40].close
    structure = {"levels": [{"price": entry * 1.002, "kind": "resistance",
                             "touches": 3, "first_bar": 5, "last_bar": 30}], "sweeps": []}
    t = _trade("long", entry, 40, stop=entry - 1.0, planned_r=2.0)
    g = setups.grade_trade(t, s, structure)
    check("entering into resistance is flagged",
          any(f["name"] == "Location" and f["met"] is False for f in g["factors"]))


def test_replay_grades_each_trade():
    r = client.post("/setup/generate-scenarios",
                    json={"regimes": ["trend_up"], "per_regime": 1, "seed": 8181,
                          "history_bars": 60, "playback_bars": 40}, headers=KEY).get_json()
    sid = r["results"][0]["scenario_id"]
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "sg"}).get_json()
    sess = s["session_id"]
    entry_bar = s["history_bars"] - 1
    price = client.get(f"/sessions/{sess}/bars").get_json()[entry_bar]["close"]
    client.post(f"/sessions/{sess}/trades", json={
        "direction": "long", "size": 10, "bar_sequence": entry_bar,
        "stop_loss": round(price * 0.98, 2), "take_profit": round(price * 1.06, 2)})
    client.post(f"/sessions/{sess}/advance", json={"bar_sequence": entry_bar + 39})
    client.post(f"/sessions/{sess}/end")
    replay = client.get(f"/sessions/{sess}/replay").get_json()
    check("replay carries a setup-grade summary", "setup_grades" in replay)
    if replay["trades"]:
        g = replay["trades"][0].get("setup")
        check("each trade is graded A/B/C", g and g["grade"] in ("A", "B", "C"))
        check("the grade lists its factors", g and len(g["factors"]) >= 3)


def test_coach_flags_low_probability_setups():
    replay = {
        "trades": [
            {"direction": "short", "setup": {"grade": "C", "score": -2, "factors": []},
             "pnl": -10, "achieved_r": -1.0, "planned_r": None, "exit_reason": "manual"},
            {"direction": "short", "setup": {"grade": "C", "score": -1, "factors": []},
             "pnl": -5, "achieved_r": -0.5, "planned_r": None, "exit_reason": "manual"},
        ],
    }
    disc = {"no_stop_count": 0, "revenge_count": 0, "oversize_count": 0,
            "discipline_score": 60, "trades_total": 2}
    findings = build_findings(None, disc, replay)
    check("a setup-quality note links the confluence lesson",
          any(f["lesson_id"] == "advanced_confluence" for f in findings))


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
