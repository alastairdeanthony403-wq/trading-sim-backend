"""Synthetic market generator (Phase E step 1).

Produces regime-switching OHLCV series server-side so we can mint unlimited
scenarios without hitting API limits and without players memorising real
history. The model is a regime-switched geometric Brownian motion with
volatility clustering and occasional jumps/gaps — simple, dependency-free
(standard library only), and visually plausible.

Each scenario is built from a *phase plan*: an ordered list of regimes the
market moves through (e.g. a "crash" scenario is a calm rally that rolls over
into a sharp sell-off and choppy aftermath). Scenarios are tagged by their
headline regime so missions can request "a crash scenario".
"""
import math
import random

# Headline regimes a caller can ask for.
REGIMES = ["trend_up", "trend_down", "range", "high_vol", "crash", "bubble_pop"]

# Per-bar drift (mu) and base volatility (sigma) in log-return terms.
REGIME_PARAMS = {
    "trend_up":   {"mu":  0.0011, "sigma": 0.009, "jump_p": 0.01, "jump": -0.03},
    "trend_down": {"mu": -0.0012, "sigma": 0.012, "jump_p": 0.02, "jump": -0.04},
    "range":      {"mu":  0.0000, "sigma": 0.007, "jump_p": 0.00, "jump":  0.00},
    "high_vol":   {"mu":  0.0000, "sigma": 0.028, "jump_p": 0.04, "jump": -0.05},
    "crash":      {"mu": -0.0060, "sigma": 0.024, "jump_p": 0.10, "jump": -0.09},
    "bubble":     {"mu":  0.0075, "sigma": 0.016, "jump_p": 0.02, "jump":  0.06},
}

# Difficulty tier by headline regime (calmer = easier to trade well).
REGIME_TIER = {
    "range": 1, "trend_up": 1, "trend_down": 2,
    "high_vol": 2, "bubble_pop": 2, "crash": 3,
}


def _phase_plan(regime, n, rng):
    """Return a list of length n giving the active regime at each bar. Headline
    regimes like 'crash'/'bubble_pop' are scripted as a sequence of phases; the
    plain regimes run throughout with light variation."""
    def blocks(spec):
        out = []
        for reg, frac in spec:
            out += [reg] * max(1, int(round(frac * n)))
        # pad/trim to exactly n
        while len(out) < n:
            out.append(spec[-1][0])
        return out[:n]

    if regime == "crash":
        return blocks([("trend_up", 0.42), ("range", 0.18),
                       ("crash", 0.15), ("high_vol", 0.25)])
    if regime == "bubble_pop":
        return blocks([("trend_up", 0.28), ("bubble", 0.34),
                       ("crash", 0.12), ("trend_down", 0.26)])
    if regime == "trend_up":
        # steady climb with occasional range pullbacks
        return [("range" if rng.random() < 0.18 else "trend_up") for _ in range(n)]
    if regime == "trend_down":
        return [("range" if rng.random() < 0.18 else "trend_down") for _ in range(n)]
    if regime == "high_vol":
        return ["high_vol"] * n
    # range (default): mean-reverting throughout
    return ["range"] * n


def generate_series(regime="range", n_bars=120, seed=None, start_price=100.0,
                    events=None):
    """Generate an OHLCV series for a headline regime. Deterministic for a given
    seed. Guarantees positive prices and OHLC consistency
    (low <= min(o,c) <= max(o,c) <= high).

    `events` (optional) is a list of {bar, sentiment, impact} dicts — a scripted
    news reaction: at the event bar the price jumps by sentiment*impact and
    volatility spikes for the following few bars (the whipsaw the lessons teach).
    """
    rng = random.Random(seed)
    plan = _phase_plan(regime, n_bars, rng)
    ev_by_bar = {int(e["bar"]): e for e in (events or [])}

    bars = []
    price = float(start_price)
    vol_mult = 1.0          # volatility-clustering state (GARCH-ish persistence)
    # range-regime anchor for mean reversion / false breakouts
    anchor = price

    for i in range(n_bars):
        reg = plan[i]
        p = REGIME_PARAMS[reg]
        # Volatility clustering: decay toward 1, spike after a big shock.
        sigma = p["sigma"] * vol_mult
        z = rng.gauss(0, 1)

        mu = p["mu"]
        if reg == "range":
            # pull back toward the anchor so it oscillates instead of drifting
            mu += -0.06 * math.log(price / anchor)
        elif reg == "bubble":
            # accelerate as the bubble inflates
            mu *= 1.0 + 0.5 * (i / n_bars)

        log_ret = mu + sigma * z
        # occasional jump/gap
        if rng.random() < p["jump_p"]:
            log_ret += p["jump"] * (0.5 + rng.random())

        # scripted news reaction: a signed shock + a volatility spike/whipsaw
        ev = ev_by_bar.get(i)
        if ev is not None:
            log_ret += ev.get("sentiment", 0) * ev.get("impact", 0.05)
            sigma *= 2.2
            vol_mult = max(vol_mult, 2.4)

        open_ = price
        # small gap between bars sometimes (open away from prior close)
        if i > 0 and rng.random() < 0.06:
            open_ = price * math.exp(sigma * rng.gauss(0, 1) * 0.8)

        close = max(1e-6, open_ * math.exp(log_ret))

        hi_base = max(open_, close)
        lo_base = min(open_, close)
        high = hi_base * math.exp(abs(rng.gauss(0, 1)) * sigma * 0.7)
        low = lo_base * math.exp(-abs(rng.gauss(0, 1)) * sigma * 0.7)
        low = max(1e-6, min(low, lo_base))
        high = max(high, hi_base)

        rng_pct = (high - low) / close if close else 0.0
        volume = round(1000 * (1 + 6 * rng_pct) * (0.6 + 0.8 * rng.random()), 2)

        bars.append({
            "open": round(open_, 4), "high": round(high, 4),
            "low": round(low, 4), "close": round(close, 4), "volume": volume,
        })

        # update state for next bar
        price = close
        vol_mult = 0.82 * vol_mult + 0.18 * (1.0 + 1.4 * abs(z))
        vol_mult = min(vol_mult, 4.0)
        if reg != "range":
            anchor = price   # only the range regime mean-reverts

    return bars


def make_scenario_spec(regime, seed):
    """Metadata for a generated scenario: internal name, difficulty tier, tags."""
    tier = REGIME_TIER.get(regime, 1)
    return {
        "name_internal": f"synthetic_{regime}_{seed}",
        "difficulty_tier": tier,
        "tags": ["synthetic", regime],
    }


# ── News events (Phase E step 2) ──────────────────────────────────────────
# Clearly-fictional headlines. Each has a category, a sentiment (+1 bullish /
# -1 bearish), and a rough reaction size. Copy is neutral/educational — it
# describes what markets do, never "how to profit".
NEWS_TEMPLATES = [
    {"category": "rate_decision", "sentiment": -1, "impact": 0.055,
     "headline": "Central bank hikes rates more than expected",
     "detail": "Policymakers surprise with a larger hike; risk assets sell off and volatility jumps."},
    {"category": "rate_decision", "sentiment": 1, "impact": 0.045,
     "headline": "Surprise rate cut lifts sentiment",
     "detail": "An unexpected cut sparks a relief rally — watch for a fade once the initial spike passes."},
    {"category": "earnings", "sentiment": 1, "impact": 0.06,
     "headline": "Northwind Industries beats earnings expectations",
     "detail": "Results top forecasts; the gap-up can whipsaw as early buyers take profit."},
    {"category": "earnings", "sentiment": -1, "impact": 0.065,
     "headline": "Meridian Corp misses and cuts guidance",
     "detail": "A miss plus weak guidance; spreads widen and the drop can overshoot."},
    {"category": "scandal", "sentiment": -1, "impact": 0.08,
     "headline": "Regulator opens accounting probe into Vantel Group",
     "detail": "Headline risk in its purest form — sharp, disorderly moves and blown-out spreads."},
    {"category": "hype", "sentiment": 1, "impact": 0.05,
     "headline": "Sector goes viral as retail piles in",
     "detail": "A hype wave on thin conviction; moves are fast and reversals are faster."},
    {"category": "recession", "sentiment": -1, "impact": 0.05,
     "headline": "GDP contracts, recession fears mount",
     "detail": "Macro fear broadens the sell-off; trends can persist but with violent counter-rallies."},
]


def build_news_scenario(seed, n_bars=140, regime=None):
    """Build a 'Scenario Mode' series with 3–5 scripted news events baked into
    the price. Returns (bars, events) where each event carries its headline
    metadata AND the bar it breaks on, and the bars already contain the
    reaction. Deterministic for a given seed."""
    rng = random.Random(seed)
    base_regime = regime or rng.choice(["trend_up", "range", "trend_down", "high_vol"])

    n_events = rng.randint(3, 5)
    # space events out, keeping clear of the very start/end
    lo, hi = int(n_bars * 0.15), int(n_bars * 0.9)
    bars_for_events = sorted(rng.sample(range(lo, hi), n_events))

    events = []
    for bar in bars_for_events:
        tpl = rng.choice(NEWS_TEMPLATES)
        events.append({
            "bar": bar,
            "category": tpl["category"],
            "headline": tpl["headline"],
            "detail": tpl["detail"],
            "sentiment": tpl["sentiment"],
            "impact": round(tpl["impact"] * (0.8 + 0.4 * rng.random()), 4),
        })

    bars = generate_series(regime=base_regime, n_bars=n_bars, seed=seed, events=events)
    return bars, events


# ── Scam / pump-and-dump scenarios (Phase E step 3) ──────────────────────
# Fictional promoter handles for the hype feed. The point of these scenarios is
# to teach RECOGNITION of a pump-and-dump, never how to run one — the debrief
# spells out the tells and the copy stays satirical of hype, not instructional.
SHILL_HANDLES = ["@MoonBoyCapital", "@AlphaGuru", "@100xCaller",
                 "@DiamondHandsDan", "@EarlyWhale"]
SHILL_POSTS = [
    "Accumulating quietly here 👀 you didn't hear it from me",
    "This one is about to run — don't miss the boat 🚀",
    "Still SO early. Generational wealth incoming.",
    "Weak hands getting shaken out. We only go up from here.",
    "If you're not in yet, what are you even doing? 💎🙌",
    "Told you. Screenshot this. Next stop the moon.",
]


def build_scam_scenario(seed, n_bars=120, start_price=20.0):
    """A pump-and-dump: a quiet base, an accelerating ramp on THIN volume while
    promoters hype it, then a violent rug when liquidity vanishes. Returns
    (bars, events) where events are the escalating hype posts plus a 'rug'
    marker at the top. Deterministic for a given seed."""
    rng = random.Random(seed)
    pump_start = int(n_bars * 0.30)
    rug_bar = int(n_bars * 0.66)
    rug_len = max(4, int(n_bars * 0.08))

    bars = []
    price = float(start_price)
    for i in range(n_bars):
        if i < pump_start:
            mu, sigma, volf = 0.0004, 0.010, 1.0         # quiet base, normal volume
        elif i < rug_bar:
            prog = (i - pump_start) / max(1, (rug_bar - pump_start))
            mu, sigma, volf = 0.020 + 0.030 * prog, 0.022, 0.35   # ramp on THIN volume
        elif i < rug_bar + rug_len:
            mu, sigma, volf = -0.16, 0.05, 3.2           # the rug: crash on huge volume
        else:
            mu, sigma, volf = -0.004, 0.020, 0.5         # dead, drifting lower

        z = rng.gauss(0, 1)
        log_ret = mu + sigma * z
        open_ = price
        close = max(1e-4, open_ * math.exp(log_ret))
        hi = max(open_, close) * math.exp(abs(rng.gauss(0, 1)) * sigma * 0.6)
        lo = min(open_, close) * math.exp(-abs(rng.gauss(0, 1)) * sigma * 0.6)
        lo = max(1e-4, min(lo, min(open_, close)))
        hi = max(hi, max(open_, close))
        volume = round(1000 * volf * (0.7 + 0.6 * rng.random()), 2)

        bars.append({"open": round(open_, 4), "high": round(hi, 4),
                     "low": round(lo, 4), "close": round(close, 4), "volume": volume})
        price = close

    # Escalating hype through the pump, then the rug alert at the top.
    events = []
    n_posts = min(len(SHILL_POSTS), 5)
    for k in range(n_posts):
        bar = pump_start + int((rug_bar - pump_start) * (k + 1) / (n_posts + 1))
        events.append({
            "bar": bar, "category": "hype", "sentiment": 1, "impact": 0.0,
            "headline": SHILL_POSTS[k],
            "detail": rng.choice(SHILL_HANDLES),
        })
    events.append({
        "bar": rug_bar, "category": "rug", "sentiment": -1, "impact": 0.0,
        "headline": "Liquidity pulled — the price collapses",
        "detail": "The promoters went quiet at the top. This is the rug.",
    })
    return bars, events


# Recognise-a-scam checklist used in the post-scenario debrief.
SCAM_ANATOMY = [
    "The ramp came on THIN volume — few real buyers, easy to push.",
    "Anonymous promoters posted escalating urgency: 'don't miss out', 'so early'.",
    "Promises of fast, guaranteed, life-changing gains — and no fundamentals.",
    "The loudest voices went silent right at the top.",
    "When liquidity vanished, the exit was a cliff, not a slope.",
]
