"""Bar provider — the single source of a scenario's bars (Phase 2).

Generated scenarios (engine_version set) hold NO bar rows; their bars are
regenerated deterministically from (engine_version, seed, params) and cached in
memory. Real-market / legacy scenarios read from the scenario_bars table. Every
bar-read path in the app goes through here, so the two kinds are interchangeable
downstream (playback, advance, replay, contests).

Bars are returned as lightweight immutable `Bar` tuples with the same attribute
names as the ORM rows (b.bar_sequence / b.open / …), so call sites are unchanged.

Multi-timeframe (Phase 2): intraday scenarios store a 1-minute base series. Any
higher timeframe (5m/15m/30m/1h/4h) is *aggregated* from the base bars on read
(open=first, high=max, low=min, close=last, volume=sum), so the timeframes can
never disagree. Aggregation is reveal-aware: passing up_to_base only aggregates
already-revealed base bars, so the current higher-TF candle forms from revealed
minutes only — no future bar can leak into a coarser timeframe.
"""
from collections import namedtuple
from functools import lru_cache

Bar = namedtuple("Bar", "bar_sequence open high low close volume")

# Minutes per bar for each supported timeframe (relative to the 1m base).
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


@lru_cache(maxsize=512)
def _generated(engine_version, params_items):
    """Cached generation keyed by (engine_version, sorted gen_params items).
    params_items is a hashable tuple so the LRU cache can key on it."""
    from app import engine
    raw = engine.generate(engine_version, dict(params_items))
    return tuple(Bar(i, b["open"], b["high"], b["low"], b["close"], b.get("volume"))
                 for i, b in enumerate(raw))


def series(scenario):
    """Full ordered BASE bar series (generated-and-cached or from rows). For
    intraday scenarios this is the 1-minute series; higher timeframes come from
    series_tf()."""
    if scenario.engine_version:
        p = dict(scenario.gen_params or {})
        p["seed"] = scenario.seed          # seed lives in its own column
        return _generated(scenario.engine_version, tuple(sorted(p.items())))
    from app.models.scenario import ScenarioBar
    rows = (ScenarioBar.query.filter_by(scenario_id=scenario.id)
            .order_by(ScenarioBar.bar_sequence).all())
    return tuple(Bar(r.bar_sequence, r.open, r.high, r.low, r.close, r.volume) for r in rows)


def count(scenario):
    return len(series(scenario))


def upto(scenario, up_to):
    """Base bars with bar_sequence <= up_to (used for server-authoritative reveal)."""
    return [b for b in series(scenario) if up_to is None or b.bar_sequence <= up_to]


def at(scenario, seq):
    s = series(scenario)
    return s[seq] if 0 <= seq < len(s) and s[seq].bar_sequence == seq else next(
        (b for b in s if b.bar_sequence == seq), None)


# ── Multi-timeframe aggregation ───────────────────────────────────────────

def aggregate(base, mult, up_to_base=None):
    """Aggregate a base (1m) Bar series into `mult`-minute candles.

    Bucket i covers base sequences [i*mult, i*mult + mult - 1]. When up_to_base
    is given, only base bars with bar_sequence <= up_to_base contribute, so the
    final bucket is a *partial* candle formed from revealed bars only — no future
    bar can leak up through the aggregation. Returns dicts carrying base_start/
    base_end so a click on a coarse candle maps back to a base bar."""
    if mult <= 1:
        return [dict(bar_sequence=b.bar_sequence, open=b.open, high=b.high,
                     low=b.low, close=b.close, volume=b.volume,
                     base_start=b.bar_sequence, base_end=b.bar_sequence)
                for b in base if up_to_base is None or b.bar_sequence <= up_to_base]

    buckets = {}
    for b in base:
        if up_to_base is not None and b.bar_sequence > up_to_base:
            break                                  # base is ordered → done
        buckets.setdefault(b.bar_sequence // mult, []).append(b)

    out = []
    for idx in sorted(buckets):
        grp = buckets[idx]
        vols = [g.volume for g in grp if g.volume is not None]
        out.append(dict(
            bar_sequence=idx,
            open=grp[0].open,
            high=max(g.high for g in grp),
            low=min(g.low for g in grp),
            close=grp[-1].close,
            volume=round(sum(vols), 2) if vols else None,
            base_start=grp[0].bar_sequence,
            base_end=grp[-1].bar_sequence,
        ))
    return out


def series_tf(scenario, tf, up_to_base=None):
    """Aggregated candle series for a timeframe (dicts). tf must be in TF_MINUTES;
    an unknown timeframe falls back to the base series."""
    return aggregate(series(scenario), TF_MINUTES.get(tf, 1), up_to_base)


def available_timeframes(scenario):
    """Timeframes a scenario can be viewed on. Multi-TF intraday scenarios carry
    an explicit list; every other scenario is single-timeframe."""
    tfs = getattr(scenario, "available_timeframes", None)
    return list(tfs) if tfs else [scenario.timeframe]


def base_timeframe(scenario):
    return getattr(scenario, "base_timeframe", None) or scenario.timeframe
