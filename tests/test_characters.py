"""Character voices + coach psychology read (Phase E step 4).

Pure tests need no DB; the endpoint tests use a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_characters.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar
from app.characters import voices_for_context, voices_for_event
from app.coach import build_findings

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_context_voices_conflict():
    v = voices_for_context("after_stopout")
    stances = {x["stance"] for x in v}
    check("after a stop-out, two voices speak", len(v) == 2)
    check("they conflict (aggressive vs cautious)", {"aggressive", "cautious"} <= stances)
    check("each voice has a name + line", all(x["name"] and x["line"] for x in v))
    check("unknown context yields no voices", voices_for_context("nope") == [])


def test_event_voices_by_category():
    hype = voices_for_event("hype")
    rug = voices_for_event("rug")
    bear = voices_for_event("rate_decision", sentiment=-1)
    bull = voices_for_event("earnings", sentiment=1)
    check("hype pits guru vs analyst",
          {v["character"] for v in hype} == {"guru", "analyst"})
    check("rug warns (analyst + risk manager)",
          {v["character"] for v in rug} == {"analyst", "risk_manager"})
    check("bearish news: guru says buy the dip",
          any("dip" in v["line"].lower() for v in bear))
    check("bullish news: analyst warns about chasing",
          any("chas" in v["line"].lower() for v in bull))


def test_coach_psychology_read_names_the_impulse():
    discipline = {"no_stop_count": 0, "revenge_count": 2, "oversize_count": 0,
                  "discipline_score": 45, "trades_total": 3}
    replay = {"trades": [
        {"pnl": 50, "achieved_r": 1.0, "planned_r": 2.0, "direction": "long"},
    ]}
    findings = build_findings(None, discipline, replay)
    psych = [f for f in findings if "Psychology read" in f["text"]]
    check("a psychology read is produced", len(psych) == 1)
    check("it names revenge and the character", "revenge" in psych[0]["text"]
          and "Rex" in psych[0]["text"])
    check("it links to the psychology lesson", psych[0]["lesson_id"] == "psychology_discipline")


def test_advance_surfaces_voices_after_stopout():
    with app.app_context():
        s = Scenario(name_internal="v", asset_class="stocks", timeframe="1D",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate([(100, 100.5, 99.5, 100), (96, 96.5, 95.5, 96)]):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        sid = s.id
    sess = client.post(f"/scenarios/{sid}/start", json={"user_id": "v"}).get_json()["session_id"]
    client.post(f"/sessions/{sess}/trades",
                json={"direction": "long", "size": 10, "bar_sequence": 0, "stop_loss": 99})
    res = client.post(f"/sessions/{sess}/advance", json={"bar_sequence": 1}).get_json()
    check("a losing stop-out surfaces character voices", len(res.get("voices", [])) == 2)
    check("the voices tempt a revenge trade",
          any(v["stance"] == "aggressive" for v in res["voices"]))


def test_news_events_carry_voices():
    r = client.post("/setup/generate-news-scenarios",
                    json={"count": 1, "n_bars": 120, "seed": 4242},
                    headers={"X-Setup-Key": os.environ.get("SETUP_KEY", "")})
    sid = r.get_json()["results"][0]["scenario_id"]
    sess = client.post(f"/scenarios/{sid}/start", json={"user_id": "v2"}).get_json()["session_id"]
    ev = client.get(f"/sessions/{sess}/events").get_json()
    check("every headline carries conflicting voices",
          all(len(e.get("voices", [])) == 2 for e in ev))


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
