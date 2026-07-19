"""Rule 0 — pre-playback history window (Phase 2 slice 1).

Requires a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_rule0.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_generation_sets_history_and_length():
    r = client.post("/setup/generate-scenarios",
                    json={"regimes": ["trend_up"], "per_regime": 1,
                          "history_bars": 300, "playback_bars": 160},
                    headers={"X-Setup-Key": os.environ.get("SETUP_KEY", "")}).get_json()
    row = r["results"][0]
    check("scenario reports its history window", row["history_bars"] == 300)
    check("total = history + playback", row["bars"] == 460)
    with app.app_context():
        sc = Scenario.query.get(row["scenario_id"])
        check("history_bars persisted on the scenario", sc.history_bars == 300)
    return row["scenario_id"]


def test_start_session_returns_history_window():
    sid = test_generation_sets_history_and_length()
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "r0"}).get_json()
    check("start_session exposes a 300-bar history window", s["history_bars"] == 300)
    # the client can load that many bars immediately (pre-playback context)
    bars = client.get(f"/sessions/{s['session_id']}/bars?up_to=299").get_json()
    check("300 history bars are available on load", len(bars) == 300)


def test_legacy_scenario_falls_back_to_small_window():
    # A scenario with no history_bars (e.g. real-market) keeps the small window.
    with app.app_context():
        sc = Scenario(name_internal="legacy", asset_class="equity", timeframe="1D",
                      difficulty_tier=1, tags=["historical"], is_active=True)
        db.session.add(sc); db.session.flush()
        for i in range(100):
            db.session.add(ScenarioBar(scenario_id=sc.id, bar_sequence=i,
                                       open=10, high=11, low=9, close=10, volume=1))
        db.session.commit()
        sid = sc.id
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "r0"}).get_json()
    check("legacy scenario uses the small fallback window", s["history_bars"] == 30)


def test_contest_reveals_after_300_history():
    s = client.post("/contests/current/start", json={"user_id": "r0c"}).get_json()
    check("contest starts with 300 bars of history served", s["bars_served"] == 300)
    # anti-cheat still holds: cannot see beyond the served window
    bars = client.get(f"/sessions/{s['session_id']}/bars?up_to=100000").get_json()
    check("contest bars are capped at the served window", len(bars) == 301)


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
