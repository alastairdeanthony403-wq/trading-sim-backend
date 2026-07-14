"""Margin / leverage / liquidation / bankruptcy tests (Phase A).

Requires a Postgres DATABASE_URL. Run:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_margin.py
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
        s = Scenario(name_internal="m", asset_class="crypto", timeframe="1h",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate(bars):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


def _start(sid):
    return client.post(f"/scenarios/{sid}/start", json={"user_id": "t"}).get_json()["session_id"]


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_insufficient_margin_rejected():
    sid = _scenario([(100, 101, 99, 100), (100, 101, 99, 100)])
    s = _start(sid)
    # size 200 @ ~100 = notional ~20000, no leverage, balance 10000 → rejected
    r = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 200, "bar_sequence": 0})
    check("over-margin market order is rejected 400", r.status_code == 400)


def test_leverage_allows_larger_size():
    sid = _scenario([(100, 101, 99, 100), (100, 101, 99, 100)])
    s = _start(sid)
    r = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 200, "bar_sequence": 0, "leverage": 5})
    check("5x leverage admits the same size", r.status_code == 200 and r.get_json().get("status") == "open")


def test_liquidation_but_survives():
    # 10x, size 990 (~all margin). Price falls to 94 close → equity below the
    # maintenance level but not wiped → liquidated, account survives (not blown).
    sid = _scenario([(100, 101, 99, 100), (100, 100, 94, 94)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 990, "bar_sequence": 0, "leverage": 10})
    res = client.post(f"/sessions/{s}/advance", json={"bar_sequence": 1}).get_json()
    liq = [e for e in res["events"] if e["event"] == "liquidated"]
    check("position is liquidated", len(liq) == 1)
    check("account NOT blown (survived)", res["blown"] is False)
    check("status not blown", res["status"] != "blown")


def test_bankruptcy_blows_account():
    # Same leveraged position, but a hard drop to 88 close wipes equity < 0.
    sid = _scenario([(100, 101, 99, 100), (100, 100, 88, 88)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 990, "bar_sequence": 0, "leverage": 10})
    res = client.post(f"/sessions/{s}/advance", json={"bar_sequence": 1}).get_json()
    check("account is BLOWN", res["blown"] is True)
    check("blown event emitted", any(e.get("event") == "blown" for e in res["events"]))
    check("session status blown", res["status"] == "blown")

    # end_session: composite forced to 0, post-mortem present
    end = client.post(f"/sessions/{s}/end").get_json()
    check("blown session scores composite 0", end["score_composite"] == 0.0)
    check("end reports blown", end["blown"] is True)
    pm = end.get("post_mortem") or {}
    check("post-mortem has equity curve", isinstance(pm.get("equity_curve"), list) and len(pm["equity_curve"]) >= 2)
    check("post-mortem has 1%-risk counterfactual", "disciplined_ending_balance" in pm)
    check("post-mortem names the damaging trade", len(pm.get("worst_trades", [])) >= 1)


def test_margin_call_warning():
    # Price at 96 close: equity between maintenance and 1.5x maintenance → warn,
    # but no liquidation (still above maintenance).
    sid = _scenario([(100, 101, 99, 100), (100, 100, 96, 96)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 990, "bar_sequence": 0, "leverage": 10})
    res = client.post(f"/sessions/{s}/advance", json={"bar_sequence": 1}).get_json()
    check("margin-call warning raised", res["margin_call"] is True)
    check("not liquidated yet", not any(e["event"] == "liquidated" for e in res["events"]))
    check("position still open", any(p["status"] == "open" for p in res["positions"]))


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
