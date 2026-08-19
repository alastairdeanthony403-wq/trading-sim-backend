"""The XP rubric — every point value in the product, in one place.

DESIGN RULE (non-negotiable): XP rewards process, never outcome.

Nothing in this module reads profit, P&L, win rate, return %, or account
equity. A learner who follows their plan and loses money earns MORE than one
who violates it and profits, because the only things that pay are: showing up,
finishing the learning path, defining risk before entering, sizing sanely, not
revenge trading, and honouring the stop they declared.

`app.rules.check_mission_rules` returns a per-rule verdict; PROCESS_RULE_TYPES
below decides which of those verdicts are allowed to pay. `min_return_pct` is
deliberately absent — the six seeded missions that use it keep their rules and
their pass/fail behaviour untouched, but their profit clause earns nothing.
"""

# ── learning path ──────────────────────────────────────────────────────────
LESSON_COMPLETE = 20
UNIT_CHECK_PASS = 60
SPOT_CHECK_PASS = 30

# ── trading sessions (career, contest, practice AND paper) ─────────────────
# Participation. Outcome-independent by construction: finishing a losing
# session pays exactly what finishing a winning one pays.
SESSION_COMPLETE = 10

# Process components, each awarded only if the session earned it.
STOP_ON_EVERY_TRADE = 25      # no_stop_count == 0
NO_OVERSIZED_RISK = 25        # oversize_count == 0
NO_REVENGE_TRADING = 15       # revenge_count == 0

# Risk-adjusted band, read off discipline_score (0..100) — the one session
# metric with no profit term in it. score_composite is NOT used here: it mixes
# in return %, win rate and Sharpe, and would smuggle outcome back in.
DISCIPLINE_BANDS = [(80.0, 20), (60.0, 10)]   # (min score, xp), best first

# A session with no closed trades is not a process demonstration — it pays the
# participation award only, so opening and abandoning runs earns nothing extra.
MIN_TRADES_FOR_PROCESS_XP = 1

# ── plan adherence ─────────────────────────────────────────────────────────
# The stop declared when the position was opened was never widened. Requires
# trades.entry_stop_loss, captured at entry and never rewritten.
PLAN_ADHERENCE = 20           # per session, all trades honoured their entry stop

# ── missions ───────────────────────────────────────────────────────────────
# Paid per satisfied PROCESS rule, whether or not the mission passed overall.
# A learner who honours every risk rule but misses the mission's profit target
# still banks the discipline they actually demonstrated.
MISSION_PROCESS_RULE = 10

PROCESS_RULE_TYPES = frozenset({
    "require_stop_on_all",
    "max_risk_pct_per_trade",
    "max_drawdown_pct",       # a sizing outcome, not a profit one: a learner can
                              # lose money with a small drawdown and pass this
    "no_revenge",
    "max_trades",
    "min_trades",
})

# Explicitly excluded from every award path. Kept named so the exclusion is
# searchable and the negative test can assert against it.
OUTCOME_RULE_TYPES = frozenset({"min_return_pct"})

# ── caps ───────────────────────────────────────────────────────────────────
# Paper trading is self-serve, unlimited, and as short as 5 minutes, so a flat
# per-session award would make grinding short runs the optimal strategy. Paper
# pays the same rubric as anything else, up to a daily ceiling.
PAPER_DAILY_XP_CAP = 120

# One-shot migration of XP that only ever existed in the learner's browser.
# It is forgeable by design (it was client state), so it is clamped to the top
# of the legacy rank table and can fire exactly once per learner.
LEGACY_IMPORT_CAP = 1400


def discipline_band_xp(discipline_score):
    """XP for the risk-adjusted band a session landed in (0 if below all bands)."""
    if discipline_score is None:
        return 0
    for minimum, xp in DISCIPLINE_BANDS:
        if discipline_score >= minimum:
            return xp
    return 0
