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


def generate_series(regime="range", n_bars=120, seed=None, start_price=100.0):
    """Generate an OHLCV series for a headline regime. Deterministic for a given
    seed. Guarantees positive prices and OHLC consistency
    (low <= min(o,c) <= max(o,c) <= high)."""
    rng = random.Random(seed)
    plan = _phase_plan(regime, n_bars, rng)

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
