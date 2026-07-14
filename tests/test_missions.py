"""Mission engine tests (Phase B).

    DATABASE_URL=postgresql://.../trading_sim_dev SETUP_KEY=testkey \
        python tests/test_missions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SETUP_KEY", "testkey")

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar

app = create_app()
client = app.test_client()
H = {"X-Setup-Key": os.environ["SETUP_KEY"]}


def _scenario(bars):
    with app.app_context():
        s = Scenario(name_internal="mn", asset_class="crypto", timeframe="1h",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate(bars):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


# a gently rising market so a simple long finishes green
RISE = [(100, 101, 99, 100), (101, 103, 100, 102), (103, 105, 102, 104), (105, 107, 104, 106)]


def _mission_id(slug):
    for m in client.get("/missions").get_json():
        if m["slug"] == slug:
            return m["id"]
    return None


def test_seed_and_list():
    r = client.post("/setup/seed-missions", headers=H)
    check("seed authorized + ok", r.status_code == 200 and r.get_json()["total"] >= 10)
    check("seed is idempotent", client.post("/setup/seed-missions", headers=H).status_code == 200)
    check("seed rejects without key", client.post("/setup/seed-missions").status_code == 401)
    missions = client.get("/missions").get_json()
    check("missions listed", len(missions) >= 10)


def test_daily_is_deterministic():
    a = client.get("/missions/daily?user_id=x").get_json()
    b = client.get("/missions/daily?user_id=x").get_json()
    check("daily mission is stable within a day", a["mission"]["id"] == b["mission"]["id"])
    check("daily reports a date + streak", "date" in a and "streak" in a)


def test_mission_pass():
    sid = _scenario(RISE)
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "p"}).get_json()["session_id"]
    # disciplined winning trade: small risk + a stop
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0, "stop_loss": 99}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 3})
    client.post(f"/sessions/{s}/end")
    mid = _mission_id("small-risk")
    r = client.post(f"/missions/{mid}/submit", json={"session_id": s, "user_id": "p"}).get_json()
    check("disciplined session passes 'small-risk'", r["passed"] is True)
    check("passing awards XP", r["xp_awarded"] > 0)


def test_mission_fail_no_stop():
    sid = _scenario(RISE)
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "f"}).get_json()["session_id"]
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0}).get_json()  # no stop
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 3})
    client.post(f"/sessions/{s}/end")
    mid = _mission_id("small-risk")
    r = client.post(f"/missions/{mid}/submit", json={"session_id": s, "user_id": "f"}).get_json()
    check("no-stop session fails 'small-risk'", r["passed"] is False)
    check("no XP on failure", r["xp_awarded"] == 0)
    check("failing rule is reported", any(not x["passed"] for x in r["results"]))


def test_live_status_and_streak():
    sid = _scenario(RISE)
    s = client.post(f"/scenarios/{sid}/start", json={"user_id": "d"}).get_json()["session_id"]
    mid = _mission_id("first-stops")
    live = client.get(f"/sessions/{s}/mission/{mid}/status").get_json()
    check("live status returns rule results", isinstance(live["results"], list) and len(live["results"]) >= 1)
    # complete a daily and confirm streak increments
    t = client.post(f"/sessions/{s}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0, "stop_loss": 99}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 3})
    client.post(f"/sessions/{s}/end")
    client.post(f"/missions/{mid}/submit", json={"session_id": s, "user_id": "d", "is_daily": True})
    day = client.get("/missions/daily?user_id=d").get_json()
    check("daily streak counts a passed day", day["streak"] >= 1)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    # ordered so seeding runs first
    order = ["test_seed_and_list", "test_daily_is_deterministic", "test_mission_pass",
             "test_mission_fail_no_stop", "test_live_status_and_streak"]
    failed = 0
    for name in order:
        print(name)
        try:
            globals()[name]()
        except AssertionError:
            failed += 1
        except Exception as e:
            print(f"  ERROR {e}"); failed += 1
    print(f"\n{'ALL PASSED' if failed == 0 else str(failed) + ' FAILED'} ({len(order)} tests)")
    sys.exit(1 if failed else 0)
