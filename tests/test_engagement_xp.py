"""Engagement layer (Phase 1) — XP integrity, process-not-profit, local days.

The three guarantees this layer has to make, and the negative tests that keep
them honest:

  * idempotency — a replayed award pays once
  * process over outcome — a disciplined loss beats a reckless profit, and a
    mission's profit clause earns nothing
  * local-day truth — activity lands on the learner's calendar date, not UTC's

    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_engagement_xp.py
"""
import os
import sys
from datetime import datetime, timezone, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models.scenario import Scenario, ScenarioBar
from app.models.session import Session, Trade
from app.models.mission import Mission
from app.models.engagement import XpEvent, ActivityDay
from app.engagement import xp_rules
from app.engagement.awards import award_session_xp, award_mission_xp
from app.engagement.day import local_date
from app.engagement.service import (award_xp, total_xp, get_or_create_profile,
                                    paper_xp_remaining_today)
from app.rules import evaluate_discipline, check_mission_rules, session_context

app = create_app()
client = app.test_client()

UID = "eng_" + os.urandom(4).hex()          # fresh learner per run


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def _scenario(bars):
    with app.app_context():
        s = Scenario(name_internal="eng", asset_class="crypto", timeframe="1h",
                     difficulty_tier=1, is_active=True)
        db.session.add(s); db.session.flush()
        for i, (o, h, l, c) in enumerate(bars):
            db.session.add(ScenarioBar(scenario_id=s.id, bar_sequence=i,
                                       open=o, high=h, low=l, close=c, volume=1))
        db.session.commit()
        return s.id


DOWN = [(100, 101, 99, 100), (100, 100, 98, 99), (99, 99, 97, 98), (98, 98, 96, 97)]
UP   = [(100, 101, 99, 100), (100, 102, 100, 101), (101, 103, 101, 102), (102, 104, 102, 103)]


def _play(bars, user, stop_loss, size=10):
    """One trade, opened at bar 0 and closed at bar 2. Returns the end payload."""
    sid = client.post(f"/scenarios/{_scenario(bars)}/start",
                      json={"user_id": user}).get_json()["session_id"]
    t = client.post(f"/sessions/{sid}/trades",
                    json={"direction": "long", "size": size, "bar_sequence": 0,
                          "stop_loss": stop_loss}).get_json()
    client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
    return sid, client.post(f"/sessions/{sid}/end").get_json()


def _xp_for_session(session_id):
    return sum(e.amount for e in XpEvent.query.filter_by(source_id=str(session_id)).all()
               if e.source_type in ("scenario_complete", "risk_discipline", "plan_adherence"))


# ── 1. idempotency ─────────────────────────────────────────────────────────
def test_replayed_key_awards_once():
    with app.app_context():
        user = UID + "_idem"
        key = f"test:replay:{user}"
        first = award_xp(user, "lesson_complete", "x", 50, key)
        second = award_xp(user, "lesson_complete", "x", 50, key)
        check("first award lands", first["awarded"] == 50 and not first["duplicate"])
        check("replay awards nothing", second["awarded"] == 0)
        check("replay reports duplicate", second["duplicate"] is True)
        check("total counted once", total_xp(user) == 50)
        check("one ledger row", XpEvent.query.filter_by(idempotency_key=key).count() == 1)


def test_lesson_keys_are_per_learner():
    """A shared item id must not let the first learner claim it for everyone."""
    with app.app_context():
        a, b = UID + "_la", UID + "_lb"
        client.post(f"/progress/{a}/complete", json={"item_id": "risk_basics"})
        client.post(f"/progress/{b}/complete", json={"item_id": "risk_basics"})
        check("learner A earned the lesson", total_xp(a) == xp_rules.LESSON_COMPLETE)
        check("learner B earned it too", total_xp(b) == xp_rules.LESSON_COMPLETE)


def test_remarking_a_lesson_pays_once():
    with app.app_context():
        user = UID + "_re"
        client.post(f"/progress/{user}/complete", json={"item_id": "order_types"})
        again = client.post(f"/progress/{user}/complete",
                            json={"item_id": "order_types"}).get_json()
        check("second mark awards nothing", again["xp_awarded"] == 0)
        check("total is one lesson", total_xp(user) == xp_rules.LESSON_COMPLETE)


def test_unit_check_pays_the_check_rate():
    with app.app_context():
        user = UID + "_chk"
        r = client.post(f"/progress/{user}/complete",
                        json={"item_id": "check_foundations"}).get_json()
        check("check pays the check rate", r["xp_awarded"] == xp_rules.UNIT_CHECK_PASS)


# ── 2. process over outcome ────────────────────────────────────────────────
def test_disciplined_loss_beats_reckless_profit():
    """The flagship guarantee from the design principles."""
    with app.app_context():
        loser, _ = _play(DOWN, UID + "_loss", stop_loss=96)     # disciplined, red
        winner, _ = _play(UP, UID + "_win", stop_loss=30)       # oversized, green

        loss_score = db.session.get(Session, loser).score
        win_score = db.session.get(Session, winner).score
        check("the disciplined session lost money", loss_score.total_return_pct < 0)
        check("the reckless session made money", win_score.total_return_pct > 0)
        check("the reckless session violated risk", win_score.oversize_count == 1)

        loss_xp, win_xp = _xp_for_session(loser), _xp_for_session(winner)
        print(f"       disciplined loss = {loss_xp} XP, reckless profit = {win_xp} XP")
        check("disciplined loss earns at least as much", loss_xp >= win_xp)
        check("and strictly more here", loss_xp > win_xp)


def test_no_xp_source_reads_profit():
    check("min_return_pct is not a paying rule",
          "min_return_pct" not in xp_rules.PROCESS_RULE_TYPES)
    check("min_return_pct is named as an outcome rule",
          "min_return_pct" in xp_rules.OUTCOME_RULE_TYPES)
    check("the two sets never overlap",
          not (xp_rules.PROCESS_RULE_TYPES & xp_rules.OUTCOME_RULE_TYPES))


def test_mission_profit_clause_earns_nothing():
    """Same process, opposite P&L → identical mission XP."""
    with app.app_context():
        mission = Mission(slug="t_" + os.urandom(3).hex(), title="Profit clause",
                          difficulty_tier=1, xp_reward=70,
                          rules=[{"type": "require_stop_on_all", "label": "stops"},
                                 {"type": "min_return_pct", "param": 5.0, "label": "+5%"}])
        db.session.add(mission); db.session.commit()

        awarded = {}
        for label, bars in (("loss", DOWN), ("profit", UP)):
            sid, _ = _play(bars, UID + "_m" + label, stop_loss=96 if bars is DOWN else 99)
            session = db.session.get(Session, sid)
            disc = evaluate_discipline(session)
            passed, results = check_mission_rules(mission.rules,
                                                  session_context(session, disc))
            awarded[label] = award_mission_xp(session.user_id, mission, session,
                                              results)["awarded"]

        check("only the process rule paid",
              awarded["loss"] == xp_rules.MISSION_PROCESS_RULE)
        check("profit changed nothing", awarded["loss"] == awarded["profit"])


def test_session_with_no_trades_pays_participation_only():
    with app.app_context():
        user = UID + "_empty"
        sid = client.post(f"/scenarios/{_scenario(DOWN)}/start",
                          json={"user_id": user}).get_json()["session_id"]
        client.post(f"/sessions/{sid}/end")
        check("no trades → participation only",
              _xp_for_session(sid) == xp_rules.SESSION_COMPLETE)


def test_widening_a_stop_forfeits_plan_adherence():
    with app.app_context():
        user = UID + "_widen"
        sid = client.post(f"/scenarios/{_scenario(DOWN)}/start",
                          json={"user_id": user}).get_json()["session_id"]
        t = client.post(f"/sessions/{sid}/trades",
                        json={"direction": "long", "size": 10, "bar_sequence": 0,
                              "stop_loss": 96}).get_json()
        client.patch(f"/trades/{t['trade_id']}", json={"stop_loss": 90})
        client.post(f"/trades/{t['trade_id']}/close", json={"bar_sequence": 2})
        client.post(f"/sessions/{sid}/end")
        kinds = {e.source_type for e in XpEvent.query.filter_by(source_id=str(sid)).all()}
        check("entry stop was preserved",
              db.session.get(Trade, t["trade_id"]).entry_stop_loss == 96)
        check("no plan-adherence award", "plan_adherence" not in kinds)


def test_honouring_the_entry_stop_pays_adherence():
    with app.app_context():
        sid, _ = _play(DOWN, UID + "_honour", stop_loss=96)
        kinds = {e.source_type for e in XpEvent.query.filter_by(source_id=str(sid)).all()}
        check("plan adherence awarded", "plan_adherence" in kinds)


# ── 3. local-day truth ─────────────────────────────────────────────────────
def test_local_date_uses_the_learners_zone():
    late = datetime(2026, 8, 19, 23, 30, tzinfo=timezone.utc)
    check("UTC+12 has already rolled over",
          local_date("Pacific/Auckland", late) == date(2026, 8, 20))
    check("UTC-7 is still on the previous day",
          local_date("America/Los_Angeles", late) == date(2026, 8, 19))
    check("UTC itself is unchanged", local_date("UTC", late) == date(2026, 8, 19))

    early = datetime(2026, 8, 19, 0, 30, tzinfo=timezone.utc)
    check("UTC-7 is still the day before",
          local_date("America/Los_Angeles", early) == date(2026, 8, 18))
    check("a bad zone falls back instead of raising",
          local_date("Not/AZone", early) == date(2026, 8, 19))


def test_activity_lands_on_the_local_date():
    """At any instant, one of UTC+14 / UTC-11 disagrees with the UTC date."""
    with app.app_context():
        utc_today = datetime.now(timezone.utc).date()
        landed = []
        for zone, suffix in (("Pacific/Kiritimati", "_far_e"), ("Pacific/Midway", "_far_w")):
            user = UID + suffix
            profile = get_or_create_profile(user)
            profile.timezone = zone
            db.session.commit()
            award_xp(user, "lesson_complete", "tz", 10, f"tz:{user}")
            row = ActivityDay.query.filter_by(user_id=user).one()
            check(f"{zone} row is on its own local date",
                  row.activity_date == local_date(zone))
            landed.append(row.activity_date)
        check("at least one zone differs from the UTC date",
              any(d != utc_today for d in landed))


def test_goal_met_tracks_the_chosen_goal():
    with app.app_context():
        user = UID + "_goal"
        client.patch(f"/engagement/profile/{user}",
                     json={"daily_goal_type": "lessons", "daily_goal_target": 2})
        client.post(f"/progress/{user}/complete", json={"item_id": "risk_basics"})
        row = ActivityDay.query.filter_by(user_id=user).one()
        check("one of two lessons is not the goal", row.goal_met is False)
        client.post(f"/progress/{user}/complete", json={"item_id": "order_types"})
        db.session.refresh(row)
        check("two of two meets it", row.goal_met is True)


# ── 4. caps and the legacy import ──────────────────────────────────────────
def test_paper_xp_is_capped_per_day():
    with app.app_context():
        user = UID + "_paper"
        scenario_id = _scenario(DOWN)
        remaining_before = paper_xp_remaining_today(user)
        check("cap starts full", remaining_before == xp_rules.PAPER_DAILY_XP_CAP)

        total = 0
        for _ in range(6):                        # more paper runs than the cap allows
            session = Session(user_id=user, scenario_id=scenario_id, mode="paper",
                              starting_balance=10000.0, status="complete")
            db.session.add(session); db.session.flush()
            db.session.add(Trade(session_id=session.id, bar_sequence_entered=0,
                                 bar_sequence_exited=2, direction="long", size=10,
                                 entry_price=100.0, exit_price=98.0, stop_loss=96.0,
                                 entry_stop_loss=96.0, pnl=-20.0, status="closed"))
            db.session.commit()
            total += award_session_xp(session, evaluate_discipline(session))

        check("paper XP stopped at the daily cap", total == xp_rules.PAPER_DAILY_XP_CAP)
        check("no budget left", paper_xp_remaining_today(user) == 0)


def test_career_sessions_are_not_capped():
    with app.app_context():
        user = UID + "_uncapped"
        earned = [_xp_for_session(_play(DOWN, user, stop_loss=96)[0]) for _ in range(3)]
        check("every career session paid in full",
              all(e == earned[0] and e > 0 for e in earned))


def test_legacy_import_is_clamped_and_single_fire():
    with app.app_context():
        user = UID + "_legacy"
        first = client.post("/engagement/xp/import",
                            json={"user_id": user, "xp": 999999}).get_json()
        check("clamped to the cap", first["imported"] == xp_rules.LEGACY_IMPORT_CAP)
        second = client.post("/engagement/xp/import",
                             json={"user_id": user, "xp": 500}).get_json()
        check("a second import is a duplicate", second["duplicate"] is True)
        check("total unchanged", second["total_xp"] == xp_rules.LEGACY_IMPORT_CAP)


def test_profile_rejects_bad_input_and_allows_lowering_goals():
    with app.app_context():
        user = UID + "_prof"
        bad = client.patch(f"/engagement/profile/{user}", json={"timezone": "Mars/Olympus"})
        check("unknown timezone rejected", bad.status_code == 400)
        bad = client.patch(f"/engagement/profile/{user}", json={"daily_goal_type": "vibes"})
        check("unknown goal type rejected", bad.status_code == 400)

        client.patch(f"/engagement/profile/{user}", json={"daily_goal_target": 5})
        lowered = client.patch(f"/engagement/profile/{user}",
                               json={"daily_goal_target": 1}).get_json()
        check("goals can be lowered freely", lowered["daily_goal_target"] == 1)

        defaults = client.get(f"/engagement/profile/{UID}_defaults").get_json()
        check("sound is off by default", defaults["sound_enabled"] is False)
        check("nudges are off by default", defaults["nudges_opt_in"] is False)


if __name__ == "__main__":
    with app.app_context():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                print(name)
                fn()
    print("\nall engagement tests passed")
