"""Surprise spot checks in the learning path.

A spot check is the same concept-matched live market as an end-of-unit check,
but dropped in unannounced after a lesson. Which lessons trigger one is decided
server-side by a hash of (user, lesson): unpredictable to the learner, stable for
a given user, and impossible to re-roll by reloading. Passing retires it; failing
leaves it due so it can be retried on a fresh market.

Requires a Postgres DATABASE_URL:
    DATABASE_URL=postgresql://.../db python tests/test_spot_checks.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app import academy
from app.models.progress import UserProgress
from app.routes.progress import CURRICULUM

app = create_app()
client = app.test_client()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _u(tag="spot"):
    return f"{tag}_{uuid.uuid4().hex[:8]}"


ALL_LESSONS = [l for unit in CURRICULUM for l in unit["lessons"]]


def _complete(user_id, lessons):
    with app.app_context():
        from app.routes.progress import get_or_create_progress
        p = get_or_create_progress(user_id)
        p.completed_lessons = list(lessons)
        db.session.commit()


def _trigger_lesson(user_id):
    """The first lesson that owes this user a spot check."""
    return next((l for l in ALL_LESSONS
                 if academy.lesson_triggers_spot_check(user_id, l)), None)


def test_nothing_due_before_any_lesson_is_finished():
    uid = _u()
    r = client.get(f"/academy/spotcheck?user_id={uid}").get_json()
    check("a learner who has done nothing is never spot-checked", r["due"] is False)


def test_spot_checks_are_deterministic_and_unpredictable():
    uid = _u()
    a = [academy.lesson_triggers_spot_check(uid, l) for l in ALL_LESSONS]
    b = [academy.lesson_triggers_spot_check(uid, l) for l in ALL_LESSONS]
    check("the same user always gets the same trigger lessons", a == b)
    check("only some lessons trigger one", 0 < sum(a) < len(ALL_LESSONS))
    other = [academy.lesson_triggers_spot_check(_u(), l) for l in ALL_LESSONS]
    check("different learners get different trigger lessons", a != other or sum(a) == 0)


def test_a_finished_trigger_lesson_makes_one_due():
    uid = _u()
    lesson = _trigger_lesson(uid)
    if lesson is None:
        print("  ..  this user has no trigger lesson; skipping")
        return
    _complete(uid, [lesson])
    r = client.get(f"/academy/spotcheck?user_id={uid}").get_json()
    check("finishing a trigger lesson springs a spot check", r["due"] is True)
    check("the spot check names the lesson that sprang it", r["lesson_id"] == lesson)
    check("it carries a concept, goal and graded rules",
          r["concept"] in academy.CONCEPTS and r["goal"] and len(r["rules"]) > 0)
    again = client.get(f"/academy/spotcheck?user_id={uid}").get_json()
    check("reloading cannot re-roll it away", again["lesson_id"] == lesson)


def test_spot_check_tests_a_concept_already_taught():
    uid = _u()
    lesson = _trigger_lesson(uid)
    if lesson is None:
        print("  ..  no trigger lesson; skipping")
        return
    _complete(uid, [lesson])
    r = client.get(f"/academy/spotcheck?user_id={uid}").get_json()
    unit = next(u for u in CURRICULUM if lesson in u["lessons"])
    check("the concept is the one that lesson's unit teaches",
          r["concept"] == academy.CHECK_CONCEPT[unit["check"]])


def test_passing_retires_it_and_failing_keeps_it_due():
    uid = _u()
    lesson = _trigger_lesson(uid)
    if lesson is None:
        print("  ..  no trigger lesson; skipping")
        return
    _complete(uid, [lesson])
    due = client.get(f"/academy/spotcheck?user_id={uid}").get_json()

    # Fail it: grade a run with no trades at all (min_trades is always a rule).
    s = client.post("/academy/practice/start", json={
        "user_id": uid, "concept_tag": due["concept"], "spot_lesson": lesson}).get_json()
    g = client.post(f"/academy/practice/{s['session_id']}/grade").get_json()
    check("a run with no trades fails the check", g["passed"] is False)
    still = client.get(f"/academy/spotcheck?user_id={uid}").get_json()
    check("a failed spot check stays due for a retry", still["due"] is True and still["lesson_id"] == lesson)

    # Pass it: mark it directly through the same path a passing grade takes.
    with app.app_context():
        p = UserProgress.query.filter_by(user_id=uid).first()
        p.spot_checks_done = [lesson]
        db.session.commit()
    after = client.get(f"/academy/spotcheck?user_id={uid}").get_json()
    check("a passed spot check is retired",
          after["due"] is False or after.get("lesson_id") != lesson)


def test_retry_serves_a_fresh_market():
    uid = _u()
    a = client.post("/academy/practice/start",
                    json={"user_id": uid, "concept_tag": "risk_stops", "spot_lesson": "x"}).get_json()
    b = client.post("/academy/practice/start",
                    json={"user_id": uid, "concept_tag": "risk_stops", "spot_lesson": "x"}).get_json()
    check("each attempt mints a different market", a["scenario_id"] != b["scenario_id"])
    bars_a = client.get(f"/sessions/{a['session_id']}/bars?up_to=999999").get_json()
    check("only the warm-up block is revealed up front", len(bars_a) == a["warmup_bars"])


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
