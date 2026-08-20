"""Milestones (Phase 3) — honest criteria, idempotent unlocks, no mystery locks.

    DATABASE_URL=postgresql://.../trading_sim_dev SETUP_KEY=testkey \
        python tests/test_milestones.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.engagement import Milestone, UserMilestone
from app.models.scenario import Scenario, ScenarioBar
from app.engagement import milestones as ms
from app.engagement.feedback import feedback_payload
from app.engagement.service import award_xp, total_xp

app = create_app()
client = app.test_client()

UID = "mile_" + os.urandom(4).hex()
KEY = {"X-Setup-Key": os.environ.get("SETUP_KEY", "testkey")}


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _seed():
    return client.post("/setup/seed-milestones", headers=KEY).get_json()


DOWN = [(100, 101, 99, 100), (100, 100, 98, 99), (99, 99, 97, 98), (98, 98, 96, 97)]


def _scenario():
    with app.app_context():
        s = Scenario(name_internal="mile", asset_class="crypto", timeframe="1h",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate(DOWN):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


def _disciplined_session(user):
    sid = client.post(f"/scenarios/{_scenario()}/start",
                      json={"user_id": user}).get_json()["session_id"]
    t = client.post(f"/sessions/{sid}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0,
                          "stop_loss": 96}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    return sid, client.post(f"/sessions/{sid}/end").get_json()


# ── 1. the catalogue ───────────────────────────────────────────────────────
def test_seeding_is_idempotent():
    with app.app_context():
        first = _seed()
        check("seed reports the full catalogue", first["total"] == len(ms.SEED_MILESTONES))
        second = _seed()
        check("re-seeding creates nothing new", second["created"] == 0)
        check("re-seeding updates in place", second["updated"] == second["total"])
        check("no duplicate rows",
              Milestone.query.count() == len(ms.SEED_MILESTONES))


def test_every_milestone_states_its_criteria():
    """The core anti-mystery guarantee."""
    with app.app_context():
        _seed()
        for m in Milestone.query.all():
            text = ms.criteria_text(m.threshold_type, m.threshold_value)
            check(f"{m.code}: criteria is real text",
                  bool(text) and m.threshold_type not in text)
            check(f"{m.code}: threshold_type is a known metric",
                  m.threshold_type in ms.METRICS)
            check(f"{m.code}: has a description", bool((m.description or "").strip()))


def test_catalogue_leans_on_discipline_and_craft():
    with app.app_context():
        _seed()
        counts = {}
        for m in Milestone.query.all():
            counts[m.category] = counts.get(m.category, 0) + 1
        total = sum(counts.values())
        heavy = counts.get("discipline", 0) + counts.get("craft", 0)
        print(f"       {counts}")
        check("every category is populated", set(counts) ==
              {"learning", "discipline", "consistency", "craft"})
        check("discipline + craft are the majority", heavy > total / 2)


def test_no_profitability_language_in_milestone_copy():
    banned = ["profit", "p&l", "money", "rich", "returns", "win rate", "payout",
              "earnings", "beat the market"]
    with app.app_context():
        _seed()
        offences = []
        for m in Milestone.query.all():
            blob = f"{m.name} {m.description}".lower()
            offences += [(m.code, p) for p in banned if p in blob]
        for code, phrase in offences:
            print(f"       {code}: {phrase!r}")
        check("no milestone implies money made", not offences)


# ── 2. unlocking ───────────────────────────────────────────────────────────
def test_unlock_is_idempotent():
    with app.app_context():
        _seed()
        user = UID + "_idem"
        award_xp(user, "lesson_complete", "l1", 20, f"m:{user}:1")
        client.post(f"/progress/{user}/complete", json={"item_id": "how_markets_work"})

        first = {u.milestone_id for u in UserMilestone.query.filter_by(user_id=user).all()}
        check("something unlocked", len(first) > 0)

        for _ in range(3):
            ms.evaluate(user)          # evaluation is constant; must be safe
        after = UserMilestone.query.filter_by(user_id=user).all()
        check("no duplicate unlocks", len(after) == len(first))
        check("same set", {u.milestone_id for u in after} == first)


def test_first_lesson_unlocks_the_learning_milestone():
    with app.app_context():
        _seed()
        user = UID + "_lesson"
        r = client.post(f"/progress/{user}/complete",
                        json={"item_id": "how_markets_work"}).get_json()
        codes = [m["code"] for m in r["feedback"]["milestones_unlocked"]]
        check("first_lesson is reported in the feedback block",
              "first_lesson" in codes)
        unlocked = next(m for m in r["feedback"]["milestones_unlocked"]
                        if m["code"] == "first_lesson")
        check("the unlock carries its criteria", bool(unlocked["criteria"]))
        check("and is marked unlocked", unlocked["unlocked"] is True)


def test_an_unlock_is_reported_once():
    with app.app_context():
        _seed()
        user = UID + "_once"
        first = client.post(f"/progress/{user}/complete",
                            json={"item_id": "how_markets_work"}).get_json()
        check("reported on the action that earned it",
              len(first["feedback"]["milestones_unlocked"]) > 0)
        second = client.post(f"/progress/{user}/complete",
                             json={"item_id": "order_types"}).get_json()
        codes = [m["code"] for m in second["feedback"]["milestones_unlocked"]]
        check("not repeated on the next action", "first_lesson" not in codes)


def test_rereading_a_session_does_not_consume_an_unlock():
    """A results screen re-read must not swallow a celebration.

    The FIRST /end is the action that earned the unlock, so it claims and
    reports it — that's correct. What must never happen is a later re-read
    quietly consuming an unlock the learner hasn't been shown.
    """
    with app.app_context():
        _seed()
        user = UID + "_reread"
        sid = client.post(f"/scenarios/{_scenario()}/start",
                          json={"user_id": user}).get_json()["session_id"]
        t = client.post(f"/sessions/{sid}/trades",
                        json={"direction": "long", "size": 10, "bar_sequence": 0,
                              "stop_loss": 96}).get_json()
        client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})

        earned = client.post(f"/sessions/{sid}/end").get_json()
        check("the earning action reports its unlocks",
              len(earned["engagement"]["feedback"]["milestones_unlocked"]) > 0)

        # Now earn something new through a path that does NOT claim (evaluation
        # is server-side and runs constantly), and prove re-reading the old
        # session leaves it waiting to be shown.
        from app.routes.progress import get_or_create_progress
        progress = get_or_create_progress(user)
        progress.completed_lessons = ["how_markets_work", "order_types",
                                      "chart_reading_basics", "support_resistance",
                                      "trends_conditions"]
        db.session.commit()
        ms.evaluate(user)
        pending = UserMilestone.query.filter_by(user_id=user, seen_at=None).count()
        check("a new unlock is waiting", pending > 0)

        for _ in range(2):
            r = client.post(f"/sessions/{sid}/end").get_json()
            # Re-showing is fine and even desirable; silently CONSUMING is the
            # bug, so the row must still be unseen afterwards.
            check("a re-read still shows the pending unlock",
                  len(r["engagement"]["feedback"]["milestones_unlocked"]) == pending)
        check("still waiting after two re-reads",
              UserMilestone.query.filter_by(user_id=user, seen_at=None).count() == pending)

        claimed = feedback_payload(user, 0, claim_milestones=True)
        check("an explicit claim returns them",
              len(claimed["milestones_unlocked"]) == pending)
        check("and only once",
              feedback_payload(user, 0)["milestones_unlocked"] == [])


def test_sessions_unlock_discipline_milestones():
    with app.app_context():
        _seed()
        user = UID + "_disc"
        for _ in range(5):
            _disciplined_session(user)
        codes = {um.milestone.code for um in
                 UserMilestone.query.filter_by(user_id=user).all()}
        print(f"       unlocked: {sorted(codes)}")
        check("first scored session", "first_session" in codes)
        check("five sessions with a stop on every trade", "stops_five" in codes)
        check("five sessions inside the risk cap", "risk_five" in codes)
        check("five sessions where the entry stop held", "plan_five" in codes)


# ── 3. the gallery ─────────────────────────────────────────────────────────
def test_gallery_shows_locked_criteria_and_progress():
    with app.app_context():
        _seed()
        user = UID + "_gallery"
        client.post(f"/progress/{user}/complete", json={"item_id": "how_markets_work"})
        r = client.get(f"/engagement/milestones/{user}").get_json()

        check("every seeded milestone is listed",
              r["total_count"] == len(ms.SEED_MILESTONES))
        check("categories are grouped", set(r["categories"]) ==
              {"learning", "discipline", "consistency", "craft"})

        locked = [m for m in r["milestones"] if not m["unlocked"]]
        check("there are locked entries to inspect", len(locked) > 0)
        check("EVERY locked entry states its criteria",
              all(m["criteria"].strip() for m in locked))
        check("no locked entry hides its name",
              all(m["name"].strip() and "?" not in m["name"] for m in locked))
        check("every locked entry has a target",
              all(m["target"] is not None for m in locked))
        check("every locked entry shows current progress",
              all(m["current"] is not None for m in locked))

        five = next(m for m in r["milestones"] if m["code"] == "five_lessons")
        check("progress is real: 1 of 5 lessons",
              five["current"] == 1 and five["target"] == 5)


def test_rate_metrics_need_a_sample_behind_them():
    """A rate with no sample is not an achievement.

    career metrics report a 100% stop rate for a learner with no trades (so new
    accounts aren't gated), which would have unlocked the stop-rate milestone
    for doing nothing at all. Same for a 100 average discipline off one session.
    """
    with app.app_context():
        _seed()
        user = UID + "_nosample"
        client.post(f"/progress/{user}/complete", json={"item_id": "how_markets_work"})
        codes = {um.milestone.code for um in
                 UserMilestone.query.filter_by(user_id=user).all()}
        check("no stop-rate unlock without trades", "stop_rate_ninety" not in codes)
        check("no discipline-average unlock without sessions", "disc_ninety" not in codes)
        check("the lesson milestone still unlocked", "first_lesson" in codes)

        metrics = ms.compute_metrics(user)
        check("stop rate reads zero, not one", metrics["pct_trades_with_stops"] == 0.0)
        check("discipline average reads zero", metrics["avg_discipline"] == 0.0)


def test_one_perfect_session_is_not_an_average():
    with app.app_context():
        _seed()
        user = UID + "_onesession"
        _disciplined_session(user)
        codes = {um.milestone.code for um in
                 UserMilestone.query.filter_by(user_id=user).all()}
        check("first session unlocked", "first_session" in codes)
        check("but not the 90 average", "disc_ninety" not in codes)
        check("and not the 80 average", "disc_eighty" not in codes)


def test_criteria_read_as_english_at_one():
    check("singular lesson", ms.criteria_text("lessons_completed", 1)
          == "Complete 1 lesson")
    check("plural lessons", ms.criteria_text("lessons_completed", 20)
          == "Complete 20 lessons")
    check("singular session", "1 session with" in
          ms.criteria_text("sessions_all_stops", 1))
    check("singular time", ms.criteria_text("goals_met", 1)
          == "Meet your daily goal 1 time")


def test_percentage_criteria_render_as_percentages():
    with app.app_context():
        _seed()
        text = ms.criteria_text("pct_trades_with_stops", 0.9)
        check("0.9 renders as 90%", "90%" in text)


if __name__ == "__main__":
    with app.app_context():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                print(name)
                fn()
    print("\nall milestone tests passed")
