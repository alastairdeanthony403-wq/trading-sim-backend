"""Consistency streaks (Phase 4) — the humane-design guarantees, as tests.

The streak is the easiest mechanic here to turn into a trap, so these assert
the shape rather than just the arithmetic: weekly not daily, rest days applied
silently, a soft reset that never wipes a run, and — the important one — that
NOTHING in the product gates on the value.

    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_streaks.py
"""
import os
import pathlib
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.engagement import ActivityDay, StreakState
from app.engagement import streaks
from app.engagement.day import local_date
from app.engagement.service import get_or_create_profile, award_xp

app = create_app()
client = app.test_client()

UID = "streak_" + os.urandom(4).hex()


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _monday(weeks_ago=0, tz="Europe/London"):
    today = local_date(tz)
    return today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_ago)


def _activity(user, day, xp=10):
    row = ActivityDay.query.filter_by(user_id=user, activity_date=day).first()
    if not row:
        row = ActivityDay(user_id=user, activity_date=day)
        db.session.add(row)
    row.xp_earned = (row.xp_earned or 0) + xp
    db.session.commit()


def _fill_week(user, weeks_ago, days=3):
    start = _monday(weeks_ago)
    for i in range(days):
        _activity(user, start + timedelta(days=i))


def _state(user, count, last_period_weeks_ago, freezes=0, best=None):
    """Put a learner mid-run, with the cursor just before the periods to test."""
    st = streaks.get_or_create(user)
    st.current_count = count
    st.best_count = best if best is not None else count
    st.freezes_available = freezes
    st.freezes_used = {"granted_month": f"{date.today().year}-{date.today().month:02d}",
                       "used": []}
    st.last_counted_period = streaks.period_key("week", _monday(last_period_weeks_ago))
    db.session.commit()
    return st


# ── 1. weekly, not daily ───────────────────────────────────────────────────
def test_the_target_is_active_days_per_week():
    with app.app_context():
        user = UID + "_weekly"
        get_or_create_profile(user)
        check("the default unit is a week", streaks.get_or_create(user).unit == "week")
        check("the target is a handful of days, not all of them",
              1 < streaks.WEEKLY_ACTIVE_DAYS_TARGET < 7)

        _fill_week(user, 0, days=streaks.WEEKLY_ACTIVE_DAYS_TARGET - 1)
        streaks.recompute(user)
        v = streaks.view(user)
        check("under target does not count yet", v["current_count"] == 0)
        check("progress is reported honestly",
              v["active_days_this_period"] == streaks.WEEKLY_ACTIVE_DAYS_TARGET - 1)

        _fill_week(user, 0, days=streaks.WEEKLY_ACTIVE_DAYS_TARGET)
        streaks.recompute(user)
        check("hitting the target counts this week straight away",
              streaks.view(user)["current_count"] == 1)


def test_four_days_off_costs_nothing():
    with app.app_context():
        user = UID + "_daysoff"
        get_or_create_profile(user)
        _fill_week(user, 0, days=streaks.WEEKLY_ACTIVE_DAYS_TARGET)
        streaks.recompute(user)
        before = streaks.view(user)["current_count"]
        streaks.recompute(user)          # nothing new happened; days pass
        check("the run is unaffected by inactive days inside the period",
              streaks.view(user)["current_count"] == before)


# ── 2. rest days ───────────────────────────────────────────────────────────
def test_a_missed_week_silently_spends_a_rest_day():
    with app.app_context():
        user = UID + "_freeze"
        get_or_create_profile(user)
        _state(user, count=5, last_period_weeks_ago=3, freezes=2)
        _fill_week(user, 1)              # last week met; week 2 ago missed
        result = streaks.recompute(user)

        check("a rest day was applied", len(result["froze"]) == 1)
        check("the run did not break", result["soft_reset"] is False)
        check("the count kept climbing", result["current"] == 6)
        v = streaks.view(user)
        check("one rest day left", v["freezes_available"] == 1)
        check("the spend is on the record for telling them afterwards",
              len(v["freezes_used"]) == 1)


def test_rest_days_are_topped_up_monthly():
    with app.app_context():
        user = UID + "_topup"
        get_or_create_profile(user)
        st = streaks.get_or_create(user)
        st.freezes_available = 0
        st.freezes_used = {"granted_month": "1999-01", "used": []}
        db.session.commit()

        streaks.recompute(user)
        check("a new month tops rest days back up",
              streaks.get_or_create(user).freezes_available == streaks.FREEZES_PER_MONTH)


def test_rest_days_do_not_hoard():
    with app.app_context():
        user = UID + "_hoard"
        get_or_create_profile(user)
        st = streaks.get_or_create(user)
        st.freezes_available = streaks.FREEZES_PER_MONTH
        st.freezes_used = {"granted_month": "1999-01", "used": []}
        db.session.commit()
        streaks.recompute(user)
        check("a top-up is a top-up, not an accumulation",
              streaks.get_or_create(user).freezes_available == streaks.FREEZES_PER_MONTH)


# ── 3. soft reset ──────────────────────────────────────────────────────────
def test_soft_reset_arithmetic():
    cases = {0: 0, 1: 1, 2: 1, 3: 2, 5: 4, 8: 4, 12: 8, 13: 12, 20: 16, 52: 36}
    for before, after in cases.items():
        check(f"{before} softens to {after}", streaks.soft_reset(before) == after)
    check("never lands on zero once a run has started",
          all(streaks.soft_reset(n) >= 1 for n in range(1, 60)))


def test_running_out_of_rest_days_softens_rather_than_wipes():
    with app.app_context():
        user = UID + "_soft"
        get_or_create_profile(user)
        _state(user, count=12, last_period_weeks_ago=2, freezes=0, best=12)
        # last week missed entirely, and no rest days left
        result = streaks.recompute(user)
        check("the run broke", result["soft_reset"] is True)
        check("12 dropped to 8, not to 0", result["current"] == 8)
        check("best is preserved", result["best"] == 12)
        v = streaks.view(user)
        check("and best is displayed", "12" in (v["best_label"] or ""))


def test_best_count_survives_everything():
    with app.app_context():
        user = UID + "_best"
        get_or_create_profile(user)
        _state(user, count=8, last_period_weeks_ago=2, freezes=0, best=20)
        streaks.recompute(user)
        check("a break never touches the best", streaks.view(user)["best_count"] == 20)


# ── 4. timezone-correct periods ────────────────────────────────────────────
def test_period_boundaries_are_calendar_correct():
    sunday = date(2026, 8, 23)          # ISO week 34
    monday = date(2026, 8, 24)          # ISO week 35
    check("Sunday closes the week", streaks.period_key("week", sunday) == "2026-W34")
    check("Monday opens the next", streaks.period_key("week", monday) == "2026-W35")
    check("the period starts on Monday",
          streaks.period_start("week", sunday) == date(2026, 8, 17))
    check("a period spans seven days",
          len(streaks.period_days("week", monday)) == 7)
    check("the day unit is its own period",
          streaks.period_key("day", monday) == "2026-08-24")


def test_the_period_follows_the_learners_timezone():
    """At any instant, UTC+14 and UTC-11 can sit in different local weeks."""
    with app.app_context():
        keys = {}
        for zone, suffix in (("Pacific/Kiritimati", "_tz_e"), ("Pacific/Midway", "_tz_w")):
            user = UID + suffix
            profile = get_or_create_profile(user)
            profile.timezone = zone
            db.session.commit()
            v = streaks.view(user)
            expected = streaks.period_key("week", local_date(zone))
            check(f"{zone}: the period is the learner's own",
                  v["period_key"] == expected)
            keys[zone] = v["period_key"]
        print(f"       {keys}")


# ── 5. display only — the load-bearing guarantee ───────────────────────────
GATING_FILES = [
    "app/routes/progress.py",     # career levels, tool + market + mode unlocks
    "app/routes/contests.py",     # ranked entry
    "app/routes/paper.py",        # paper entry
    "app/routes/academy.py",      # lesson / check gating
    "app/routes/game.py",         # scenario start, scoring
    "app/engagement/milestones.py",
    "app/engagement/xp_rules.py",
    "app/rules.py",
]


def test_no_gating_code_reads_the_streak():
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for rel in GATING_FILES:
        text = (root / rel).read_text()
        for pattern in (r"\bstreaks\b", r"\bStreakState\b", r"streak_state",
                        r"current_count", r"best_count", r"freezes_available"):
            for m in re.finditer(pattern, text):
                line = text[:m.start()].count("\n") + 1
                snippet = text.splitlines()[line - 1].strip()
                if snippet.startswith("#"):
                    continue          # a comment about the rule is not a read
                offenders.append(f"{rel}:{line} {snippet[:60]}")
    for o in offenders:
        print(f"       {o}")
    check("no gating module reads streak state", not offenders)


def test_a_long_streak_changes_nothing_a_learner_can_unlock():
    with app.app_context():
        plain, streaky = UID + "_plain", UID + "_streaky"
        for u in (plain, streaky):
            get_or_create_profile(u)
            award_xp(u, "lesson_complete", "x", 20, f"gate:{u}")

        st = streaks.get_or_create(streaky)
        st.current_count, st.best_count = 52, 52
        db.session.commit()

        def snapshot(u):
            career = client.get(f"/career/{u}").get_json()
            tools = client.get(f"/config/tools/{u}").get_json()
            xp = client.get(f"/engagement/xp/{u}").get_json()
            miles = client.get(f"/engagement/milestones/{u}").get_json()
            # user_id differs by construction; everything else must not.
            return {
                "level": career["level"],
                "modes": career["modes"],
                "unlocks": career.get("unlocks"),
                "unlocked_tools": tools["unlocked_tools"],
                "tool_level": tools["tool_level"],
                "unlocked_markets": tools["unlocked_markets"],
                "total_xp": xp["total_xp"],
                "career_level_from_xp": xp["career_level"],
                "milestones": miles["unlocked_count"],
                "requirements": career.get("requirements"),
            }

        a, b = snapshot(plain), snapshot(streaky)
        for key in a:
            check(f"a 52-week run does not change {key}", a[key] == b[key])


# ── 6. copy ────────────────────────────────────────────────────────────────
LOSS_LANGUAGE = [
    "don't lose", "dont lose", "lose your", "you'll lose", "about to lose",
    "keep your streak", "streak alive", "break your", "don't break",
    "expires", "ends in", "last chance", "hurry", "running out", "at risk",
    "days left", "before midnight",
]


def test_streak_copy_never_names_what_could_be_lost():
    with app.app_context():
        user = UID + "_copy"
        get_or_create_profile(user)
        _state(user, count=6, last_period_weeks_ago=0, freezes=2, best=9)
        v = streaks.view(user)
        rendered = [str(v.get(k) or "") for k in
                    ("label", "best_label", "progress_label")]
        print(f"       {rendered}")
        offences = [(p, t) for t in rendered for p in LOSS_LANGUAGE if p in t.lower()]
        check("no loss-aversion phrasing", not offences)
        check("the copy states what happened", "Consistent for 6 weeks" in rendered[0])
        check("no countdown is offered",
              not any(k in v for k in ("days_remaining", "expires_at", "deadline")))


def test_the_old_utc_daily_streak_is_gone():
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "app/routes/missions.py").read_text()
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    check("_daily_streak no longer exists", "_daily_streak" not in code)
    with app.app_context():
        r = client.get(f"/missions/daily?user_id={UID}_daily").get_json()
        check("the daily endpoint no longer reports a streak", "streak" not in r)


# ── 7. idempotency ─────────────────────────────────────────────────────────
def test_recompute_is_idempotent():
    with app.app_context():
        user = UID + "_idem"
        get_or_create_profile(user)
        _fill_week(user, 0, days=streaks.WEEKLY_ACTIVE_DAYS_TARGET)
        first = streaks.recompute(user)
        for _ in range(5):
            again = streaks.recompute(user)
        check("the count does not drift", again["current"] == first["current"])
        check("no phantom rest days are spent",
              streaks.get_or_create(user).freezes_available
              == streaks.FREEZES_PER_MONTH)


if __name__ == "__main__":
    with app.app_context():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                print(name)
                fn()
    print("\nall streak tests passed")
