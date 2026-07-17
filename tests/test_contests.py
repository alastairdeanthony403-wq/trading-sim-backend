"""Weekly contest + anti-cheat tests (Phase G step 1).

Requires a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_contests.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _u():
    return "c_" + uuid.uuid4().hex[:8]


def _start(user):
    return client.post("/contests/current/start", json={"user_id": user}).get_json()


def test_current_contest_has_scenario():
    c = client.get("/contests/current").get_json()
    check("contest has a scenario", bool(c["scenario_id"]))
    check("contest has bars", c["bar_count"] > 0)
    check("contest is titled", bool(c["title"]))


def test_anti_cheat_never_leaks_future_bars():
    s = _start(_u())
    sid, served = s["session_id"], s["bars_served"]
    # ask for the entire array — must be clamped to the served window
    bars = client.get(f"/sessions/{sid}/bars?up_to=100000").get_json()
    check("bars clamped to the served window", len(bars) == served + 1)
    # advancing reveals exactly ONE more bar, regardless of requested bar
    res = client.post(f"/sessions/{sid}/advance", json={"bar_sequence": 100000}).get_json()
    check("advance reveals exactly one new bar", res["bars_served"] == served + 1)
    bars2 = client.get(f"/sessions/{sid}/bars?up_to=100000").get_json()
    check("still cannot see beyond the new high-water", len(bars2) == served + 2)


def test_submit_requires_display_name():
    s = _start(_u())
    r = client.post(f"/contests/{s['contest_id']}/submit",
                    json={"session_id": s["session_id"], "user_id": "x"})
    check("submitting without a name is rejected", r.status_code == 400)


def test_one_scored_attempt_per_user():
    u = _u()
    s = _start(u)
    cid, sid = s["contest_id"], s["session_id"]
    r1 = client.post(f"/contests/{cid}/submit",
                     json={"session_id": sid, "user_id": u, "display_name": "Solo"}).get_json()
    check("first submit is scored", r1["already_entered"] is False)
    # a second attempt (new session) does not create a duplicate entry
    s2 = _start(u)
    r2 = client.post(f"/contests/{cid}/submit",
                     json={"session_id": s2["session_id"], "user_id": u, "display_name": "Solo"}).get_json()
    check("second submit is blocked as already entered", r2["already_entered"] is True)


def test_two_players_rank_on_the_leaderboard():
    cid = client.get("/contests/current").get_json()["contest_id"]
    for name, direction in [("Ada", "long"), ("Bo", "short")]:
        u = _u()
        s = _start(u)
        sid = s["session_id"]
        # take one trade on an already-revealed bar, then play to the end
        client.post(f"/sessions/{sid}/trades",
                    json={"direction": direction, "size": 10, "bar_sequence": 5})
        for _ in range(140):
            r = client.post(f"/sessions/{sid}/advance", json={"bar_sequence": 100000}).get_json()
            if r.get("status") in ("blown", "complete"):
                break
        client.post(f"/contests/{cid}/submit",
                    json={"session_id": sid, "user_id": u, "display_name": name})
    board = client.get(f"/contests/{cid}/leaderboard").get_json()
    check("leaderboard has entries", len(board) >= 2)
    check("ranks are sequential from 1", [e["rank"] for e in board[:2]] == [1, 2])
    scores = [e["composite_score"] for e in board]
    check("leaderboard is sorted by score (desc)",
          all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)))
    check("entries carry display names", all(e["display_name"] for e in board))


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
