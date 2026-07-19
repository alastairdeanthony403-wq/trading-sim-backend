"""Bar provider — the single source of a scenario's bars (Phase 2).

Generated scenarios (engine_version set) hold NO bar rows; their bars are
regenerated deterministically from (engine_version, seed, params) and cached in
memory. Real-market / legacy scenarios read from the scenario_bars table. Every
bar-read path in the app goes through here, so the two kinds are interchangeable
downstream (playback, advance, replay, contests).

Bars are returned as lightweight immutable `Bar` tuples with the same attribute
names as the ORM rows (b.bar_sequence / b.open / …), so call sites are unchanged.
"""
from collections import namedtuple
from functools import lru_cache

Bar = namedtuple("Bar", "bar_sequence open high low close volume")


@lru_cache(maxsize=512)
def _generated(engine_version, kind, seed, n_bars, regime, start_price):
    from app import engine
    raw = engine.generate(engine_version, {
        "kind": kind, "seed": seed, "n_bars": n_bars,
        "regime": regime, "start_price": start_price,
    })
    return tuple(Bar(i, b["open"], b["high"], b["low"], b["close"], b.get("volume"))
                 for i, b in enumerate(raw))


def series(scenario):
    """Full ordered bar series for a scenario (generated-and-cached or from rows)."""
    if scenario.engine_version:
        p = scenario.gen_params or {}
        return _generated(scenario.engine_version, p.get("kind", "regime"),
                          scenario.seed, int(p.get("n_bars")),
                          p.get("regime"), float(p.get("start_price", 100.0)))
    from app.models.scenario import ScenarioBar
    rows = (ScenarioBar.query.filter_by(scenario_id=scenario.id)
            .order_by(ScenarioBar.bar_sequence).all())
    return tuple(Bar(r.bar_sequence, r.open, r.high, r.low, r.close, r.volume) for r in rows)


def count(scenario):
    return len(series(scenario))


def upto(scenario, up_to):
    """Bars with bar_sequence <= up_to (used for server-authoritative reveal)."""
    return [b for b in series(scenario) if up_to is None or b.bar_sequence <= up_to]


def at(scenario, seq):
    s = series(scenario)
    return s[seq] if 0 <= seq < len(s) and s[seq].bar_sequence == seq else next(
        (b for b in s if b.bar_sequence == seq), None)
