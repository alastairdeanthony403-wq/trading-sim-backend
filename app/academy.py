"""Academy × engine merge (Phase 1) — concept→scenario registry + grading.

Each end-of-unit knowledge check embeds a live market scenario that matches the
concept just taught: a support/resistance unit yields a ranging market, a trend
unit a trending one, a volatility/news unit a high-vol one. The learner has to
demonstrate the concept in the simulator, graded SERVER-SIDE by the existing
Phase-B mission rule engine (`app.rules.check_mission_rules`) — this module only
supplies the concept→regime mapping and the demonstration rule set, it does not
re-implement grading.

Grading is risk/discipline-shaped (stop on every trade, sized risk, no revenge,
survived, actually took a trade) evaluated in the concept's regime — the concept
match comes from the market context, the pass gate from disciplined execution.
Structure metadata (Phase 3) is available and surfaced as advisory info, but the
pass/fail gate is the mission rules, so a check never flakes on setup timing.
"""


# ── Mission-engine rule builders (Phase-B rule schema: {type, param, label}) ──
def _stop():           return {"type": "require_stop_on_all", "label": "Every position had a stop-loss"}
def _no_revenge():     return {"type": "no_revenge", "label": "No revenge trade after a loss"}
def _min_trades(n=1):  return {"type": "min_trades", "param": n, "label": f"Took at least {n} trade"}
def _max_trades(n):    return {"type": "max_trades", "param": n, "label": f"No overtrading (≤ {n} trades)"}
def _max_risk(p):      return {"type": "max_risk_pct_per_trade", "param": p, "label": f"Risked ≤ {p}% of the account per trade"}
def _max_dd(p):        return {"type": "max_drawdown_pct", "param": p, "label": f"Kept drawdown under {p}%"}


# ── Concept registry ─────────────────────────────────────────────────────────
# Each concept maps to: the regime(s) the engine should generate, the learner-
# facing goal, the demonstration rule set (graded), warm-up/live bar counts, and
# an optional advisory `structure` note. `anchor_tf` is "1D" for Phase 1 (single
# timeframe); switching a concept to intraday later is a config change here.
CONCEPTS = {
    "support_resistance": {
        "regimes": ["range"],
        "goal": "This market is ranging. Trade a level — enter near support or resistance "
                "with a defined stop, and don't chase price through the middle of the range.",
        "rules": [_stop(), _no_revenge(), _min_trades(1), _max_risk(3), _max_dd(15)],
        "warmup_bars": 40, "live_bars": 30, "anchor_tf": "1D", "structure": "level",
    },
    "trend_following": {
        "regimes": ["trend_up", "trend_down"],
        "goal": "This market is trending. Trade with the trend, not against it — enter on a "
                "pullback with a stop, and give the trend room to work.",
        "rules": [_stop(), _no_revenge(), _min_trades(1), _max_risk(3), _max_dd(15)],
        "warmup_bars": 40, "live_bars": 30, "anchor_tf": "1D", "structure": "bos",
    },
    "liquidity_sweeps": {
        "regimes": ["range"],
        "goal": "Structure & liquidity. Let the level be tested or swept first, then enter "
                "with your stop beyond the sweep — don't get caught chasing the fake-out.",
        "rules": [_stop(), _no_revenge(), _min_trades(1), _max_dd(12)],
        "warmup_bars": 40, "live_bars": 30, "anchor_tf": "1D", "structure": "sweep",
    },
    "risk_stops": {
        "regimes": ["range", "trend_up", "high_vol"],
        "goal": "Size every trade by risk — pick your invalidation first, keep the risk small, "
                "and always trade with a stop.",
        "rules": [_stop(), _max_risk(2), _no_revenge(), _min_trades(1)],
        "warmup_bars": 40, "live_bars": 30, "anchor_tf": "1D", "structure": None,
    },
    "volatility_news": {
        "regimes": ["high_vol"],
        "goal": "Volatility is high. Survive it — keep risk tight, always use a stop, and don't "
                "let a single trade blow up the account.",
        "rules": [_stop(), _max_risk(2), _max_dd(10), _min_trades(1)],
        "warmup_bars": 40, "live_bars": 30, "anchor_tf": "1D", "structure": None,
    },
    "discipline": {
        "regimes": ["range", "high_vol"],
        "goal": "A disciplined dress rehearsal — every trade gets a stop, no revenge after a "
                "loss, and no overtrading. Treat it like real money.",
        "rules": [_stop(), _no_revenge(), _max_trades(5), _min_trades(1)],
        "warmup_bars": 40, "live_bars": 30, "anchor_tf": "1D", "structure": None,
    },
}

# Each end-of-unit check → the concept its practice scenario demonstrates.
CHECK_CONCEPT = {
    "check_foundations":  "risk_stops",         # fallback: no bespoke concept for foundations
    "check_reading":      "support_resistance",
    "check_structure":    "liquidity_sweeps",
    "check_risk":         "risk_stops",
    "check_discipline":   "discipline",
    "check_fundamentals": "volatility_news",
}

# Checks that fell back to the nearest concept rather than a bespoke one.
FALLBACK_CHECKS = {"check_foundations"}


def concept_for_check(check_id):
    return CHECK_CONCEPT.get(check_id, "risk_stops")


def spec_for(concept_tag):
    return CONCEPTS.get(concept_tag)


def concept_of_scenario(scenario):
    """Recover the concept tag stored on a practice scenario's tags."""
    for t in (scenario.tags or []):
        if t in CONCEPTS:
            return t
    return None


def grade(spec, session):
    """Grade a finished practice session against its concept's rule set, using the
    existing Phase-B mission engine. Returns (passed, rule_results, discipline)."""
    from app.rules import evaluate_discipline, session_context, check_mission_rules
    disc = evaluate_discipline(session)
    ctx = session_context(session, disc)
    passed, results = check_mission_rules(spec["rules"], ctx)
    return passed, results, disc
