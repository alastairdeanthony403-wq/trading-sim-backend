"""Synthetic market engine v2 (Phase 1 — core realism).

A dependency-free (standard-library only), fully-seeded generator that produces
OHLCV series with *real* market properties rather than a plain random walk:

  * Regime switching via a Markov chain over
    {trend_up, trend_down, range, high_vol, low_vol} with persistent durations
    and SMOOTH transitions (drift/vol are blended over several bars, no snaps).
  * Volatility clustering via a GARCH(1,1) conditional variance, plus an explicit
    ATR expansion/compression cycle layered on top so candle ranges visibly cycle.
  * Fat-tailed returns via standardized Student-t innovations (excess kurtosis).
  * A market-structure layer: memory of recent swing highs/lows that price
    respects probabilistically, occasional liquidity sweeps (poke beyond a level
    then reverse) and failed breakouts.
  * A volume engine: spikes on breakouts, quiet in compression, correlated with
    range.

Candlestick shapes (dojis, pin bars, engulfing, inside/outside bars) are NOT
scripted — they emerge from the body/wick model and are verified by occurrence
tests. Determinism: same seed -> identical series (required for contests).

Public API kept stable for the rest of the app: REGIMES, generate_series,
make_scenario_spec, build_news_scenario, build_scam_scenario, SCAM_ANATOMY.
"""
import math
import random

# Markov regime set (Phase 1 adds low_vol).
REGIMES = ["trend_up", "trend_down", "range", "high_vol", "low_vol"]

# Per-regime per-bar drift (mu, in log-return terms) and volatility multiplier.
REGIME_CFG = {
    "trend_up":   {"mu":  0.00090, "vol": 1.00},
    "trend_down": {"mu": -0.00110, "vol": 1.15},
    "range":      {"mu":  0.00000, "vol": 0.80},
    "high_vol":   {"mu":  0.00000, "vol": 2.30},
    "low_vol":    {"mu":  0.00030, "vol": 0.45},
}

# Probability of STAYING in a regime each bar → mean duration ≈ 1/(1-stay).
_STAY = {"trend_up": 0.955, "trend_down": 0.945, "range": 0.955,
         "high_vol": 0.910, "low_vol": 0.960}

# Difficulty tier by headline regime (calmer = easier). Kept for make_scenario_spec.
REGIME_TIER = {"low_vol": 1, "range": 1, "trend_up": 1, "trend_down": 2, "high_vol": 3}

# GARCH(1,1) on the standardized innovation: unconditional variance = 1.
_G_OMEGA, _G_ALPHA, _G_BETA = 0.02, 0.10, 0.88   # alpha+beta = 0.98 → persistent
_T_DF = 8                                          # Student-t d.o.f. → fat tails (moderate)
_BASE_VOL = 0.0105                                 # overall per-bar vol scale
_WICK = 0.42                                       # wick size vs volatility


def _student_t(rng, df=_T_DF):
    """A standardized (unit-variance) Student-t draw — Gaussian core with a
    heavy tail. Pure stdlib: t = z / sqrt(chi2_df / df)."""
    z = rng.gauss(0, 1)
    chi2 = sum(rng.gauss(0, 1) ** 2 for _ in range(df))
    t = z / math.sqrt(chi2 / df) if chi2 > 0 else z
    return t * math.sqrt((df - 2) / df)   # rescale to unit variance


def _regime_path(n, rng, start=None, home_bias=0.55):
    """A Markov regime sequence of length n. The requested regime is the *home*
    state: the market spends most of its time there but takes excursions into
    other regimes and returns — so a 'high_vol' scenario is genuinely choppier
    than a 'low_vol' one, while switch timings differ per seed (no templates)."""
    home = start if start in REGIME_CFG else None
    cur = home or rng.choice(REGIMES)
    path = []
    for _ in range(n):
        path.append(cur)
        if rng.random() > _STAY[cur]:
            if home and cur != home and rng.random() < home_bias:
                cur = home                                    # drift back home
            else:
                cur = rng.choice([s for s in REGIMES if s != cur])
    return path


# ── Intraday trading sessions (Phase 4) ───────────────────────────────────
# Each profile splits a trading day into named sessions with a volatility
# multiplier, so intraday markets breathe with a realistic rhythm: an active
# open, a midday lull, a lively close (equities), or the Asia→London→NY handover
# with a hot London/NY overlap (FX). Bands are fractions of the day [start, end).
SESSION_PROFILES = {
    "equity": [
        {"name": "Open",      "start": 0.00, "end": 0.08, "vol": 1.7},
        {"name": "Morning",   "start": 0.08, "end": 0.30, "vol": 1.15},
        {"name": "Midday",    "start": 0.30, "end": 0.62, "vol": 0.72},
        {"name": "Afternoon", "start": 0.62, "end": 0.90, "vol": 1.0},
        {"name": "Close",     "start": 0.90, "end": 1.00, "vol": 1.5},
    ],
    "fx": [
        {"name": "Asia",         "start": 0.00, "end": 0.33, "vol": 0.75},
        {"name": "London",       "start": 0.33, "end": 0.50, "vol": 1.3},
        {"name": "LDN/NY",       "start": 0.50, "end": 0.67, "vol": 1.75},
        {"name": "New York",     "start": 0.67, "end": 0.92, "vol": 1.1},
        {"name": "Late NY",      "start": 0.92, "end": 1.00, "vol": 0.7},
    ],
}


def session_bands(profile="equity"):
    """The session layout for a profile (name/start/end/vol per session)."""
    return list(SESSION_PROFILES.get(profile, SESSION_PROFILES["equity"]))


def session_at(frac, profile="equity"):
    """Which session a within-day fraction [0,1) falls in: (name, vol_mult)."""
    prof = SESSION_PROFILES.get(profile, SESSION_PROFILES["equity"])
    for s in prof:
        if s["start"] <= frac < s["end"]:
            return s["name"], s["vol"]
    return prof[-1]["name"], prof[-1]["vol"]


def _session_vol_curve(n, bars_per_day, profile):
    """Per-bar volatility multiplier from the session profile (length n)."""
    prof = SESSION_PROFILES.get(profile, SESSION_PROFILES["equity"])
    curve = []
    for i in range(n):
        frac = (i % bars_per_day) / bars_per_day
        v = prof[-1]["vol"]
        for s in prof:
            if s["start"] <= frac < s["end"]:
                v = s["vol"]
                break
        curve.append(v)
    return curve


# ── Asset personalities (Phase 6) ─────────────────────────────────────────
# Each market class has a behavioural signature the generator honours, so a
# "crypto" scenario feels wild and gappy while "forex" is a smooth mean-reverting
# range. vol_mult scales volatility, t_df sets tail fatness (lower = fatter),
# gap_prob is the per-bar opening-gap chance, mean_rev pulls price back toward a
# slow anchor (ranging). asset=None → the neutral behaviour used everywhere so
# far (unchanged), so synthetic/legacy scenarios and their tests are untouched.
ASSET_PROFILES = {
    "crypto":      {"vol_mult": 1.7,  "t_df": 5,  "gap_prob": 0.010, "mean_rev": 0.0,
                    "label": "Wild — high volatility, fat tails, frequent gaps"},
    "forex":       {"vol_mult": 0.55, "t_df": 12, "gap_prob": 0.002, "mean_rev": 0.060,
                    "label": "Smooth — low volatility, mean-reverting ranges"},
    "indices":     {"vol_mult": 0.80, "t_df": 9,  "gap_prob": 0.006, "mean_rev": 0.004,
                    "label": "Grind — trends with low volatility and open gaps"},
    "commodities": {"vol_mult": 1.20, "t_df": 6,  "gap_prob": 0.008, "mean_rev": 0.0,
                    "label": "Spiky — sharp supply-shock moves"},
    "stocks":      {"vol_mult": 1.00, "t_df": 8,  "gap_prob": 0.007, "mean_rev": 0.0,
                    "label": "Balanced — trends with earnings gaps"},
}


def asset_profile(name):
    """Personality profile for an asset class, or None for neutral/synthetic."""
    return ASSET_PROFILES.get(name)


def correlated_line(base_bars, seed, rho, start=None):
    """A benchmark line (list of {bar_sequence, value}) correlated at ~`rho` with
    the base series' returns — e.g. a sector index the asset moves with. Built
    from the base's log-returns + seeded idiosyncratic noise, so realized
    correlation ≈ rho and it's deterministic. Teaches relative strength."""
    closes = [(b["close"] if isinstance(b, dict) else b.close) for b in base_bars]
    if len(closes) < 2:
        return [{"bar_sequence": 0, "value": round(start or (closes[0] if closes else 100.0), 4)}]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / len(rets)
    sd = math.sqrt(var) or 1e-9
    rng = random.Random(seed)
    k = math.sqrt(max(0.0, 1.0 - rho * rho))
    value = float(start or closes[0])
    line = [{"bar_sequence": 0, "value": round(value, 4)}]
    for i, r in enumerate(rets, start=1):
        zr = (r - mu) / sd
        ref_ret = mu + sd * (rho * zr + k * rng.gauss(0, 1))
        value = max(1e-6, value * math.exp(ref_ret))
        line.append({"bar_sequence": i, "value": round(value, 4)})
    return line


def generate_series(regime="range", n_bars=120, seed=None, start_price=100.0,
                    events=None, difficulty=2, gap_prob=0.0, vol_scale=1.0,
                    bar_vol=None, asset=None):
    """Generate an OHLCV series. Deterministic for a given seed. Guarantees
    positive prices and OHLC consistency (low ≤ min(o,c) ≤ max(o,c) ≤ high).

    `regime` biases the opening/dominant regime; the Markov chain still switches.
    `events` (optional): [{bar, sentiment, impact}] — a scripted news reaction
    (signed shock + volatility spike) baked into the price at that bar.
    `difficulty` (1–3) scales how often liquidity sweeps / failed breakouts fire.
    `gap_prob`: per-bar probability of an opening gap (asset personality, Phase 6).
    `vol_scale`: per-bar drift+volatility multiplier. 1.0 → the daily scale used
    everywhere so far (unchanged). Intraday 1-minute series pass a small value so
    a single bar moves like a minute, not a day; the shape of the process is
    otherwise identical (Phase 2 multi-timeframe).
    `bar_vol` (optional): a per-bar volatility multiplier sequence (length n_bars),
    e.g. an intraday session profile so the market breathes with the trading day
    (Phase 4). None → flat 1.0 everywhere.
    `asset` (optional): an asset-class personality name (crypto/forex/…) that tunes
    volatility, tail fatness, gaps and mean-reversion (Phase 6). None → neutral.
    """
    rng = random.Random(seed)
    plan = _regime_path(n_bars, rng, start=regime)
    ev_by_bar = {int(e["bar"]): e for e in (events or [])}

    # asset personality (Phase 6): neutral when asset is None → unchanged behaviour
    prof = ASSET_PROFILES.get(asset)
    a_vol = prof["vol_mult"] if prof else 1.0
    a_df = prof["t_df"] if prof else _T_DF
    a_mr = prof["mean_rev"] if prof else 0.0
    if prof and not gap_prob:
        gap_prob = prof["gap_prob"]
    anchor = float(start_price)          # slow mean-reversion anchor (forex-like)

    # smoothing state (blended drift/vol so regimes don't snap)
    tau = rng.uniform(3.0, 8.0)
    mu_eff = REGIME_CFG[plan[0]]["mu"]
    vol_eff = REGIME_CFG[plan[0]]["vol"]

    # GARCH state (variance of the standardized innovation) + last shock
    h = 1.0
    last_a = 0.0

    # ATR expansion/compression cycle (slow multiplicative oscillator)
    atr_period = rng.uniform(45, 95)
    atr_phase = rng.uniform(0, 2 * math.pi)
    atr_amp = 0.42

    # structure memory
    swing_hi = swing_lo = start_price
    sweep_bias = 0        # +/-1 reversal push for the bar(s) after a sweep
    sweep_left = 0
    sweep_freq = 0.010 + 0.010 * difficulty   # scales with difficulty

    bars = []
    price = float(start_price)
    recent_high = recent_low = price

    for i in range(n_bars):
        target = REGIME_CFG[plan[i]]
        mu_eff += (target["mu"] - mu_eff) / tau
        vol_eff += (target["vol"] - vol_eff) / tau

        # GARCH conditional variance + fat-tailed standardized innovation
        h = _G_OMEGA + _G_ALPHA * (last_a ** 2) + _G_BETA * h
        z = _student_t(rng, a_df)
        a = math.sqrt(h) * z
        last_a = a

        atr = 1.0 + atr_amp * math.sin(2 * math.pi * i / atr_period + atr_phase)
        sess = bar_vol[i] if bar_vol is not None else 1.0
        sigma = _BASE_VOL * vol_scale * a_vol * sess * vol_eff * max(0.35, atr)
        log_ret = mu_eff * vol_scale + sigma * a
        if a_mr:                      # mean-reversion pull toward the slow anchor
            log_ret += -a_mr * math.log(price / anchor)

        # scripted news reaction (kept from the old engine)
        ev = ev_by_bar.get(i)
        if ev is not None:
            log_ret += ev.get("sentiment", 0) * ev.get("impact", 0.05)
            vol_eff = max(vol_eff, 2.2)

        # structure: reversal push lingering after a liquidity sweep
        if sweep_left > 0:
            log_ret += sweep_bias * sigma * 1.1
            sweep_left -= 1

        open_ = price
        if i > 0 and rng.random() < gap_prob:
            open_ = price * math.exp(sigma * rng.gauss(0, 1) * 1.2)   # gap open

        close = max(1e-6, open_ * math.exp(log_ret))

        # ── wicks / candlestick geometry ──────────────────────────────────
        hi_base, lo_base = max(open_, close), min(open_, close)
        up_wick = abs(_student_t(rng, a_df)) * sigma * _WICK
        dn_wick = abs(_student_t(rng, a_df)) * sigma * _WICK
        high = hi_base * math.exp(up_wick)
        low = lo_base * math.exp(-dn_wick)

        # ── liquidity sweep: poke just beyond a nearby prior extreme, close back
        if sweep_left == 0 and rng.random() < sweep_freq:
            if abs(close - recent_high) / close < 0.02 and rng.random() < 0.5:
                high = max(high, recent_high * math.exp(sigma * (0.6 + rng.random())))
                close = min(close, recent_high * math.exp(-sigma * 0.3))
                sweep_bias, sweep_left = -1, rng.randint(1, 2)
            elif abs(close - recent_low) / close < 0.02:
                low = min(low, recent_low * math.exp(-sigma * (0.6 + rng.random())))
                close = max(close, recent_low * math.exp(sigma * 0.3))
                sweep_bias, sweep_left = +1, rng.randint(1, 2)

        # ── failed breakout: small break of a recent level often snaps back
        elif close > recent_high and (close - recent_high) / close < 0.006 and rng.random() < 0.35:
            close = recent_high * math.exp(-sigma * 0.2)
        elif close < recent_low and (recent_low - close) / close < 0.006 and rng.random() < 0.35:
            close = recent_low * math.exp(sigma * 0.2)

        # enforce OHLC validity after any structural adjustment
        low = max(1e-6, min(low, open_, close))
        high = max(high, open_, close)

        # ── volume: quiet base, spikes on wide-range / breakout bars ──────
        range_pct = (high - low) / close if close else 0.0
        breakout = 1.0 if abs(a) > 1.6 else 0.0
        volume = round(1000 * (0.5 + 5.0 * range_pct + 1.8 * breakout) * (0.7 + 0.6 * rng.random()), 2)

        bars.append({"open": round(open_, 4), "high": round(high, 4),
                     "low": round(low, 4), "close": round(close, 4), "volume": volume})

        # update memory
        price = close
        anchor = anchor * 0.95 + close * 0.05      # ~20-bar EMA anchor for mean reversion
        recent_high = max(recent_high * 0.985 + close * 0.015, high) if i else high
        recent_low = min(recent_low * 0.985 + close * 0.015, low) if i else low
        # track slow swing extremes for the next sweep target
        swing_hi = max(swing_hi * 0.97 + high * 0.03, high * 0.999)
        swing_lo = min(swing_lo * 0.97 + low * 0.03, low * 1.001)

    return bars


def generate_intraday_series(seed, days=7, bars_per_day=390, regime="range",
                             start_price=100.0, vol_scale=0.15,
                             session_profile="equity", events=None):
    """~`days` sessions of 1-minute bars — the source of truth for multi-timeframe
    intraday scenarios. Every higher timeframe (5m/15m/30m/1h/4h) is *aggregated*
    from these 1m bars, so the timeframes can never disagree. Per-bar volatility
    is scaled down so 1-minute candles look like minutes rather than days, and a
    trading-session profile (Phase 4) makes the day breathe — active open/close,
    quiet midday, or the FX session handover. `events` bakes scheduled news
    reactions into the price. Deterministic for a given seed."""
    n = int(days) * int(bars_per_day)
    bar_vol = _session_vol_curve(n, int(bars_per_day), session_profile) \
        if session_profile else None
    return generate_series(regime=regime, n_bars=n, seed=seed,
                           start_price=start_price, vol_scale=vol_scale,
                           bar_vol=bar_vol, events=events)


def make_scenario_spec(regime, seed):
    """Metadata for a generated scenario: internal name, difficulty tier, tags."""
    tier = REGIME_TIER.get(regime, 1)
    return {
        "name_internal": f"synthetic_{regime}_{seed}",
        "difficulty_tier": tier,
        "tags": ["synthetic", regime],
    }


# ── News events (kept from Phase E step 2) ────────────────────────────────
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
    """A 'Scenario Mode' series with 3–5 scripted news events baked into the
    price. Returns (bars, events). Deterministic for a given seed."""
    rng = random.Random(seed)
    base_regime = regime or rng.choice(["trend_up", "range", "trend_down", "high_vol"])

    n_events = rng.randint(3, 5)
    lo, hi = int(n_bars * 0.15), int(n_bars * 0.9)
    bars_for_events = sorted(rng.sample(range(lo, hi), n_events))

    events = []
    for bar in bars_for_events:
        tpl = rng.choice(NEWS_TEMPLATES)
        events.append({
            "bar": bar, "category": tpl["category"], "headline": tpl["headline"],
            "detail": tpl["detail"], "sentiment": tpl["sentiment"],
            "impact": round(tpl["impact"] * (0.8 + 0.4 * rng.random()), 4),
        })

    bars = generate_series(regime=base_regime, n_bars=n_bars, seed=seed, events=events)
    return bars, events


# ── Scam / pump-and-dump scenarios (kept from Phase E step 3) ─────────────
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
    """A pump-and-dump: quiet base → ramp on THIN volume with escalating hype →
    a violent rug. Returns (bars, events). Deterministic for a given seed."""
    rng = random.Random(seed)
    pump_start = int(n_bars * 0.30)
    rug_bar = int(n_bars * 0.66)
    rug_len = max(4, int(n_bars * 0.08))

    bars = []
    price = float(start_price)
    for i in range(n_bars):
        if i < pump_start:
            mu, sigma, volf = 0.0004, 0.010, 1.0
        elif i < rug_bar:
            prog = (i - pump_start) / max(1, (rug_bar - pump_start))
            mu, sigma, volf = 0.020 + 0.030 * prog, 0.022, 0.35
        elif i < rug_bar + rug_len:
            mu, sigma, volf = -0.16, 0.05, 3.2
        else:
            mu, sigma, volf = -0.004, 0.020, 0.5

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

    events = []
    n_posts = min(len(SHILL_POSTS), 5)
    for k in range(n_posts):
        bar = pump_start + int((rug_bar - pump_start) * (k + 1) / (n_posts + 1))
        events.append({"bar": bar, "category": "hype", "sentiment": 1, "impact": 0.0,
                       "headline": SHILL_POSTS[k], "detail": rng.choice(SHILL_HANDLES)})
    events.append({"bar": rug_bar, "category": "rug", "sentiment": -1, "impact": 0.0,
                   "headline": "Liquidity pulled — the price collapses",
                   "detail": "The promoters went quiet at the top. This is the rug."})
    return bars, events


SCAM_ANATOMY = [
    "The ramp came on THIN volume — few real buyers, easy to push.",
    "Anonymous promoters posted escalating urgency: 'don't miss out', 'so early'.",
    "Promises of fast, guaranteed, life-changing gains — and no fundamentals.",
    "The loudest voices went silent right at the top.",
    "When liquidity vanished, the exit was a cliff, not a slope.",
]
