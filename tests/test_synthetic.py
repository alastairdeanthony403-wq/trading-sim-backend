"""Synthetic market generator sanity + regime-distinctness tests (Phase E).

Pure generator tests — no database required. Run:
    python tests/test_synthetic.py
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.synthetic import generate_series, REGIMES


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _log_returns(bars):
    out = []
    for i in range(1, len(bars)):
        out.append(math.log(bars[i]["close"] / bars[i - 1]["close"]))
    return out


def _stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _max_drawdown(bars):
    peak = bars[0]["close"]
    mdd = 0.0
    for b in bars:
        peak = max(peak, b["close"])
        mdd = max(mdd, (peak - b["low"]) / peak)
    return mdd


def test_ohlc_consistency_all_regimes():
    ok = True
    for regime in REGIMES:
        for seed in range(5):
            bars = generate_series(regime=regime, n_bars=150, seed=seed)
            for b in bars:
                if not (b["low"] > 0 and b["high"] > 0 and b["open"] > 0 and b["close"] > 0):
                    ok = False
                if not (b["low"] <= min(b["open"], b["close"]) + 1e-9):
                    ok = False
                if not (b["high"] >= max(b["open"], b["close"]) - 1e-9):
                    ok = False
                if not (b["high"] >= b["low"]):
                    ok = False
    check("every bar has positive, consistent OHLC across all regimes", ok)


def test_length_and_determinism():
    a = generate_series(regime="crash", n_bars=120, seed=42)
    b = generate_series(regime="crash", n_bars=120, seed=42)
    c = generate_series(regime="crash", n_bars=120, seed=43)
    check("requested bar count produced", len(a) == 120)
    check("same seed → identical series", a == b)
    check("different seed → different series", a != c)


def test_high_vol_is_more_volatile_than_range():
    hv = _stdev(_log_returns(generate_series("high_vol", 200, seed=7)))
    rg = _stdev(_log_returns(generate_series("range", 200, seed=7)))
    check("high_vol return-stdev exceeds range", hv > rg * 1.8)


def test_crash_scenario_has_deep_drawdown():
    worst = min(_max_drawdown(generate_series("crash", 160, seed=s)) for s in range(4))
    check("every crash scenario draws down hard (>20%)", worst > 0.20)


def test_trend_regimes_have_direction():
    # Any single realisation is noisy (as real trends are), so assert on the
    # mean across many seeds: up-trends drift up, down-trends drift down.
    def mean_net(regime):
        rs = [generate_series(regime, 200, seed=s)[-1]["close"] /
              generate_series(regime, 200, seed=s)[0]["open"] for s in range(16)]
        return sum(rs) / len(rs)
    up_mean = mean_net("trend_up")
    dn_mean = mean_net("trend_down")
    check("trend_up drifts up on average", up_mean > 1.10)
    check("trend_down drifts down on average", dn_mean < 0.90)
    check("up-trend beats down-trend", up_mean > dn_mean)


def test_range_stays_bounded():
    # A range market should not drift far from where it started.
    nets = [generate_series("range", 200, seed=s)[-1]["close"] /
            generate_series("range", 200, seed=s)[0]["open"] for s in range(4)]
    check("range markets stay near their origin", all(0.75 < n < 1.33 for n in nets))


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
