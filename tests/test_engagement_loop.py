"""Engagement loop (Phase 2) — feedback contract, honest next goals, safe copy.

    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_engagement_loop.py
"""
import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar
from app.models.session import Session
from app.models.mission import Mission
from app.engagement import xp_rules
from app.engagement.feedback import feedback_payload, next_goal, goal_progress
from app.engagement.service import get_or_create_profile, get_or_create_day
from app.engagement.day import local_date

app = create_app()
client = app.test_client()

UID = "loop_" + os.urandom(4).hex()

FEEDBACK_KEYS = {"xp_awarded", "xp_total", "goal_progress", "next_goal",
                 "milestones_unlocked"}
GOAL_KEYS = {"type", "target", "current", "met", "remaining", "label"}
NEXT_KEYS = {"done_for_today", "label", "action", "item_id", "item_type"}


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _scenario(bars):
    with app.app_context():
        s = Scenario(name_internal="loop", asset_class="crypto", timeframe="1h",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate(bars):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


DOWN = [(100, 101, 99, 100), (100, 100, 98, 99), (99, 99, 97, 98), (98, 98, 96, 97)]


def _disciplined_losing_session(user):
    sid = client.post(f"/scenarios/{_scenario(DOWN)}/start",
                      json={"user_id": user}).get_json()["session_id"]
    t = client.post(f"/sessions/{sid}/trades",
                    json={"direction": "long", "size": 10, "bar_sequence": 0,
                          "stop_loss": 96}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    return sid, client.post(f"/sessions/{sid}/end").get_json()


def _assert_feedback(label, fb):
    check(f"{label}: feedback keys exact", set(fb) == FEEDBACK_KEYS)
    check(f"{label}: goal_progress keys exact", set(fb["goal_progress"]) == GOAL_KEYS)
    check(f"{label}: next_goal keys exact", set(fb["next_goal"]) == NEXT_KEYS)
    check(f"{label}: xp_awarded is an int", isinstance(fb["xp_awarded"], int))
    check(f"{label}: xp_total is an int", isinstance(fb["xp_total"], int))
    # Phase 2 pinned this to []; Phase 3 populates it. What the contract
    # guarantees is that it is always a list, and that anything in it states
    # the criteria that unlocked it.
    check(f"{label}: milestones is a list", isinstance(fb["milestones_unlocked"], list))
    check(f"{label}: every unlock states its criteria",
          all(m.get("criteria") and m.get("name")
              for m in fb["milestones_unlocked"]))


# ── 1. the feedback contract ───────────────────────────────────────────────
def test_lesson_completion_returns_feedback():
    with app.app_context():
        r = client.post(f"/progress/{UID}_lesson/complete",
                        json={"item_id": "how_markets_work"}).get_json()
        _assert_feedback("lesson", r["feedback"])
        check("lesson: award matches the rubric",
              r["feedback"]["xp_awarded"] == xp_rules.LESSON_COMPLETE)


def test_session_end_returns_engagement_block():
    with app.app_context():
        _, res = _disciplined_losing_session(UID + "_sess")
        eng = res["engagement"]
        check("session: engagement keys exact",
              set(eng) == {"strengths", "focus", "xp_breakdown",
                           "xp_session_total", "feedback"})
        _assert_feedback("session", eng["feedback"])
        check("session: breakdown sums to the session total",
              sum(e["amount"] for e in eng["xp_breakdown"]) == eng["xp_session_total"])
        check("session: breakdown is non-empty", len(eng["xp_breakdown"]) > 0)


def test_refinalising_returns_the_same_engagement_block():
    """The results screen can be re-read without re-awarding anything."""
    with app.app_context():
        sid, first = _disciplined_losing_session(UID + "_refin")
        again = client.post(f"/sessions/{sid}/end").get_json()
        check("re-finalise pays nothing more",
              again["engagement"]["xp_session_total"]
              == first["engagement"]["xp_session_total"])
        _assert_feedback("re-finalise", again["engagement"]["feedback"])


def test_mission_submit_returns_feedback():
    with app.app_context():
        user = UID + "_mission"
        mission = Mission(slug="l_" + os.urandom(3).hex(), title="Loop", difficulty_tier=1,
                          xp_reward=50,
                          rules=[{"type": "require_stop_on_all", "label": "stops"}])
        db.session.add(mission); db.session.commit()
        sid, _ = _disciplined_losing_session(user)
        r = client.post(f"/missions/{mission.id}/submit",
                        json={"session_id": sid, "user_id": user}).get_json()
        _assert_feedback("mission", r["feedback"])


# ── 2. the next goal is honest ─────────────────────────────────────────────
def test_a_met_goal_is_never_answered_with_a_bigger_one():
    with app.app_context():
        user = UID + "_chain"
        client.patch(f"/engagement/profile/{user}",
                     json={"daily_goal_type": "lessons", "daily_goal_target": 1})
        r = client.post(f"/progress/{user}/complete",
                        json={"item_id": "how_markets_work"}).get_json()
        nxt = r["feedback"]["next_goal"]
        check("goal reported met", r["feedback"]["goal_progress"]["met"] is True)
        check("next goal is a stop, not a bigger target", nxt["done_for_today"] is True)
        check("no onward action is pushed", nxt["action"] is None)

        # Doing more today must not silently raise the bar either.
        r2 = client.post(f"/progress/{user}/complete",
                         json={"item_id": "order_types"}).get_json()
        check("target unchanged after extra work",
              r2["feedback"]["goal_progress"]["target"] == 1)
        check("still a clean stop", r2["feedback"]["next_goal"]["done_for_today"] is True)


def test_unmet_goal_names_one_concrete_action():
    with app.app_context():
        user = UID + "_concrete"
        client.patch(f"/engagement/profile/{user}",
                     json={"daily_goal_type": "lessons", "daily_goal_target": 3})
        fb = feedback_payload(user, 0)
        nxt = fb["next_goal"]
        check("not done for today", nxt["done_for_today"] is False)
        check("names a specific curriculum item", bool(nxt["item_id"]))
        check("and it is the first unstarted one", nxt["item_id"] == "how_markets_work")


def test_every_goal_type_produces_a_usable_next_step():
    with app.app_context():
        for goal_type in ("lessons", "sessions", "minutes", "xp"):
            user = f"{UID}_g_{goal_type}"
            client.patch(f"/engagement/profile/{user}",
                         json={"daily_goal_type": goal_type, "daily_goal_target": 2})
            profile = get_or_create_profile(user)
            row = get_or_create_day(user, local_date(profile.timezone))
            nxt = next_goal(user, profile, row)
            prog = goal_progress(profile, row)
            check(f"{goal_type}: label is non-empty", bool(nxt["label"].strip()))
            check(f"{goal_type}: progress label is non-empty", bool(prog["label"].strip()))


# ── 3. the session summary reads process, not P&L ──────────────────────────
def test_a_disciplined_loss_is_all_strengths():
    with app.app_context():
        _, res = _disciplined_losing_session(UID + "_strong")
        eng = res["engagement"]
        check("the session lost money", res["total_return_pct"] < 0)
        check("strengths were still found", len(eng["strengths"]) >= 3)
        check("nothing flagged to work on", eng["focus"] is None)


def test_a_missing_stop_becomes_the_one_focus_item():
    with app.app_context():
        user = UID + "_nostop"
        sid = client.post(f"/scenarios/{_scenario(DOWN)}/start",
                          json={"user_id": user}).get_json()["session_id"]
        t = client.post(f"/sessions/{sid}/trades",
                        json={"direction": "long", "size": 10,
                              "bar_sequence": 0}).get_json()
        client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
        eng = client.post(f"/sessions/{sid}/end").get_json()["engagement"]
        check("a focus item was raised", eng["focus"] is not None)
        check("it points at the missing stop", "stop" in eng["focus"]["text"].lower())
        check("and links to a lesson", eng["focus"]["lesson_id"] == "risk_basics")
        check("focus is a single item, not a list", isinstance(eng["focus"], dict))


# ── 4. the summary endpoint ────────────────────────────────────────────────
def test_summary_endpoint_shape():
    with app.app_context():
        r = client.get(f"/engagement/summary/{UID}_sum").get_json()
        check("summary keys exact",
              set(r) == {"user_id", "total_xp", "career_level", "career_level_name",
                         "next_level_name", "career_progress", "requirements",
                         "goal", "next_goal", "consistency", "today", "profile"})
        check("consistency keys exact",
              set(r["consistency"]) == {"week_start", "active_days", "goals_met"})
        check("career_progress is a fraction", 0.0 <= r["career_progress"] <= 1.0)


# ── 5. copy scan ───────────────────────────────────────────────────────────
BANNED = [
    # profitability / outcome claims
    "profit", "p&l", "make money", "made money", "beat the market",
    "win rate", "returns", "payout", "earnings potential",
    # loss-aversion / streak guilt
    "don't lose", "dont lose", "lose your", "keep your streak", "streak alive",
    "break your", "you'll lose", "at risk of losing",
    # manufactured urgency
    "hurry", "ends in", "expires", "last chance", "limited time", "only today",
    "act now", "running out",
]

SCANNED = ["app/engagement/feedback.py", "app/engagement/summary.py"]


def _string_literals(path):
    """Every string literal that could reach a learner.

    Docstrings are excluded: they are developer-facing, and the ones in this
    layer deliberately NAME the banned vocabulary in order to forbid it.
    """
    tree = ast.parse(pathlib.Path(path).read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def test_no_banned_language_in_engagement_copy():
    """Snapshot guard over every string the engagement layer can show a learner.

    Scoped to copy this layer authors. app/coach.py's per-trade review language
    ("your average winner was +1.2R") is trade analysis, not a profitability
    claim, and predates this phase.
    """
    offences = []
    for path in SCANNED:
        for literal in _string_literals(path):
            low = literal.lower()
            for phrase in BANNED:
                if phrase in low:
                    offences.append((path, phrase, literal[:70]))
    for path, phrase, snippet in offences:
        print(f"       {path}: {phrase!r} in {snippet!r}")
    check("no banned phrases in engagement copy", not offences)


def test_rendered_copy_is_also_clean():
    """The literals above are templates; check what learners actually see."""
    with app.app_context():
        rendered = []
        for goal_type in ("lessons", "sessions", "minutes", "xp"):
            user = f"{UID}_c_{goal_type}"
            client.patch(f"/engagement/profile/{user}",
                         json={"daily_goal_type": goal_type, "daily_goal_target": 1})
            fb = feedback_payload(user, 0)
            rendered += [fb["goal_progress"]["label"], fb["next_goal"]["label"]]

        _, res = _disciplined_losing_session(UID + "_copy")
        eng = res["engagement"]
        rendered += eng["strengths"]
        if eng["focus"]:
            rendered.append(eng["focus"]["text"])

        offences = [(p, t) for t in rendered for p in BANNED if p in t.lower()]
        for phrase, text in offences:
            print(f"       {phrase!r} in {text!r}")
        check("no banned phrases in rendered copy", not offences)


if __name__ == "__main__":
    with app.app_context():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                print(name)
                fn()
    print("\nall engagement loop tests passed")
