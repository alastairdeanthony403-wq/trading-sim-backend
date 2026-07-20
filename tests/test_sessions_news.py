"""Intraday trading sessions + news (Phase 4).

Sessions give the intraday day a volatility rhythm (active open/close, quiet
midday, or the FX handover); news schedules releases at session opens whose price
reaction is baked into the seed-only series and surfaced through the existing
event/ticker/voice pipeline.

Requires a Postgres DATABASE_URL + SETUP_KEY:
    DATABASE_URL=postgresql://.../db SETUP_KEY=testkey python tests/test_sessions_news.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, bar_provider
from app import synthetic as syn
from app.models.scenario import Scenario
from app.models.event import ScenarioEvent

app = create_app()
client = app.test_client()
KEY = {"X-Setup-Key": os.environ.get("SETUP_KEY", "")}


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_session_vol_curve_shape():
    bpd = 390
    curve = syn._session_vol_curve(bpd, bpd, "equity")
    open_v = curve[5]
    mid_v = curve[int(bpd * 0.45)]
    close_v = curve[bpd - 3]
    check("open is more volatile than midday", open_v > mid_v)
    check("close is more volatile than midday", close_v > mid_v)
    check("the curve repeats each day", syn._session_vol_curve(2 * bpd, bpd, "equity")[bpd + 5] == open_v)


def test_session_lookup_and_bands():
    name, vol = syn.session_at(0.01, "equity")
    check("the first fraction is the Open session", name == "Open")
    check("fx profile has a London/NY overlap band",
          any(b["name"] == "LDN/NY" for b in syn.session_bands("fx")))
    check("bands cover the whole day", abs(syn.session_bands("equity")[-1]["end"] - 1.0) < 1e-9)


def test_generated_intraday_breathes_with_sessions():
    """Over many days, average bar range near the open should exceed midday."""
    bars = syn.generate_intraday_series(seed=2024, days=6, bars_per_day=390,
                                        regime="range", session_profile="equity")
    bpd = 390
    def avg_range(lo, hi):
        rs = [(b["high"] - b["low"]) for k, b in enumerate(bars) if lo <= (k % bpd) < hi]
        return sum(rs) / len(rs)
    open_rng = avg_range(0, int(bpd * 0.08))
    mid_rng = avg_range(int(bpd * 0.30), int(bpd * 0.62))
    check("open-session bars are wider than midday bars on average", open_rng > mid_rng * 1.2)


def test_intraday_determinism_with_sessions_and_news():
    events = [{"bar": 120, "category": "earnings", "headline": "beat", "detail": "d",
               "sentiment": 1, "impact": 0.02}]
    a = syn.generate_intraday_series(seed=77, days=2, bars_per_day=200,
                                     session_profile="fx", events=events)
    b = syn.generate_intraday_series(seed=77, days=2, bars_per_day=200,
                                     session_profile="fx", events=events)
    check("same seed + profile + events regenerate identically", a == b)


def test_news_reaction_is_baked_into_price():
    """A scheduled release moves the event bar and perturbs everything after it,
    while bars before it are untouched (same seed, with vs without news)."""
    ev_bar = 150
    events = [{"bar": ev_bar, "category": "scandal", "headline": "probe",
               "detail": "d", "sentiment": -1, "impact": 0.02}]
    plain = syn.generate_intraday_series(seed=909, days=2, bars_per_day=200,
                                         session_profile="equity")
    news = syn.generate_intraday_series(seed=909, days=2, bars_per_day=200,
                                        session_profile="equity", events=events)
    check("bars before the release are identical", plain[:ev_bar] == news[:ev_bar])
    check("the release changes the series from the event bar on", plain[ev_bar] != news[ev_bar])
    move = abs(math.log(news[ev_bar]["close"] / news[ev_bar]["open"]))
    typical = sorted(abs(math.log(b["close"] / b["open"])) for b in news[:ev_bar])[len(news[:ev_bar]) // 2]
    check("the event bar's move is much larger than a typical bar", move > 5 * typical)


def test_news_toggle_endpoint():
    on = client.post("/setup/generate-intraday-scenarios",
                     json={"regimes": ["range"], "per_regime": 1, "seed": 5,
                           "days": 3, "bars_per_day": 200, "news": 2,
                           "session_profile": "equity"}, headers=KEY).get_json()["results"][0]
    off = client.post("/setup/generate-intraday-scenarios",
                      json={"regimes": ["range"], "per_regime": 1, "seed": 6,
                            "days": 3, "bars_per_day": 200, "news": 0}, headers=KEY).get_json()["results"][0]
    check("news scenario reports scheduled releases", on["news"] == 2)
    check("no-news scenario schedules none", off["news"] == 0)
    with app.app_context():
        on_rows = ScenarioEvent.query.filter_by(scenario_id=on["scenario_id"]).count()
        off_rows = ScenarioEvent.query.filter_by(scenario_id=off["scenario_id"]).count()
        on_sc = Scenario.query.get(on["scenario_id"])
        check("news scenario persisted event rows for the ticker", on_rows == 2)
        check("no-news scenario has no event rows", off_rows == 0)
        check("news scenario is tagged 'news'", "news" in (on_sc.tags or []))
        check("events are stored in gen_params so bars regenerate the reaction",
              len(on_sc.gen_params.get("events", [])) == 2)


def test_bar_provider_serves_intraday_with_events():
    """The JSON cache key handles the nested events list; bars serve + are valid."""
    r = client.post("/setup/generate-intraday-scenarios",
                    json={"regimes": ["trend_up"], "per_regime": 1, "seed": 8,
                          "days": 2, "bars_per_day": 200, "news": 3}, headers=KEY).get_json()
    sid = r["results"][0]["scenario_id"]
    with app.app_context():
        sc = Scenario.query.get(sid)
        s1 = bar_provider.series(sc)
        bar_provider._generated.cache_clear()
        s2 = bar_provider.series(sc)
        check("intraday-with-news series is served and cached deterministically", s1 == s2)
        check("count is days*bars_per_day", len(s1) == 2 * 200)
        check("bars are valid OHLC", all(b.high >= max(b.open, b.close) and
                                         b.low <= min(b.open, b.close) for b in s1))
    # start_session advertises the session context
    st = client.post(f"/scenarios/{sid}/start", json={"user_id": "sn"}).get_json()
    check("start_session returns the session profile + bands",
          st["session_profile"] == "equity" and len(st["session_bands"]) == 5
          and st["bars_per_day"] == 200)
    events = client.get(f"/sessions/{st['session_id']}/events").get_json()
    check("the events endpoint serves the releases with character voices",
          len(events) == 3 and all(e["voices"] for e in events))


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
