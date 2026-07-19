"""Seed-only synthetic scenarios + engine versioning (Phase 2).

Requires a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_seed_only.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, bar_provider
from app import engine
from app.models.scenario import Scenario, ScenarioBar

app = create_app()
client = app.test_client()
KEY = {"X-Setup-Key": os.environ.get("SETUP_KEY", "")}


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_generation_persists_no_bars_but_serves_them():
    r = client.post("/setup/generate-scenarios",
                    json={"regimes": ["trend_up"], "per_regime": 1,
                          "history_bars": 300, "playback_bars": 160}, headers=KEY).get_json()
    sid = r["results"][0]["scenario_id"]
    with app.app_context():
        rows = ScenarioBar.query.filter_by(scenario_id=sid).count()
        sc = Scenario.query.get(sid)
        served = bar_provider.count(sc)
        check("generated scenario stores ZERO bar rows", rows == 0)
        check("but serves the full generated series", served == 460)
        check("engine_version + seed persisted", sc.engine_version == "v2" and sc.seed is not None)
    # the play pipeline sees them
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "so"}).get_json()
    bars = client.get(f"/sessions/{s['session_id']}/bars").get_json()
    check("bars endpoint serves the generated series", len(bars) == 460)
    check("bars are valid OHLC", all(b["high"] >= max(b["open"], b["close"]) and
                                     b["low"] <= min(b["open"], b["close"]) for b in bars))


def test_regeneration_is_deterministic():
    a = client.post("/setup/generate-scenarios",
                    json={"regimes": ["range"], "per_regime": 1, "seed": 5150,
                          "history_bars": 100, "playback_bars": 60}, headers=KEY).get_json()["results"][0]["scenario_id"]
    with app.app_context():
        s1 = bar_provider.series(Scenario.query.get(a))
        bar_provider._generated.cache_clear()   # force a fresh regeneration
        s2 = bar_provider.series(Scenario.query.get(a))
    check("same seed+version regenerate identical bars", s1 == s2)


def test_engine_version_pin_isolates_existing_scenarios():
    # A scenario minted at v2 must keep producing v2 bars even after the engine
    # changes — the guarantee that protects in-flight contests.
    with app.app_context():
        sc = Scenario(name_internal="pin", asset_class="synthetic", timeframe="1D",
                      difficulty_tier=1, is_active=True, engine_version="v2", seed=999,
                      gen_params={"kind": "regime", "n_bars": 80, "regime": "trend_up"})
        db.session.add(sc); db.session.commit()
        v2_bars = bar_provider.series(sc)

        # register a DIFFERENT engine v3 and make it "current"
        engine.ENGINES["v3"] = lambda p: [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}
                                          for _ in range(p["n_bars"])]
        bar_provider._generated.cache_clear()
        try:
            still = bar_provider.series(Scenario.query.get(sc.id))
            check("v2 scenario is unchanged by a new engine version", still == v2_bars)
            check("the v2 bars are NOT the flat v3 output", still[0].open != 1 or still[10].close != 1)
        finally:
            engine.ENGINES.pop("v3", None)
            bar_provider._generated.cache_clear()


def test_unknown_engine_version_errors():
    with app.app_context():
        sc = Scenario(name_internal="bad", asset_class="synthetic", timeframe="1D",
                      difficulty_tier=1, is_active=True, engine_version="v-nope", seed=1,
                      gen_params={"kind": "regime", "n_bars": 10, "regime": "range"})
        raised = False
        try:
            bar_provider.series(sc)
        except ValueError:
            raised = True
        check("unknown engine_version raises (never silently wrong)", raised)


def test_generated_scenario_full_playthrough():
    sid = client.post("/setup/generate-scenarios",
                      json={"regimes": ["trend_up"], "per_regime": 1, "seed": 321,
                            "history_bars": 40, "playback_bars": 20}, headers=KEY).get_json()["results"][0]["scenario_id"]
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "so2"}).get_json()["session_id"]
    # open at the last history bar, advance to the end, end + score
    client.post(f"/sessions/{s}/trades", json={"direction": "long", "size": 10, "bar_sequence": 39})
    res = client.post(f"/sessions/{s}/advance", json={"bar_sequence": 59}).get_json()
    end = client.post(f"/sessions/{s}/end").get_json()
    check("advance processed a generated scenario", isinstance(res.get("positions"), list))
    check("session scores end-to-end on a seed-only scenario", "score_composite" in end)


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
