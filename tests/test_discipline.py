"""Discipline scoring tests (Phase B).

    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_discipline.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar
from app.models.progress import UserProgress

app = create_app()
client = app.test_client()


def _scenario(bars):
    with app.app_context():
        s = Scenario(name_internal="d", asset_class="crypto", timeframe="1h",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate(bars):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


def _start(sid, user="disc"):
    return client.post(f"/scenarios/{sid}/start", json={"user_id": user}).get_json()["session_id"]


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


FLAT = [(100, 101, 99, 100)] * 6


def test_no_stop_is_penalised():
    s = _start(_scenario(FLAT))
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    d = client.post(f"/sessions/{s}/end").get_json()["discipline"]
    check("no-stop trade flagged", d["no_stop_count"] == 1)
    check("discipline below 100", d["discipline_score"] < 100)


def test_disciplined_trade_scores_full():
    s = _start(_scenario(FLAT))
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0,
                          "stop_loss": 99}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    d = client.post(f"/sessions/{s}/end").get_json()["discipline"]
    check("no violations", d["rule_violations"] == 0)
    check("discipline is 100", d["discipline_score"] == 100)


def test_oversize_is_penalised():
    s = _start(_scenario(FLAT))
    # SL far away → risk ~6% of balance (> 5% threshold)
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0,
                          "stop_loss": 40}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    d = client.post(f"/sessions/{s}/end").get_json()["discipline"]
    check("oversize flagged", d["oversize_count"] == 1)


def test_revenge_pattern_detected():
    # trade1 stops out at bar1; trade2 opened at bar2 (within 5 bars) at 2x size
    bars = [(100, 101, 99, 100), (100, 101, 98, 100), (100, 101, 99, 100), (100, 101, 99, 100)]
    s = _start(_scenario(bars))
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 10, "bar_sequence": 0, "stop_loss": 99})
    client.post(f"/sessions/{s}/advance", json={"bar_sequence": 1})   # trade1 stops out at bar1
    t2 = client.post(f"/sessions/{s}/trades",
                     json={"direction": "long", "size": 25, "bar_sequence": 2, "stop_loss": 98}).get_json()
    client.post(f"/trades/{t2['trade_id']}/close", json={"bar_sequence": 3})
    d = client.post(f"/sessions/{s}/end").get_json()["discipline"]
    check("revenge trade detected", d["revenge_count"] == 1)


def test_discipline_moves_composite():
    # identical closed trade, one with a stop (disciplined) one without.
    def run(with_stop):
        s = _start(_scenario(FLAT))
        body = {"direction": "long", "size": 10, "bar_sequence": 0}
        if with_stop:
            body["stop_loss"] = 99
        t = client.post(f"/sessions/{s}/trades", json=body).get_json()
        client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
        return client.post(f"/sessions/{s}/end").get_json()
    disciplined = run(True)
    reckless = run(False)
    check("same trade P&L both runs",
          abs(disciplined["total_return_pct"] - reckless["total_return_pct"]) < 1e-6)
    check("discipline raises composite",
          disciplined["score_composite"] > reckless["score_composite"])


def test_user_aggregates_accumulate():
    user = "agg_user"
    s = _start(_scenario(FLAT), user=user)
    t1 = client.post(f"/sessions/{s}/trades",
                     json={"direction": "long", "size": 10, "bar_sequence": 0, "stop_loss": 99}).get_json()
    t2 = client.post(f"/sessions/{s}/trades",
                     json={"direction": "short", "size": 5, "bar_sequence": 0}).get_json()
    client.post(f"/trades/{t1['trade_id']}/close", json={"bar_sequence": 2})
    client.post(f"/trades/{t2['trade_id']}/close", json={"bar_sequence": 2})
    client.post(f"/sessions/{s}/end")
    with app.app_context():
        p = UserProgress.query.filter_by(user_id=user).first()
        check("total trades counted", p.total_trades_all == 2)
        check("trades-with-stops counted", p.trades_with_stops_all == 1)
        check("sessions scored counted", p.sessions_scored == 1)


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
