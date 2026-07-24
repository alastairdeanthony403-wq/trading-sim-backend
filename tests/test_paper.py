"""Paper Trading mode (Phase 2) — timed practice on the intraday engine.

The reveal is governed by the server wall clock. These tests exercise the clock
math by backdating started_at (no real waiting): they verify the analysis→live
phases, the elapsed-time reveal cap, no future leak, timeframe aggregation on the
revealed window, reopen-resume, and the results/journal at 5 and 60 minutes.

Requires a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../db python tests/test_paper.py
"""
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, bar_provider
from app.models.session import Session, PaperSession
from app.models.scenario import Scenario
from app.routes.paper import BARS_PER_MINUTE, WARMUP_BARS

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _start(duration):
    return client.post("/paper/start", json={"user_id": "paper", "duration_minutes": duration}).get_json()


def _backdate(session_id, seconds):
    """Simulate `seconds` of elapsed live time by moving started_at into the past."""
    with app.app_context():
        m = PaperSession.query.filter_by(session_id=session_id).first()
        m.started_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        db.session.commit()


def test_invalid_duration_rejected():
    r = client.post("/paper/start", json={"user_id": "p", "duration_minutes": 7})
    check("a non-5-minute-step duration is rejected", r.status_code == 400)


def test_analysis_phase_shows_only_warmup():
    r = _start(15)
    sid = r["session_id"]
    check("live bar count = duration * bars_per_minute", r["live_bars"] == 15 * BARS_PER_MINUTE)
    check("total = warmup + live", r["total_bars"] == WARMUP_BARS + 15 * BARS_PER_MINUTE)
    bars = client.get(f"/sessions/{sid}/bars?up_to=999999").get_json()
    check("analysis phase reveals only the warm-up block (no live bars)",
          len(bars) == WARMUP_BARS and all(b["bar_sequence"] < WARMUP_BARS for b in bars))
    clk = client.get(f"/paper/{sid}/clock").get_json()
    check("clock reports the analysis phase", clk["phase"] == "analysis")


def test_go_live_then_clock_drives_reveal():
    r = _start(5)                                    # 5 min → 100 live bars
    sid = r["session_id"]
    client.post(f"/paper/{sid}/go-live")
    _backdate(sid, 150)                              # 2.5 minutes elapsed
    clk = client.get(f"/paper/{sid}/clock").get_json()
    expected = (WARMUP_BARS - 1) + int(150 * BARS_PER_MINUTE / 60)   # +50
    check("phase is live after go-live", clk["phase"] == "live")
    check("revealed bar index follows the wall clock", clk["bars_served"] == expected)
    bars = client.get(f"/sessions/{sid}/bars?up_to=999999").get_json()
    check("bars endpoint serves exactly the clock-revealed window",
          len(bars) == expected + 1 and all(b["bar_sequence"] <= expected for b in bars))
    check("remaining seconds counts down", clk["remaining_seconds"] == 5 * 60 - 150)


def test_no_future_leak_before_time():
    r = _start(10)
    sid = r["session_id"]
    client.post(f"/paper/{sid}/go-live")
    _backdate(sid, 60)                               # only 1 minute in
    bars = client.get(f"/sessions/{sid}/bars?up_to=999999").get_json()
    revealed = (WARMUP_BARS - 1) + BARS_PER_MINUTE   # +20
    check("pulling up_to=huge cannot reveal future bars", len(bars) == revealed + 1)
    with app.app_context():
        total = bar_provider.count(Scenario.query.get(r["scenario_id"]))
    check("only a fraction of the full series is visible one minute in", revealed + 1 < total)


def test_reopen_resumes_from_server_clock():
    r = _start(20)
    sid = r["session_id"]
    client.post(f"/paper/{sid}/go-live")
    _backdate(sid, 120)
    a = client.get(f"/paper/{sid}/clock").get_json()["bars_served"]
    # "reopen" later: more wall-clock has elapsed — the cursor advances on its own
    _backdate(sid, 300)
    b = client.get(f"/paper/{sid}/clock").get_json()["bars_served"]
    check("the market kept running while away (cursor advanced)", b > a)
    check("cursor matches 5 minutes of elapsed time",
          b == (WARMUP_BARS - 1) + int(300 * BARS_PER_MINUTE / 60))


def test_aggregation_correct_on_revealed_window():
    r = _start(30)
    sid = r["session_id"]
    client.post(f"/paper/{sid}/go-live")
    _backdate(sid, 600)                              # 10 minutes → 200 live bars
    cap = client.get(f"/paper/{sid}/clock").get_json()["bars_served"]
    m1 = client.get(f"/sessions/{sid}/bars?tf=1m&up_to=999999").get_json()
    m15 = client.get(f"/sessions/{sid}/bars?tf=15m&up_to=999999").get_json()
    check("1m feed is exactly the revealed window", len(m1) == cap + 1)
    check("15m aggregates only revealed 1m bars (no future leak up-timeframe)",
          all(c["base_end"] <= cap for c in m15) and len(m15) == math.ceil((cap + 1) / 15))


def _full_run(duration):
    r = _start(duration)
    sid = r["session_id"]
    # trade during the warm-up window, then let the whole session elapse
    price = client.get(f"/sessions/{sid}/bars").get_json()[WARMUP_BARS - 2]["close"]
    client.post(f"/sessions/{sid}/trades", json={
        "direction": "long", "size": 10, "bar_sequence": WARMUP_BARS - 2,
        "stop_loss": round(price * 0.99, 4)})
    client.post(f"/paper/{sid}/go-live")
    _backdate(sid, duration * 60 + 10)               # past the end
    clk = client.get(f"/paper/{sid}/clock").get_json()
    check(f"[{duration}m] the live window completes on the server clock", clk["live_done"] is True)
    res = client.post(f"/paper/{sid}/end").get_json()
    check(f"[{duration}m] end returns a scored result", "score_composite" in res)
    replay = client.get(f"/sessions/{sid}/replay").get_json()
    check(f"[{duration}m] replay/coach render for the paper session",
          "performance" in replay and "coach" in replay)
    return sid


def test_full_run_5_and_60_minutes():
    _full_run(5)
    _full_run(60)


def test_paper_is_not_career_gated_and_not_on_leaderboard():
    from app.models.progress import Leaderboard, UserProgress
    uid = "paper_lowstakes"
    r = client.post("/paper/start", json={"user_id": uid, "duration_minutes": 5}).get_json()
    sid = r["session_id"]
    client.post(f"/paper/{sid}/go-live")
    _backdate(sid, 400)
    client.post(f"/paper/{sid}/end")
    with app.app_context():
        sc = Session.query.get(sid)
        check("paper session is tagged mode=paper", sc.mode == "paper")
        rows = Leaderboard.query.filter_by(scenario_id=sc.scenario_id).count()
        check("paper never posts to a leaderboard", rows == 0)
        prog = UserProgress.query.filter_by(user_id=uid).first()
        check("paper does not create/inflate career progress", prog is None)


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
