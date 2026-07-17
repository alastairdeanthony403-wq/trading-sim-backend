"""Scam / pump-and-dump scenario tests (Phase E step 3).

Generator tests need no DB; the debrief/API tests use a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_scam.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.synthetic import build_scam_scenario

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_pump_then_rug_shape():
    bars, events = build_scam_scenario(seed=1, n_bars=120)
    closes = [b["close"] for b in bars]
    peak = max(closes)
    peak_i = closes.index(peak)
    start = bars[0]["open"]
    final = closes[-1]
    check("price pumps to a big peak (>2x)", peak > 2 * start)
    check("then rugs well below the peak (<50%)", final < 0.5 * peak)
    check("the rug comes after the pump", peak_i > len(bars) * 0.4)


def test_pump_is_on_thin_volume():
    bars, _ = build_scam_scenario(seed=2, n_bars=120)
    base = [b["volume"] for b in bars[:30]]
    pump = [b["volume"] for b in bars[40:70]]
    check("pump volume is thinner than the base", sum(pump) / len(pump) < sum(base) / len(base))


def test_events_and_determinism():
    a = build_scam_scenario(seed=5, n_bars=120)
    b = build_scam_scenario(seed=5, n_bars=120)
    _, events = a
    hype = [e for e in events if e["category"] == "hype"]
    rug = [e for e in events if e["category"] == "rug"]
    check("escalating hype posts present", len(hype) >= 3)
    check("exactly one rug marker", len(rug) == 1)
    check("hype posts carry a promoter handle", all(e["detail"].startswith("@") for e in hype))
    check("deterministic for a seed", a == b)


def _make_scam():
    r = client.post("/setup/generate-scam-scenarios",
                    json={"count": 1, "n_bars": 120, "seed": 909},
                    headers={"X-Setup-Key": os.environ.get("SETUP_KEY", "")})
    return r.get_json()["results"][0]["scenario_id"]


def test_debrief_flags_taking_the_bait():
    sid = _make_scam()
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "s"}).get_json()["session_id"]
    events = client.get(f"/sessions/{s}/events").get_json()
    hype_bar = next(e["bar_sequence"] for e in events if e["category"] == "hype")
    # buy into the hype and hold all the way through the rug
    client.post(f"/sessions/{s}/trades",
                json={"direction": "long", "size": 10, "bar_sequence": hype_bar})
    client.post(f"/sessions/{s}/advance", json={"bar_sequence": 119})
    d = client.get(f"/sessions/{s}/scam-debrief").get_json()
    check("recognised as a scam scenario", d["is_scam"] is True)
    check("holding into the rug is flagged as taking the bait", d["took_bait"] is True)
    check("verdict is took_bait", d["verdict"] == "took_bait")
    check("debrief teaches the anatomy", isinstance(d["anatomy"], list) and len(d["anatomy"]) >= 3)


def test_debrief_rewards_staying_out():
    sid = _make_scam()
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "s2"}).get_json()["session_id"]
    client.post(f"/sessions/{s}/advance", json={"bar_sequence": 119})   # never trades
    d = client.get(f"/sessions/{s}/scam-debrief").get_json()
    check("staying out is not taking the bait", d["took_bait"] is False)
    check("verdict is stayed_out", d["verdict"] == "stayed_out")


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
