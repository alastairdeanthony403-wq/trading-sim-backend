"""Market-structure metadata (Phase 3).

Structure is derived purely from OHLC — deterministic, no storage — and is
SERVER-SIDE ONLY: it appears in the post-session replay, never in the live bars
feed. These tests check the detectors on crafted series and the exposure rules.

Requires a Postgres DATABASE_URL + SETUP_KEY:
    DATABASE_URL=postgresql://.../db SETUP_KEY=testkey python tests/test_structure.py
"""
import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, bar_provider
from app import structure as st
from app.coach import build_findings
from app.models.scenario import Scenario, ScenarioBar

app = create_app()
client = app.test_client()
KEY = {"X-Setup-Key": os.environ.get("SETUP_KEY", "")}

B = namedtuple("B", "bar_sequence open high low close volume")


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _bar(i, o, h, l, c):
    return B(i, o, h, l, c, 1000)


def test_swing_points_detects_pivots():
    # a clean peak at i=4 and a clean trough at i=10
    prices = [100, 101, 102, 104, 108, 104, 102, 101, 100, 98, 95, 98, 100, 101, 102]
    bars = [_bar(i, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
    sw = st.swing_points(bars, span=3)
    highs = [s for s in sw if s["kind"] == "high"]
    lows = [s for s in sw if s["kind"] == "low"]
    check("a swing high is found at the peak", any(s["bar_sequence"] == 4 for s in highs))
    check("a swing low is found at the trough", any(s["bar_sequence"] == 10 for s in lows))


def test_levels_cluster_revisited_prices():
    # two swing highs at ~the same price → one resistance level with 2 touches
    swings = [
        {"bar_sequence": 4, "price": 108.0, "kind": "high"},
        {"bar_sequence": 20, "price": 108.2, "kind": "high"},
        {"bar_sequence": 10, "price": 95.0, "kind": "low"},
    ]
    lv = st.levels(swings, tol=0.01, min_touches=2)
    check("one clustered level survives (>=2 touches)", len(lv) == 1)
    check("it is resistance with 2 touches near 108",
          lv[0]["kind"] == "resistance" and lv[0]["touches"] == 2 and abs(lv[0]["price"] - 108.1) < 0.2)


def test_liquidity_sweep_detected():
    # Explicit OHLC: a swing HIGH of 108 forms at i=4; later, bar i=12 wicks to
    # 109 (clearly above 108) but CLOSES back under at 106 — a stop-hunt sweep.
    highs = [101, 102, 103, 105, 108, 105, 103, 102, 101, 102, 103, 104, 109, 105, 104, 103, 102, 101]
    bars = []
    for i, h in enumerate(highs):
        c = h - 1                                   # close a touch under the high
        bars.append(B(i, c, h, h - 3, c, 1000))
    bars[12] = B(12, 106, 109.0, 105, 106.0, 1000)  # poke above 108, close at 106
    sw = st.swing_points(bars, span=3)
    check("swing high of 108 is found at i=4",
          any(s["bar_sequence"] == 4 and abs(s["price"] - 108) < 0.01 for s in sw))
    sweeps = st.liquidity_sweeps(bars, sw)
    check("a high-side sweep is detected", any(s["side"] == "high" for s in sweeps))
    check("the sweep is anchored to the wicking bar",
          any(s["bar_sequence"] == 12 for s in sweeps))


def test_failed_breakout_detected():
    lv = [{"price": 100.0, "kind": "resistance", "touches": 2, "first_bar": 0, "last_bar": 2}]
    bars = [_bar(i, 99, 99.5, 98.5, 99) for i in range(4)]
    bars.append(_bar(4, 99, 101.5, 99, 101.0))   # closes above 100 (breakout)
    bars.append(_bar(5, 101, 101, 99, 99.5))     # closes back below 100 (fails)
    bars += [_bar(i, 99, 99.5, 98.5, 99) for i in range(6, 9)]
    fbo = st.failed_breakouts(bars, lv, within=5)
    check("a failed breakout is detected", len(fbo) == 1 and fbo[0]["side"] == "up")
    check("it records the recovery bar", fbo[0]["recovered_bar"] == 5)


def test_annotate_is_deterministic_on_a_scenario():
    r = client.post("/setup/generate-scenarios",
                    json={"regimes": ["high_vol"], "per_regime": 1, "seed": 3131,
                          "history_bars": 120, "playback_bars": 80}, headers=KEY).get_json()
    sid = r["results"][0]["scenario_id"]
    with app.app_context():
        sc = Scenario.query.get(sid)
        a = st.annotate(bar_provider.series(sc))
        bar_provider._generated.cache_clear()
        b = st.annotate(bar_provider.series(sc))
        check("structure is identical across regenerations", a == b)
        check("a real scenario yields some swings", len(a["swings"]) > 0)


def test_structure_is_server_side_only():
    """Structure appears in the post-session replay, NEVER in the live bars feed."""
    r = client.post("/setup/generate-scenarios",
                    json={"regimes": ["trend_up"], "per_regime": 1, "seed": 4242,
                          "history_bars": 60, "playback_bars": 40}, headers=KEY).get_json()
    sid = r["results"][0]["scenario_id"]
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "st"}).get_json()
    live = client.get(f"/sessions/{s['session_id']}/bars").get_json()
    check("live bars are a plain OHLC list (no structure attached)",
          isinstance(live, list) and all("structure" not in b for b in live[:3]))
    client.post(f"/sessions/{s['session_id']}/end").get_json()
    replay = client.get(f"/sessions/{s['session_id']}/replay").get_json()
    check("replay carries the structure block", "structure" in replay)
    check("structure has the four layers",
          all(k in replay["structure"] for k in ("swings", "levels", "sweeps", "failed_breakouts")))


def test_coach_flags_stops_taken_by_a_sweep():
    """The market-context finding fires when a stop-out coincides with a sweep."""
    replay = {
        "trades": [{"trade_id": 1, "direction": "long", "exit_reason": "stop_loss",
                    "bar_exited": 50, "pnl": -30.0, "achieved_r": -1.0,
                    "planned_r": 2.0, "size": 10}],
        "structure": {"sweeps": [{"bar_sequence": 50, "price": 100.0, "side": "low",
                                  "penetration": 0.4}]},
    }
    disc = {"no_stop_count": 0, "revenge_count": 0, "oversize_count": 0,
            "discipline_score": 70, "trades_total": 1}
    findings = build_findings(None, disc, replay)
    check("a liquidity-sweep coaching note is produced",
          any(f["lesson_id"] == "liquidity_concepts" for f in findings))


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
