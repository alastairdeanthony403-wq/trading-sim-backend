"""Versioned generation dispatch (Phase 2 — seed-only scenarios).

A generated scenario stores an `engine_version` alongside its seed + params.
Bars are produced by the engine registered for that version. This is the pin
that stops a future engine change from silently rewriting an existing
scenario's bars — old scenarios keep generating against the version they were
minted with (critical for reproducible, in-flight contests). New scenarios are
minted with CURRENT_ENGINE.

To evolve the engine: register a new version (e.g. "v3") in ENGINES and bump
CURRENT_ENGINE. Existing "v2" scenarios keep calling the "v2" generator.
"""
from app.synthetic import (generate_series, generate_intraday_series,
                           build_news_scenario, build_scam_scenario)

CURRENT_ENGINE = "v2"


def _v2(params):
    """params: {kind, seed, n_bars, regime?}. Returns a list of bar dicts."""
    kind = params.get("kind", "regime")
    seed = params["seed"]
    n_bars = params["n_bars"]
    if kind == "regime":
        return generate_series(regime=params.get("regime", "range"),
                               n_bars=n_bars, seed=seed,
                               start_price=params.get("start_price", 100.0))
    if kind == "intraday":
        # Intraday multi-timeframe: the stored series is 1-minute bars; higher
        # timeframes are aggregated on read (bar_provider). n_bars == days*bars_per_day.
        return generate_intraday_series(
            seed, days=params.get("days", 7),
            bars_per_day=params.get("bars_per_day", 390),
            regime=params.get("regime", "range"),
            start_price=params.get("start_price", 100.0),
            vol_scale=params.get("vol_scale", 0.15))
    if kind == "news":
        return build_news_scenario(seed, n_bars)[0]
    if kind == "scam":
        return build_scam_scenario(seed, n_bars)[0]
    raise ValueError(f"unknown generation kind: {kind}")


ENGINES = {"v2": _v2}


def generate(engine_version, params):
    """Deterministically produce a scenario's bars for the given engine version."""
    fn = ENGINES.get(engine_version)
    if fn is None:
        raise ValueError(f"unknown engine_version: {engine_version}")
    return fn(params)
