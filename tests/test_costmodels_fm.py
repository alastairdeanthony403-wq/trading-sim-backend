"""Per-asset cost models, Fund Manager mode, and concentration (Phase D).

Requires a Postgres DATABASE_URL. Run:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_costmodels_fm.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar

app = create_app()
client = app.test_client()


def _scenario(bars, asset_class="stocks"):
    with app.app_context():
        s = Scenario(name_internal="c", asset_class=asset_class, timeframe="1D",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate(bars):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


def _start(sid, mode="standard"):
    return client.post(f"/scenarios/{sid}/start",
                       json={"user_id": "t", "mode": mode}).get_json()["session_id"]


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


FLAT = [(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100)]


def test_cost_model_slippage_by_asset_class():
    # Same bar, same order: crypto (0.10%) slips the entry further than
    # indices (0.01%). Long entry is pushed UP by slippage.
    crypto = _start(_scenario(FLAT, "crypto"))
    indices = _start(_scenario(FLAT, "indices"))
    rc = client.post(f"/sessions/{crypto}/trades",
                     json={"direction": "long", "size": 50, "bar_sequence": 0}).get_json()
    ri = client.post(f"/sessions/{indices}/trades",
                     json={"direction": "long", "size": 50, "bar_sequence": 0}).get_json()
    check("crypto entry slipped to ~100.10", abs(rc["entry_price"] - 100.10) < 1e-6)
    check("indices entry slipped to ~100.01", abs(ri["entry_price"] - 100.01) < 1e-6)
    check("crypto slippage wider than indices", rc["entry_price"] > ri["entry_price"])


def test_fm_requires_stop():
    s = _start(_scenario(FLAT), mode="fund_manager")
    r = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 50, "bar_sequence": 0})
    check("FM rejects a stopless trade 400", r.status_code == 400)
    check("error names the stop requirement", "stop" in r.get_json().get("error", "").lower())


def test_fm_rejects_oversize_risk():
    s = _start(_scenario(FLAT), mode="fund_manager")
    # entry ~100, stop 90, size 50 → risk ~500 = 5% of a 10k fund > 1% cap.
    r = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 50, "bar_sequence": 0,
                          "stop_loss": 90})
    check("FM rejects >1% risk 400", r.status_code == 400)
    check("error mentions the fund limit", "1%" in r.get_json().get("error", ""))


def test_fm_accepts_disciplined_risk():
    s = _start(_scenario(FLAT), mode="fund_manager")
    # entry ~100, stop 99, size 50 → risk ~50 = 0.5% of the fund ≤ 1% cap.
    r = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 50, "bar_sequence": 0,
                          "stop_loss": 99})
    check("FM admits a ≤1% risk trade", r.status_code == 200 and r.get_json().get("status") == "open")


def test_fm_drawdown_fails_session():
    # A run of stop-outs that gap through the stop, each losing ~2% of the fund.
    # Once cumulative drawdown passes 8% the mandate is terminated (fund_fired).
    bars = []
    for _ in range(6):
        bars.append((100, 100.5, 99.5, 100))   # entry bar
        bars.append((96, 96.5, 95.5, 96))       # gaps below the 99 stop → ~2% loss
    s = _start(_scenario(bars, "indices"), mode="fund_manager")
    fired = False
    for k in range(6):
        entry_bar = 2 * k
        client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 50, "bar_sequence": entry_bar,
                          "stop_loss": 99})
        res = client.post(f"/sessions/{s}/advance",
                          json={"bar_sequence": entry_bar + 1}).get_json()
        if any(e.get("event") == "fund_fired" for e in res["events"]):
            fired = True
            check("session blown when the mandate is terminated", res["status"] == "blown")
            break
    check("8% drawdown terminates the Fund Manager mandate", fired)


def test_concentration_flag():
    # Two open positions, one carrying >60% of total open risk → concentrated.
    s = _start(_scenario(FLAT))
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 20, "bar_sequence": 0, "stop_loss": 95})
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 20, "bar_sequence": 0, "stop_loss": 99})
    res = client.post(f"/sessions/{s}/advance", json={"bar_sequence": 1}).get_json()
    check("lopsided risk flags concentration", res["concentrated"] is True)


def test_balanced_not_concentrated():
    s = _start(_scenario(FLAT))
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 20, "bar_sequence": 0, "stop_loss": 97})
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 20, "bar_sequence": 0, "stop_loss": 97})
    res = client.post(f"/sessions/{s}/advance", json={"bar_sequence": 1}).get_json()
    check("evenly split risk is not concentrated", res["concentrated"] is False)


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
