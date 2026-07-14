"""Order-execution engine tests (Phase A).

Requires a Postgres DATABASE_URL (the models use ARRAY columns). Run with:

    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_order_engine.py

Each test builds its own scenario with hand-crafted bars so the SL/TP/gap/
trailing/limit/stop behaviour is exact and independent of ingested data.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar
from app.models.session import Trade

app = create_app()
client = app.test_client()


def _scenario(bars):
    """bars = list of (open, high, low, close); returns scenario_id."""
    with app.app_context():
        s = Scenario(name_internal="test", asset_class="crypto", timeframe="1h",
                     difficulty_tier=1, is_active=True)
        db.session.add(s)
        db.session.flush()
        for i, (o, h, l, c) in enumerate(bars):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


def _start(scenario_id):
    r = client.post(f"/scenarios/{scenario_id}/start", json={"user_id": "t"})
    return r.get_json()["session_id"]


def _advance(session_id, up_to):
    return client.post(f"/sessions/{session_id}/advance",
                       json={"bar_sequence": up_to}).get_json()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_long_stop_loss_intrabar():
    # entry bar0 close=100; bar1 dips to 94 (< SL 95) then recovers
    sid = _scenario([(100, 101, 99, 100), (100, 101, 94, 100), (100, 102, 99, 101)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 1,
                "bar_sequence": 0, "stop_loss": 95})
    res = _advance(s, 2)
    ev = res["events"]
    check("long SL fires once", len(ev) == 1 and ev[0]["event"] == "closed")
    check("long SL reason", ev[0]["reason"] == "stop_loss")
    check("long SL at bar1", ev[0]["bar_sequence"] == 1)
    # filled ~95 minus slippage → pnl clearly negative
    check("long SL negative pnl", ev[0]["pnl"] < 0)


def test_long_take_profit_intrabar():
    sid = _scenario([(100, 101, 99, 100), (100, 106, 99, 101)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 1,
                "bar_sequence": 0, "take_profit": 105})
    ev = _advance(s, 1)["events"]
    check("long TP fires", len(ev) == 1 and ev[0]["reason"] == "take_profit")
    check("long TP positive pnl", ev[0]["pnl"] > 0)


def test_gap_down_through_stop_fills_at_open():
    # bar1 GAPS open to 90, well below SL 95 → fill at 90, not 95
    sid = _scenario([(100, 101, 99, 100), (90, 92, 88, 91)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 10,
                "bar_sequence": 0, "stop_loss": 95})
    ev = _advance(s, 1)["events"]
    # exit ~90 (minus slippage); entry ~100 → pnl ≈ -100, worse than -50 (a 95 fill)
    check("gap-down fills at open (worse than stop)", ev[0]["pnl"] < -90)
    check("gap-down reason stop_loss", ev[0]["reason"] == "stop_loss")


def test_gap_up_through_tp_fills_at_open():
    sid = _scenario([(100, 101, 99, 100), (112, 114, 111, 113)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 10,
                "bar_sequence": 0, "take_profit": 105})
    ev = _advance(s, 1)["events"]
    # fill ~112 not 105 → pnl > 100 (better than a 105 fill's ~50)
    check("gap-up fills at open (better than target)", ev[0]["pnl"] > 100)


def test_pessimistic_tie_stop_wins():
    # bar1 spans BOTH sl(95) and tp(105) intrabar → stop assumed first
    sid = _scenario([(100, 101, 99, 100), (100, 106, 94, 100)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 1,
                "bar_sequence": 0, "stop_loss": 95, "take_profit": 105})
    ev = _advance(s, 1)["events"]
    check("tie resolves to stop_loss", ev[0]["reason"] == "stop_loss")


def test_trailing_stop_ratchets():
    # rises 100→108 then reverses; trail 3 → stop trails to ~105 → stopped out
    sid = _scenario([
        (100, 101, 99, 100),   # bar0 entry
        (101, 104, 100, 104),  # bar1 close 104 → anchor 104
        (104, 108, 103, 108),  # bar2 close 108 → anchor 108, trail stop 105
        (108, 108, 104, 105),  # bar3 dips to 104 < 105 → trailing stop hit
    ])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 1,
                "bar_sequence": 0, "trail_distance": 3})
    ev = _advance(s, 3)["events"]
    check("trailing stop fires", len(ev) == 1)
    check("trailing reason", ev[0]["reason"] == "trailing_stop")
    check("trailing exit at bar3", ev[0]["bar_sequence"] == 3)
    check("trailing locked in a profit", ev[0]["pnl"] > 0)


def test_no_trigger_when_untouched():
    sid = _scenario([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 1,
                "bar_sequence": 0, "stop_loss": 90, "take_profit": 110})
    res = _advance(s, 2)
    check("no exit when levels untouched", res["events"] == [])
    check("position still open", res["positions"][0]["status"] == "open")


def test_limit_entry_fills_then_targets():
    # resting long limit at 96; bar1 dips to 95 → fills; bar2 hits TP 105
    sid = _scenario([(100, 101, 99, 100), (99, 100, 95, 99), (100, 106, 99, 105)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 1,
                "bar_sequence": 0, "order_type": "limit", "entry_order_price": 96,
                "take_profit": 105})
    ev = _advance(s, 2)["events"]
    kinds = [(e["event"], e.get("reason")) for e in ev]
    check("limit fills then closes on TP", ("filled", None) in kinds and
          any(k[0] == "closed" and k[1] == "take_profit" for k in kinds))


def test_stop_entry_breakout_fills():
    # resting long stop at 104; bar1 breaks up to 106 → fills
    sid = _scenario([(100, 101, 99, 100), (101, 106, 100, 105)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 1,
                "bar_sequence": 0, "order_type": "stop", "entry_order_price": 104})
    res = _advance(s, 1)
    check("stop entry fills on breakout",
          any(e["event"] == "filled" for e in res["events"]))


def test_advance_idempotent():
    sid = _scenario([(100, 101, 99, 100), (100, 101, 94, 100), (100, 101, 99, 100)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 1,
                "bar_sequence": 0, "stop_loss": 95})
    first = _advance(s, 2)["events"]
    second = _advance(s, 2)["events"]     # replay must NOT double-close
    check("advance is idempotent", len(first) == 1 and second == [])


def test_short_stop_and_tp():
    # short at 100; bar1 rises to 106 (> SL 105) → stopped
    sid = _scenario([(100, 101, 99, 100), (101, 106, 100, 104)])
    s = _start(sid)
    client.post(f"/sessions/{s}/trades", json={"direction": "short", "size": 1,
                "bar_sequence": 0, "stop_loss": 105})
    ev = _advance(s, 1)["events"]
    check("short SL fires", ev and ev[0]["reason"] == "stop_loss")
    check("short SL negative pnl", ev[0]["pnl"] < 0)


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
            print(f"  ERROR {e}")
            failed += 1
    print(f"\n{'ALL PASSED' if failed == 0 else str(failed) + ' FAILED'} "
          f"({len(TESTS)} tests)")
    sys.exit(1 if failed else 0)
