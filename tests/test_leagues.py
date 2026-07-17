"""Private league tests (Phase G step 2).

Requires a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_leagues.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.competition import Contest, ContestEntry

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _u():
    return "l_" + uuid.uuid4().hex[:8]


def test_create_and_join_by_code():
    owner = _u()
    created = client.post("/leagues", json={"name": "Desk A", "user_id": owner,
                                            "display_name": "Owner"}).get_json()
    check("league created with an invite code", bool(created["invite_code"]))
    check("owner counted as first member", created["member_count"] == 1)

    friend = _u()
    joined = client.post("/leagues/join", json={"invite_code": created["invite_code"],
                                                "user_id": friend, "display_name": "Friend"}).get_json()
    check("friend joins the same league", joined["league_id"] == created["league_id"])
    check("member count grows to 2", joined["member_count"] == 2)


def test_bad_code_and_validation():
    r = client.post("/leagues/join", json={"invite_code": "ZZZZZZ", "user_id": _u(),
                                           "display_name": "X"})
    check("unknown code is 404", r.status_code == 404)
    r2 = client.post("/leagues", json={"user_id": _u(), "display_name": "X"})
    check("missing name is rejected", r2.status_code == 400)


def test_duplicate_join_is_idempotent():
    owner = _u()
    lg = client.post("/leagues", json={"name": "Dupe", "user_id": owner,
                                       "display_name": "O"}).get_json()
    u = _u()
    client.post("/leagues/join", json={"invite_code": lg["invite_code"], "user_id": u, "display_name": "U"})
    again = client.post("/leagues/join", json={"invite_code": lg["invite_code"], "user_id": u, "display_name": "U"}).get_json()
    check("re-joining does not duplicate membership", again["member_count"] == 2)


def test_leaderboard_aggregates_weekly_results():
    owner, friend = _u(), _u()
    lg = client.post("/leagues", json={"name": "Season", "user_id": owner,
                                       "display_name": "Owner"}).get_json()
    client.post("/leagues/join", json={"invite_code": lg["invite_code"],
                                       "user_id": friend, "display_name": "Friend"})
    # seed contest entries directly with known scores
    with app.app_context():
        c = Contest.query.first()
        cid = c.id
        db.session.add(ContestEntry(contest_id=cid, user_id=owner, display_name="Owner", composite_score=30.0))
        db.session.add(ContestEntry(contest_id=cid, user_id=friend, display_name="Friend", composite_score=55.0))
        db.session.commit()
    board = client.get(f"/leagues/{lg['league_id']}/leaderboard").get_json()
    check("both members ranked", len(board) == 2)
    check("higher total ranks first", board[0]["display_name"] == "Friend" and board[0]["rank"] == 1)
    check("scores aggregated", board[0]["total_score"] >= board[1]["total_score"])
    check("play counts reported", all("contests_played" in r for r in board))


def test_my_leagues_lists_membership():
    u = _u()
    client.post("/leagues", json={"name": "Mine", "user_id": u, "display_name": "Me"})
    mine = client.get(f"/leagues/mine?user_id={u}").get_json()
    check("user's leagues are listed", any(l["name"] == "Mine" for l in mine))


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    # ensure at least one contest exists for the aggregation test
    client.get("/contests/current")
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
