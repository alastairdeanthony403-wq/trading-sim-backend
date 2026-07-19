"""Synthetic market engine v2 — statistical realism + candlestick occurrence.

Pure generator tests, no DB. Run:
    python tests/test_synthetic.py

These are the acceptance tests for the realism bar: fat tails (excess kurtosis),
volatility clustering, NO raw-return predictability, valid bars, cross-seed
independence, volume behaviour, and emergent candlestick pattern rates.
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


# ── stats helpers (stdlib) ────────────────────────────────────────────────
def _logrets(bars):
    return [math.log(bars[i]["close"] / bars[i - 1]["close"]) for i in range(1, len(bars))]


def _mean(x):
    return sum(x) / len(x)


def _excess_kurtosis(x):
    m = _mean(x); v = sum((a - m) ** 2 for a in x) / len(x)
    return (sum((a - m) ** 4 for a in x) / len(x) / v ** 2 - 3) if v else float("nan")


def _autocorr(x, lag):
    m = _mean(x); v = sum((a - m) ** 2 for a in x)
    return sum((x[i] - m) * (x[i - lag] - m) for i in range(lag, len(x))) / v if v else 0.0


def _corr(a, b):
    n = min(len(a), len(b)); a, b = a[:n], b[:n]
    ma, mb = _mean(a), _mean(b)
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = math.sqrt(sum((a[i] - ma) ** 2 for i in range(n)))
    vb = math.sqrt(sum((b[i] - mb) ** 2 for i in range(n)))
    return cov / (va * vb) if va and vb else 0.0


def _pool_logrets():
    out = []
    for reg in REGIMES:
        for s in range(8):
            out += _logrets(generate_series(reg, 400, seed=s))
    return out


# ── tests ─────────────────────────────────────────────────────────────────
def test_bars_are_valid():
    ok = True
    for reg in REGIMES:
        for seed in range(6):
            for b in generate_series(reg, 300, seed=seed):
                if not (b["low"] > 0 and b["low"] <= min(b["open"], b["close"]) + 1e-9
                        and b["high"] >= max(b["open"], b["close"]) - 1e-9
                        and b["high"] >= b["low"] and b["volume"] > 0):
                    ok = False
    check("all bars valid (OHLC consistent, positive price & volume)", ok)


def test_determinism_and_uniqueness():
    a = generate_series("trend_up", 200, seed=42)
    b = generate_series("trend_up", 200, seed=42)
    c = generate_series("trend_up", 200, seed=43)
    check("requested bar count produced", len(a) == 200)
    check("same seed → identical series", a == b)
    check("different seed → different series", a != c)


def test_fat_tails_excess_kurtosis():
    ek = _excess_kurtosis(_pool_logrets())
    check(f"returns are fat-tailed (excess kurtosis {ek:.2f} > 1)", ek > 1.0)


def test_volatility_clustering():
    # |returns| autocorrelation must be positive/significant at lags 1–5.
    abs_r = [abs(v) for v in _pool_logrets()]
    acs = [_autocorr(abs_r, lag) for lag in range(1, 6)]
    check("|returns| autocorrelation positive at every lag 1–5", all(a > 0.05 for a in acs))
    check(f"mean |returns| autocorrelation is significant ({_mean(acs):.3f} > 0.10)", _mean(acs) > 0.10)


def test_no_raw_return_predictability():
    # Raw returns must NOT be autocorrelated (no free-money edge).
    r = _pool_logrets()
    acs = [abs(_autocorr(r, lag)) for lag in range(1, 6)]
    check(f"raw-return autocorrelation ~0 at lags 1–5 (max |ac| {max(acs):.3f} < 0.10)", max(acs) < 0.10)


def test_cross_seed_independence():
    worst = 0.0
    for reg in ("trend_up", "range", "high_vol"):
        c = abs(_corr(_logrets(generate_series(reg, 400, seed=1)),
                      _logrets(generate_series(reg, 400, seed=2))))
        worst = max(worst, c)
    check(f"different seeds are uncorrelated (|corr| {worst:.3f} < 0.15)", worst < 0.15)


def test_volume_tracks_range():
    corrs = []
    for reg in REGIMES:
        bars = generate_series(reg, 400, seed=3)
        rng = [b["high"] - b["low"] for b in bars]
        vol = [b["volume"] for b in bars]
        corrs.append(_corr(rng, vol))
    check(f"volume correlates with bar range (mean corr {_mean(corrs):.2f} > 0.3)", _mean(corrs) > 0.3)


def test_regimes_have_distinct_volatility():
    def vol(reg):
        return math.sqrt(_mean([r * r for r in _logrets(generate_series(reg, 400, seed=7))]))
    check("high_vol regime is more volatile than low_vol", vol("high_vol") > vol("low_vol") * 1.8)


def test_candlestick_patterns_emerge_in_realistic_bands():
    # Emergent (not scripted) pattern rates: present, but not dominating.
    def rates(bars):
        n = len(bars); d = p = e = ins = out = 0
        for i, b in enumerate(bars):
            o, h, l, c = b["open"], b["high"], b["low"], b["close"]
            rng = h - l; body = abs(c - o)
            if rng <= 0:
                continue
            if body / rng < 0.1:
                d += 1
            uw, lw = h - max(o, c), min(o, c) - l
            if (uw > 2 * body and uw > lw) or (lw > 2 * body and lw > uw):
                p += 1
            if i > 0:
                pb = bars[i - 1]; po, ph, pl, pc = pb["open"], pb["high"], pb["low"], pb["close"]
                if body > abs(pc - po) and (c > o) != (pc > po) and max(o, c) >= max(po, pc) and min(o, c) <= min(po, pc):
                    e += 1
                if h <= ph and l >= pl:
                    ins += 1
                if h > ph and l < pl:
                    out += 1
        m = n - 1
        return d / n, p / n, e / m, ins / m, out / m

    agg = [[] for _ in range(5)]
    for reg in REGIMES:
        for s in range(8):
            for k, v in enumerate(rates(generate_series(reg, 400, seed=s))):
                agg[k].append(v)
    doji, pin, eng, ins, out = (_mean(a) for a in agg)
    check(f"dojis occur in a realistic band ({doji*100:.1f}% in 2–15%)", 0.02 <= doji <= 0.15)
    check(f"pin bars emerge, not dominating ({pin*100:.1f}% in 5–40%)", 0.05 <= pin <= 0.40)
    check(f"engulfing bars emerge, not dominating ({eng*100:.1f}% in 5–40%)", 0.05 <= eng <= 0.40)
    check(f"inside bars emerge ({ins*100:.1f}% in 3–30%)", 0.03 <= ins <= 0.30)
    check(f"outside bars emerge ({out*100:.1f}% in 3–30%)", 0.03 <= out <= 0.30)


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
