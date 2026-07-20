"""Asset personalities + correlated benchmark (Phase 6).

Each market class shapes the generated series (crypto wild + fat-tailed, forex
smooth + mean-reverting, …), and a scenario can carry a benchmark line correlated
with it. asset=None must leave the neutral series byte-identical.

Requires a Postgres DATABASE_URL + SETUP_KEY:
    DATABASE_URL=postgresql://.../db SETUP_KEY=testkey python tests/test_personalities.py
"""
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, bar_provider
from app import synthetic as syn
from app.models.scenario import Scenario

app = create_app()
client = app.test_client()
KEY = {"X-Setup-Key": os.environ.get("SETUP_KEY", "")}


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _log_rets(bars):
    c = [b["close"] for b in bars]
    return [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]


def _vol(bars):
    return statistics.pstdev(_log_rets(bars))


def _kurtosis(bars):
    r = _log_rets(bars)
    m = statistics.fmean(r)
    sd = statistics.pstdev(r) or 1e-9
    return sum(((x - m) / sd) ** 4 for x in r) / len(r)


def test_asset_none_is_unchanged():
    a = syn.generate_series(regime="range", n_bars=300, seed=42)
    b = syn.generate_series(regime="range", n_bars=300, seed=42, asset=None)
    check("asset=None leaves the neutral series byte-identical", a == b)


def test_crypto_is_wilder_than_forex():
    crypto = syn.generate_series(regime="range", n_bars=1500, seed=7, asset="crypto")
    forex = syn.generate_series(regime="range", n_bars=1500, seed=7, asset="forex")
    check("crypto is more volatile than forex", _vol(crypto) > _vol(forex) * 1.4)
    check("crypto has fatter tails than forex", _kurtosis(crypto) > _kurtosis(forex))


def test_forex_mean_reverts():
    """Mean reversion shows in the variance ratio: k-bar returns accumulate LESS
    than a random walk would, so VR(k) is well below the neutral series'."""
    def variance_ratio(bars, k=20):
        r = _log_rets(bars)
        v1 = statistics.pvariance(r)
        kk = [sum(r[i:i + k]) for i in range(0, len(r) - k)]
        return statistics.pvariance(kk) / (k * v1) if v1 else 1.0
    forex = syn.generate_series(regime="range", n_bars=1500, seed=3, asset="forex")
    neutral = syn.generate_series(regime="range", n_bars=1500, seed=3)
    check("forex mean-reverts — variance ratio well below neutral",
          variance_ratio(forex) < variance_ratio(neutral) * 0.7)


def test_personalities_are_distinct_and_deterministic():
    vols = {a: _vol(syn.generate_series(regime="range", n_bars=500, seed=11, asset=a))
            for a in ("crypto", "forex", "indices", "commodities", "stocks")}
    check("crypto is the most volatile class", vols["crypto"] == max(vols.values()))
    check("forex is the least volatile class", vols["forex"] == min(vols.values()))
    again = syn.generate_series(regime="range", n_bars=500, seed=11, asset="crypto")
    check("same seed + asset regenerate identically",
          again == syn.generate_series(regime="range", n_bars=500, seed=11, asset="crypto"))


def test_correlated_benchmark_matches_rho():
    base = syn.generate_series(regime="trend_up", n_bars=800, seed=99)
    for rho in (0.3, 0.8):
        line = syn.correlated_line(base, seed=1234, rho=rho)
        check(f"benchmark line has one point per bar (rho={rho})", len(line) == len(base))
        br = _log_rets(base)
        lr = [math.log(line[i]["value"] / line[i - 1]["value"]) for i in range(1, len(line))]
        realized = statistics.correlation(br, lr)
        check(f"realized correlation ≈ {rho} (got {realized:.2f})", abs(realized - rho) < 0.1)


def test_endpoint_personality_and_benchmark():
    r = client.post("/setup/generate-scenarios",
                    json={"regimes": ["range"], "per_regime": 1, "seed": 555,
                          "history_bars": 120, "playback_bars": 80,
                          "asset_class": "crypto", "benchmark": True, "rho": 0.75},
                    headers=KEY).get_json()["results"][0]
    check("endpoint reports the personality label", "Wild" in (r["personality"] or ""))
    check("endpoint reports a benchmark was attached", r["benchmark"] is True)
    sid = r["scenario_id"]
    with app.app_context():
        sc = Scenario.query.get(sid)
        check("asset personality stored in gen_params", sc.gen_params.get("asset") == "crypto")
        check("crypto tag applied", "crypto" in (sc.tags or []))
        ref = bar_provider.reference(sc)
        check("benchmark line is derived and served", len(ref) == bar_provider.count(sc))

    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "ap"}).get_json()
    check("start_session advertises the benchmark", s["has_reference"] is True)
    ref_ep = client.get(f"/sessions/{s['session_id']}/reference").get_json()
    check("reference endpoint serves the line", len(ref_ep) > 0 and "value" in ref_ep[0])
    capped = client.get(f"/sessions/{s['session_id']}/reference?up_to=50").get_json()
    check("reference respects the reveal cap", all(p["bar_sequence"] <= 50 for p in capped))


def test_synthetic_scenario_has_no_personality():
    r = client.post("/setup/generate-scenarios",
                    json={"regimes": ["range"], "per_regime": 1, "seed": 8,
                          "history_bars": 60, "playback_bars": 40}, headers=KEY).get_json()["results"][0]
    check("synthetic asset_class carries no personality", r["personality"] is None)
    with app.app_context():
        sc = Scenario.query.get(r["scenario_id"])
        check("no asset in gen_params for synthetic", "asset" not in (sc.gen_params or {}))
        check("no benchmark for a plain scenario", bar_provider.reference(sc) == [])


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
