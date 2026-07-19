"""Multi-timeframe intraday scenarios (Phase 2).

The stored series is 1-minute bars; higher timeframes are aggregated on read.
These tests prove the aggregation is exact and reveal-aware (no future bar leaks
into a coarser timeframe), plus the mint/serve pipeline end-to-end.

Requires a Postgres DATABASE_URL + SETUP_KEY:
    DATABASE_URL=postgresql://.../db SETUP_KEY=testkey python tests/test_intraday.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, bar_provider
from app.bar_provider import aggregate, TF_MINUTES, Bar
from app.models.scenario import Scenario

app = create_app()
client = app.test_client()
KEY = {"X-Setup-Key": os.environ.get("SETUP_KEY", "")}


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _mint_intraday(seed=4242, days=2, bars_per_day=390, anchor="15m", history=20):
    r = client.post("/setup/generate-intraday-scenarios",
                    json={"regimes": ["trend_up"], "per_regime": 1, "seed": seed,
                          "days": days, "bars_per_day": bars_per_day,
                          "anchor_tf": anchor, "history_candles": history},
                    headers=KEY).get_json()
    return r["results"][0]["scenario_id"]


def test_aggregation_is_exact():
    """Every 15m candle must equal its constituent 1m bars: open=first,
    high=max, low=min, close=last, volume=sum."""
    with app.app_context():
        sid = _mint_intraday()
        sc = Scenario.query.get(sid)
        base = bar_provider.series(sc)
        agg = bar_provider.series_tf(sc, "15m")
        mult = TF_MINUTES["15m"]
        ok = True
        for cndl in agg:
            grp = [b for b in base if cndl["base_start"] <= b.bar_sequence <= cndl["base_end"]]
            # a full (non-final) bucket must hold exactly `mult` base bars
            if cndl["base_end"] // mult == cndl["base_start"] // mult and \
               len(base) > cndl["base_end"] + 1 and len(grp) != mult:
                ok = False
            if not (math.isclose(cndl["open"], grp[0].open) and
                    math.isclose(cndl["close"], grp[-1].close) and
                    math.isclose(cndl["high"], max(g.high for g in grp)) and
                    math.isclose(cndl["low"], min(g.low for g in grp)) and
                    math.isclose(cndl["volume"], round(sum(g.volume for g in grp), 2))):
                ok = False
        check("15m candles exactly equal their 1m constituents (OHLCV)", ok)
        check("15m candle count == ceil(1m count / 15)",
              len(agg) == math.ceil(len(base) / mult))


def test_aggregation_no_future_leak():
    """Aggregating up to a reveal point must use ONLY revealed base bars — the
    forming candle is partial, and no aggregated bar references a future bar."""
    base = [Bar(i, 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i + 0.2, 10.0)
            for i in range(100)]
    up_to = 37                          # mid-way through bucket 2 (bars 30..44)
    agg = aggregate(base, 15, up_to_base=up_to)
    last = agg[-1]
    check("no aggregated candle spans beyond the reveal point",
          all(c["base_end"] <= up_to for c in agg))
    check("the forming candle is partial (revealed bars only)",
          last["base_start"] == 30 and last["base_end"] == 37)
    check("partial candle's high/close come from revealed bars only",
          math.isclose(last["close"], base[37].close) and
          math.isclose(last["high"], max(b.high for b in base[30:38])))
    # revealing one more bar extends the same forming candle, never rewrites past ones
    agg2 = aggregate(base, 15, up_to_base=up_to + 1)
    check("revealing +1 bar only extends the forming candle",
          agg2[-1]["base_end"] == 38 and agg2[:-1] == agg[:-1])


def test_all_timeframes_nest_consistently():
    """Coarser timeframes must be exact re-aggregations of finer ones — a 1h
    candle equals the four 15m candles it contains."""
    with app.app_context():
        sc = Scenario.query.get(_mint_intraday(seed=99))
        m15 = bar_provider.series_tf(sc, "15m")
        h1 = bar_provider.series_tf(sc, "1h")
        ok = True
        for hc in h1:
            kids = [c for c in m15 if hc["base_start"] <= c["base_start"] <= hc["base_end"]]
            if not (math.isclose(hc["open"], kids[0]["open"]) and
                    math.isclose(hc["close"], kids[-1]["close"]) and
                    math.isclose(hc["high"], max(k["high"] for k in kids)) and
                    math.isclose(hc["low"], min(k["low"] for k in kids))):
                ok = False
        check("1h candles are exact re-aggregations of their 15m candles", ok)


def test_determinism():
    with app.app_context():
        sc = Scenario.query.get(_mint_intraday(seed=555))
        s1 = bar_provider.series(sc)
        bar_provider._generated.cache_clear()
        s2 = bar_provider.series(sc)
        check("same seed regenerates identical 1m base series", s1 == s2)


def test_metadata_and_endpoint():
    sid = _mint_intraday(seed=2024, days=2, bars_per_day=390, anchor="15m", history=20)
    with app.app_context():
        sc = Scenario.query.get(sid)
        check("base_timeframe stored as 1m", sc.base_timeframe == "1m")
        check("available_timeframes stored",
              list(sc.available_timeframes) == ["1m", "5m", "15m", "30m", "1h", "4h"])
        check("history_bars is a whole number of anchor (15m) candles",
              sc.history_bars % TF_MINUTES["15m"] == 0)
        check("1m base count == days*bars_per_day", bar_provider.count(sc) == 2 * 390)

    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "itd"}).get_json()
    check("start_session advertises the timeframes",
          s["base_timeframe"] == "1m" and "15m" in s["available_timeframes"]
          and s["anchor_tf"] == "15m")

    full_1m = client.get(f"/sessions/{s['session_id']}/bars?tf=1m").get_json()
    tf15 = client.get(f"/sessions/{s['session_id']}/bars?tf=15m").get_json()
    check("1m endpoint serves the full base series", len(full_1m) == 2 * 390)
    check("15m endpoint aggregates it", len(tf15) == math.ceil(len(full_1m) / 15))
    check("aggregated candles are valid OHLC",
          all(c["high"] >= max(c["open"], c["close"]) and
              c["low"] <= min(c["open"], c["close"]) for c in tf15))
    check("aggregated candles carry base_start/base_end mapping",
          all("base_start" in c and "base_end" in c for c in tf15))


def test_endpoint_reveal_is_authoritative_on_every_tf():
    """A capped reveal (up_to in base units) must cap every timeframe too."""
    sid = _mint_intraday(seed=717)
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "rev"}).get_json()
    reveal = 44
    tf15 = client.get(f"/sessions/{s['session_id']}/bars?tf=15m&up_to={reveal}").get_json()
    check("no 15m candle exceeds the base reveal point",
          all(c["base_end"] <= reveal for c in tf15))
    check("exactly the buckets up to the reveal are present",
          len(tf15) == reveal // 15 + 1)


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
