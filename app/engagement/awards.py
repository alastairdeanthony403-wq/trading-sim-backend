"""Where the rubric meets the product: what each finished thing is worth.

These are the only callers of award_xp(). Each function computes an amount from
app.engagement.xp_rules and hands it over with a stable idempotency key, so the
route it hangs off can be retried, double-tapped or replayed safely.

Keys are globally unique, so any key built from a shared identifier (a lesson
id, a check id) MUST include the user_id. Keys built from a per-learner row
(a session id, a mission attempt) are already unique without it.

Note there is no special case for a blown account. Blowing up is a risk-control
failure, so it already scores near-zero through the process components — gating
on the blow-up itself would be gating on P&L, which is exactly what this layer
refuses to do.
"""
from app.engagement import xp_rules
from app.engagement.service import award_xp, paper_xp_remaining_today


# ── trading sessions ───────────────────────────────────────────────────────
def _session_components(session, discipline):
    """[(source_type, key_suffix, amount, meta)] earned by a finished session."""
    closed = [t for t in session.trades if t.status == "closed"]
    components = [("scenario_complete", "complete", xp_rules.SESSION_COMPLETE,
                   {"reason": "session_finished"})]

    if len(closed) < xp_rules.MIN_TRADES_FOR_PROCESS_XP:
        return components   # nothing was demonstrated, so nothing else is owed

    process = 0
    earned = []
    if discipline["no_stop_count"] == 0:
        process += xp_rules.STOP_ON_EVERY_TRADE
        earned.append("stop_on_every_trade")
    if discipline["oversize_count"] == 0:
        process += xp_rules.NO_OVERSIZED_RISK
        earned.append("no_oversized_risk")
    if discipline["revenge_count"] == 0:
        process += xp_rules.NO_REVENGE_TRADING
        earned.append("no_revenge_trading")

    band = xp_rules.discipline_band_xp(discipline["discipline_score"])
    if band:
        process += band
        earned.append("discipline_band")

    if process:
        components.append(("risk_discipline", "process", process,
                           {"earned": earned,
                            "discipline_score": discipline["discipline_score"]}))

    if _honoured_entry_stops(closed):
        components.append(("plan_adherence", "plan", xp_rules.PLAN_ADHERENCE,
                           {"reason": "no stop was widened after entry"}))
    return components


def _honoured_entry_stops(closed):
    """True if every closed trade declared a stop at entry and never widened it.

    Tightening or trailing a stop is honouring the plan; moving it further from
    entry mid-trade is abandoning the risk that was declared.
    """
    if not closed:
        return False
    for t in closed:
        if t.entry_stop_loss is None or t.stop_loss is None:
            return False
        declared = abs(t.entry_price - t.entry_stop_loss)
        final = abs(t.entry_price - t.stop_loss)
        if final > declared + 1e-9:
            return False
    return True


def award_session_xp(session, discipline):
    """Award a finished session's process XP. Paper is capped per local day."""
    is_paper = session.mode == "paper"
    budget = paper_xp_remaining_today(session.user_id) if is_paper else None
    results = []

    for source_type, suffix, amount, meta in _session_components(session, discipline):
        if is_paper:
            amount = min(amount, budget)
            budget -= amount
            meta = dict(meta, mode="paper", capped=xp_rules.PAPER_DAILY_XP_CAP)
        if amount <= 0:
            continue
        results.append(award_xp(
            session.user_id, source_type, session.id, amount,
            f"session:{session.id}:{suffix}", meta,
        ))
    return sum(r["awarded"] for r in results)


# ── missions ───────────────────────────────────────────────────────────────
def award_mission_xp(user_id, mission, session, rule_results):
    """Pay per PROCESS rule the learner actually satisfied.

    Deliberately independent of whether the mission passed overall: the six
    seeded missions with a profit clause keep that clause for their pass/fail
    and career gating, but it earns nothing here.
    """
    satisfied = [r for r in rule_results
                 if r["passed"] and r.get("type") in xp_rules.PROCESS_RULE_TYPES]
    amount = len(satisfied) * xp_rules.MISSION_PROCESS_RULE
    return award_xp(
        user_id, "risk_discipline", mission.id, amount,
        f"mission:{mission.id}:session:{session.id}",
        {"rules_satisfied": [r["type"] for r in satisfied],
         "mission_slug": mission.slug},
    )


# ── learning path ──────────────────────────────────────────────────────────
def award_lesson_xp(user_id, item_id):
    """Curriculum items come through one endpoint; checks pay the check rate."""
    is_check = str(item_id).startswith("check_")
    return award_xp(
        user_id,
        "topic_check" if is_check else "lesson_complete",
        item_id,
        xp_rules.UNIT_CHECK_PASS if is_check else xp_rules.LESSON_COMPLETE,
        f"{'check' if is_check else 'lesson'}:{user_id}:{item_id}",
    )


def award_spot_check_xp(user_id, lesson_id):
    return award_xp(user_id, "topic_check", lesson_id, xp_rules.SPOT_CHECK_PASS,
                    f"spot:{user_id}:{lesson_id}", {"kind": "spot_check"})


# ── one-shot legacy migration ──────────────────────────────────────────────
def import_legacy_xp(user_id, claimed_xp):
    """Import XP that only ever existed in the learner's browser.

    This value was client state and is therefore forgeable, so it is clamped to
    the top of the old rank table and keyed so it can fire exactly once, ever.
    """
    amount = max(0, min(int(claimed_xp or 0), xp_rules.LEGACY_IMPORT_CAP))
    return award_xp(user_id, "legacy_import", None, amount,
                    f"legacy:{user_id}",
                    {"claimed": int(claimed_xp or 0),
                     "cap": xp_rules.LEGACY_IMPORT_CAP})
