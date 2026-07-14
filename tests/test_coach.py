"""Replay analytics + rule-based coach tests (Phase C).

    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_coach.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar

app = create_app()
client = app.test_client()


def _scenario(bars):
    with app.app_context():
        s = Scenario(name_internal="cch", asset_class="crypto", timeframe="1h",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate(bars):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


def _start(sid, user="cch"):
    return client.post(f"/scenarios/{sid}/start", json={"user_id": user}).get_json()["session_id"]


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_mae_mfe_and_r():
    # long open at bar0 (~100.05); while open, high tops 105, low bottoms 98
    bars = [(100, 101, 99, 100), (100, 105, 98, 100), (100, 101, 99, 100)]
    s = _start(_scenario(bars))
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0,
                          "stop_loss": 99, "take_profit": 104}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    client.post(f"/sessions/{s}/end")
    rp = client.get(f"/sessions/{s}/replay").get_json()
    tr = rp["trades"][0]
    check("MFE ~ 4.95 (high 105 - entry)", 4.8 <= tr["mfe"] <= 5.05)
    check("MAE ~ 2.05 (entry - low 98)", 1.9 <= tr["mae"] <= 2.15)
    check("planned R present", tr["planned_r"] is not None)
    check("achieved R present", tr["achieved_r"] is not None)
    check("equity curve present", len(rp["equity_curve"]) >= 2)
    check("markers present (entry+exit)", len(rp["markers"]) >= 2)


def test_coach_flags_no_stop():
    bars = [(100, 101, 99, 100)] * 4
    s = _start(_scenario(bars))
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0}).get_json()  # no stop
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    client.post(f"/sessions/{s}/end")
    coach = client.get(f"/sessions/{s}/replay").get_json()["coach"]
    check("coach produced a finding", len(coach) >= 1)
    check("no-stop finding links to risk_basics",
          any(f["lesson_id"] == "risk_basics" for f in coach))
    check("findings carry severity + lesson",
          all("severity" in f and "lesson_id" in f for f in coach))


def test_coach_praises_clean_session():
    bars = [(100, 101, 99, 100), (101, 103, 100, 102), (103, 105, 102, 104)]
    s = _start(_scenario(bars))
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0,
                          "stop_loss": 99, "take_profit": 104}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    client.post(f"/sessions/{s}/end")
    coach = client.get(f"/sessions/{s}/replay").get_json()["coach"]
    # one clean, well-managed trade → either no warnings or a positive note
    check("no warnings on a clean disciplined trade",
          all(f["severity"] != "warn" for f in coach))


TESTS = ["test_mae_mfe_and_r", "test_coach_flags_no_stop", "test_coach_praises_clean_session"]

if __name__ == "__main__":
    failed = 0
    for name in TESTS:
        print(name)
        try:
            globals()[name]()
        except AssertionError:
            failed += 1
        except Exception as e:
            print(f"  ERROR {e}"); failed += 1
    print(f"\n{'ALL PASSED' if failed == 0 else str(failed) + ' FAILED'} ({len(TESTS)} tests)")
    sys.exit(1 if failed else 0)
