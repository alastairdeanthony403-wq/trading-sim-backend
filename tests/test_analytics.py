"""Performance analytics (Phase 7).

Standard trade stats — win rate, expectancy (R), profit factor, payoff ratio,
best/worst, hold time, max drawdown — derived from the replay trades.

Requires a Postgres DATABASE_URL + SETUP_KEY:
    DATABASE_URL=postgresql://.../db SETUP_KEY=testkey python tests/test_analytics.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, analytics

app = create_app()
client = app.test_client()
KEY = {"X-Setup-Key": os.environ.get("SETUP_KEY", "")}


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _t(pnl, r=None, entered=0, exited=1):
    return {"pnl": pnl, "achieved_r": r, "bar_entered": entered, "bar_exited": exited}


def test_empty_session_is_zeroed():
    p = analytics.performance([], 10000)
    check("no trades → zero counts", p["trades"] == 0 and p["wins"] == 0)
    check("undefined ratios are None, not fake zeros",
          p["profit_factor"] is None and p["expectancy_r"] is None)


def test_win_rate_and_counts():
    trades = [_t(100), _t(-50), _t(30), _t(-20)]
    p = analytics.performance(trades, 10000)
    check("counts wins and losses", p["wins"] == 2 and p["losses"] == 2)
    check("win rate is 50%", p["win_rate"] == 50.0)
    check("total pnl sums", p["total_pnl"] == 60.0)


def test_profit_factor_and_payoff():
    # gross profit 130, gross loss 70 → PF ≈ 1.86; avg win 65, avg loss -35 → payoff ≈ 1.86
    trades = [_t(100), _t(30), _t(-50), _t(-20)]
    p = analytics.performance(trades, 10000)
    check("profit factor = gross profit / gross loss", p["profit_factor"] == round(130 / 70, 2))
    check("payoff ratio = avg win / avg loss", p["payoff_ratio"] == round(65 / 35, 2))
    check("gross profit / loss reported", p["gross_profit"] == 130.0 and p["gross_loss"] == 70.0)


def test_profit_factor_none_when_no_losers():
    p = analytics.performance([_t(10), _t(20)], 10000)
    check("all winners → profit factor undefined (None)", p["profit_factor"] is None)
    check("win rate 100%", p["win_rate"] == 100.0)


def test_expectancy_and_r_stats():
    trades = [_t(200, r=2.0), _t(-100, r=-1.0), _t(-100, r=-1.0)]
    p = analytics.performance(trades, 10000)
    check("expectancy is the mean R", p["expectancy_r"] == round((2.0 - 1.0 - 1.0) / 3, 2))
    check("avg win R", p["avg_win_r"] == 2.0)
    check("avg loss R", p["avg_loss_r"] == -1.0)


def test_extremes_and_hold_time():
    trades = [_t(500, entered=0, exited=10), _t(-300, entered=5, exited=8)]
    p = analytics.performance(trades, 10000)
    check("largest win", p["largest_win"] == 500.0)
    check("largest loss", p["largest_loss"] == -300.0)
    check("average hold in bars", p["avg_hold_bars"] == round((10 + 3) / 2, 1))


def test_max_drawdown():
    # equity: 1000 -> 1100 -> 900 -> 950; peak 1100, trough 900 → dd ≈ 18.18%
    trades = [_t(100), _t(-200), _t(50)]
    p = analytics.performance(trades, 1000)
    check("max drawdown from the equity curve", p["max_drawdown_pct"] == round(200 / 1100 * 100, 2))


def test_replay_includes_performance():
    r = client.post("/setup/generate-scenarios",
                    json={"regimes": ["trend_up"], "per_regime": 1, "seed": 6161,
                          "history_bars": 60, "playback_bars": 40}, headers=KEY).get_json()
    sid = r["results"][0]["scenario_id"]
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "an"}).get_json()
    sess = s["session_id"]
    entry = s["history_bars"] - 1
    price = client.get(f"/sessions/{sess}/bars").get_json()[entry]["close"]
    client.post(f"/sessions/{sess}/trades", json={
        "direction": "long", "size": 10, "bar_sequence": entry,
        "stop_loss": round(price * 0.98, 2), "take_profit": round(price * 1.05, 2)})
    client.post(f"/sessions/{sess}/advance", json={"bar_sequence": entry + 39})
    client.post(f"/sessions/{sess}/end")
    replay = client.get(f"/sessions/{sess}/replay").get_json()
    check("replay carries a performance block", "performance" in replay)
    perf = replay["performance"]
    check("performance has the standard fields",
          all(k in perf for k in ("win_rate", "expectancy_r", "profit_factor",
                                  "payoff_ratio", "max_drawdown_pct", "avg_hold_bars")))
    check("trade count matches the trades shown", perf["trades"] == len(replay["trades"]))


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
