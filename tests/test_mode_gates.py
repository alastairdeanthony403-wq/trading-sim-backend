"""Career gates on Paper trading and Ranked.

Career is the way in: paper practice opens at level 2 and ranked at level 3.
The gate is enforced server-side, so a locked mode can't be started by calling
the API directly — hiding the button is not the mechanism.

Requires a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../db python tests/test_mode_gates.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from careerhelp import promote, level_of


def _u(tag):
    """Fresh user per run — the gate depends on stored progress."""
    return f"{tag}_{uuid.uuid4().hex[:8]}"

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_new_player_is_locked_out_of_both_modes():
    uid = _u("gate_rookie")
    r = client.post("/paper/start", json={"user_id": uid, "duration_minutes": 5})
    check("a brand-new player cannot start paper trading", r.status_code == 403)
    body = r.get_json()
    check("the refusal names the mode and the level needed",
          body["error"] == "mode_locked" and body["min_level"] == 2 and body["min_level_name"])
    rc = client.post("/contests/current/start", json={"user_id": uid})
    check("a brand-new player cannot start a ranked run", rc.status_code == 403)
    check("ranked needs a higher level than paper", rc.get_json()["min_level"] == 3)


def test_career_progress_opens_paper_then_ranked():
    uid = _u("gate_climber")
    promote(app, uid, level=2)
    check("career level rises with recorded progress", level_of(app, uid) >= 2)
    r = client.post("/paper/start", json={"user_id": uid, "duration_minutes": 5})
    check("paper opens at level 2", r.status_code == 200)
    rc = client.post("/contests/current/start", json={"user_id": uid})
    check("ranked is still locked at level 2", rc.status_code == 403)

    promote(app, uid, level=3)
    check("career reaches level 3", level_of(app, uid) >= 3)
    rc2 = client.post("/contests/current/start", json={"user_id": uid})
    check("ranked opens at level 3", rc2.status_code == 200)


def test_career_reports_mode_unlocks():
    uid = _u("gate_view")
    m = client.get(f"/career/{uid}").get_json()["modes"]
    check("career reports both modes locked for a new player",
          m["paper"]["unlocked"] is False and m["ranked"]["unlocked"] is False)
    check("career reports what each mode needs",
          m["paper"]["min_level"] == 2 and m["ranked"]["min_level"] == 3)
    promote(app, uid, level=3)
    m2 = client.get(f"/career/{uid}").get_json()["modes"]
    check("career reports both modes unlocked once earned",
          m2["paper"]["unlocked"] is True and m2["ranked"]["unlocked"] is True)


def test_career_scenarios_are_never_gated():
    """Career mode itself must always be playable — it's the way in."""
    scenarios = client.get("/scenarios").get_json()
    if not scenarios:
        print("  ..  no scenarios seeded; skipping")
        return
    r = client.post(f"/scenarios/{scenarios[0]['id']}/start",
                    json={"user_id": _u("gate_rookie2"), "starting_balance": 10000})
    check("a brand-new player can always start a career scenario", r.status_code == 200)


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
