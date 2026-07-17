"""News event system tests (Phase E step 2).

Generator-level tests need no DB; the endpoint/API tests use a Postgres
DATABASE_URL. Run:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_news.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.synthetic import build_news_scenario

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_builder_shape_and_determinism():
    bars_a, events_a = build_news_scenario(seed=100, n_bars=140)
    bars_b, events_b = build_news_scenario(seed=100, n_bars=140)
    check("140 bars produced", len(bars_a) == 140)
    check("3–5 events scheduled", 3 <= len(events_a) <= 5)
    check("events deterministic for a seed", events_a == events_b and bars_a == bars_b)
    check("events carry a headline + category",
          all(e.get("headline") and e.get("category") for e in events_a))
    check("event bars are within range",
          all(0 <= e["bar"] < 140 for e in events_a))


def test_events_move_the_price_in_their_direction():
    # Across many seeds, the return on each event bar should line up with the
    # event's sentiment (a bearish headline pushes price down, and vice-versa).
    signed = []
    bigger = 0
    total = 0
    for seed in range(12):
        bars, events = build_news_scenario(seed=seed, n_bars=140)
        moves = [abs(bars[i]["close"] / bars[i - 1]["close"] - 1) for i in range(1, len(bars))]
        med = sorted(moves)[len(moves) // 2]
        for e in events:
            b = e["bar"]
            if b == 0:
                continue
            ret = bars[b]["close"] / bars[b - 1]["close"] - 1
            signed.append(e["sentiment"] * ret)
            total += 1
            if abs(ret) > med:
                bigger += 1
    mean_signed = sum(signed) / len(signed)
    check("reactions align with sentiment on average", mean_signed > 0)
    check("most event bars move more than a typical bar", bigger / total > 0.6)


def test_events_endpoint_roundtrip():
    r = client.post("/setup/generate-news-scenarios",
                    json={"count": 1, "n_bars": 120, "seed": 555},
                    headers={"X-Setup-Key": os.environ.get("SETUP_KEY", "")})
    check("news scenario generated 200", r.status_code == 200)
    sid = r.get_json()["results"][0]["scenario_id"]
    # start a session on it and fetch its events
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "n"}).get_json()["session_id"]
    ev = client.get(f"/sessions/{s}/events").get_json()
    check("events returned for the session", isinstance(ev, list) and len(ev) >= 3)
    check("events ordered by bar", all(ev[i]["bar_sequence"] <= ev[i + 1]["bar_sequence"]
                                       for i in range(len(ev) - 1)))
    check("each event has headline + sentiment",
          all(e.get("headline") and "sentiment" in e for e in ev))


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
