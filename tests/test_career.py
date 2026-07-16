import os, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app, db
from app.models.progress import UserProgress
from app.models.mission import Mission, MissionAttempt
app = create_app(); client = app.test_client()

def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond: raise AssertionError(name)

def test_new_user_is_rookie():
    u = "car_" + uuid.uuid4().hex[:8]
    c = client.get(f"/career/{u}").get_json()
    check("new user is level 1", c["level"] == 1 and c["key"] == "market_rookie")
    check("rookie tools empty", c["unlocked_tools"] == [])
    # entry level trades equities only ("stocks"/"equity" are the same market);
    # crypto/forex/indices/commodities stay locked.
    check("rookie has the equities market", "stocks" in c["unlocked_markets"])
    check("rookie has only entry-level markets",
          set(c["unlocked_markets"]) <= {"stocks", "equity"})
    check("next level shown with requirements", c["next"]["level"] == 2 and len(c["next"]["requirements"]) >= 1)

def test_progress_advances_career():
    u = "car_" + uuid.uuid4().hex[:8]
    with app.app_context():
        m = Mission(slug="car-"+uuid.uuid4().hex[:6], title="t", rules=[], xp_reward=10)
        db.session.add(m); db.session.flush()
        db.session.add(MissionAttempt(mission_id=m.id, user_id=u, passed=True))
        p = UserProgress(user_id=u, completed_lessons=[], unlocked_scenario_tiers=[1],
                         total_scenarios_completed=3, sessions_scored=3,
                         total_trades_all=10, trades_with_stops_all=9, discipline_sum=240.0)
        db.session.add(p); db.session.commit()
    c = client.get(f"/career/{u}").get_json()
    check("meets level 2 (Junior Trader)", c["level"] >= 2)
    check("sl_tp unlocked at L2+", "sl_tp" in c["unlocked_tools"])
    t = client.get(f"/config/tools/{u}").get_json()
    check("config/tools follows career level", t["tool_level"] == c["level"])

TESTS=["test_new_user_is_rookie","test_progress_advances_career"]
if __name__=="__main__":
    failed=0
    for name in TESTS:
        print(name)
        try: globals()[name]()
        except AssertionError: failed+=1
        except Exception as e: print("  ERROR",e); failed+=1
    print(f"\n{'ALL PASSED' if failed==0 else str(failed)+' FAILED'} ({len(TESTS)} tests)")
    sys.exit(1 if failed else 0)
