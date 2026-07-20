"""Market-structure metadata (Phase 3) — derived, SERVER-SIDE ONLY.

Given a scenario's OHLC bar series, derive the structural context a trader reads:

  * swing pivots (Williams fractals) — local highs/lows,
  * horizontal support/resistance LEVELS — clustered pivots price revisits,
  * liquidity SWEEPS — a wick pokes just beyond a prior swing then the bar closes
    back (a stop-hunt / failed grab of the obvious level),
  * FAILED BREAKOUTS — price closes beyond a level then closes back within a few
    bars (the breakout that traps).

Everything is a pure function of the bars, so it is deterministic, needs no
storage, and works for both seed-only (generated) and row-based (real-market)
scenarios.

IMPORTANT — this layer is SERVER-SIDE ONLY. It must never be sent to the client
during live play; knowing where the sweeps and levels are would leak the read.
It surfaces only post-session (replay / coach), where the scenario is already
fully revealed, so structure can teach without leaking future information.
"""


def swing_points(bars, span=3):
    """Williams-fractal pivots: bar i is a swing high if its high is strictly the
    highest of the `span` bars on each side (and symmetrically for swing lows).
    Returns [{bar_sequence, price, kind: high|low}] in time order."""
    bars = list(bars)
    n = len(bars)
    out = []
    for i in range(span, n - span):
        b = bars[i]
        neigh = bars[i - span:i] + bars[i + 1:i + 1 + span]
        if b.high > max(x.high for x in neigh):
            out.append({"bar_sequence": b.bar_sequence, "price": round(b.high, 4), "kind": "high"})
        if b.low < min(x.low for x in neigh):
            out.append({"bar_sequence": b.bar_sequence, "price": round(b.low, 4), "kind": "low"})
    return out


def levels(swings, tol=0.005, min_touches=2):
    """Cluster swing prices that sit within `tol` (relative) into horizontal
    levels the market has revisited. A level touched by both highs and lows is a
    'flip' level (former resistance turned support or vice-versa). Returns the
    strongest (most-touched) first."""
    if not swings:
        return []
    pts = sorted(swings, key=lambda s: s["price"])
    clusters = [[pts[0]]]
    for p in pts[1:]:
        anchor = clusters[-1][-1]["price"]
        if anchor > 0 and abs(p["price"] - anchor) / anchor <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    out = []
    for c in clusters:
        if len(c) < min_touches:
            continue
        kinds = {p["kind"] for p in c}
        seqs = [p["bar_sequence"] for p in c]
        out.append({
            "price": round(sum(p["price"] for p in c) / len(c), 4),
            "touches": len(c),
            "kind": "flip" if len(kinds) > 1 else ("resistance" if "high" in kinds else "support"),
            "first_bar": min(seqs),
            "last_bar": max(seqs),
        })
    return sorted(out, key=lambda lv: -lv["touches"])


def liquidity_sweeps(bars, swings, eps=0.0006):
    """For each swing, find the first later bar that either SWEEPS it (a wick pokes
    beyond the level but the bar closes back on the original side — a stop-hunt) or
    genuinely breaks it (a close beyond, which retires the level). Only sweeps are
    returned. Deterministic; one event per swing at most."""
    bars = list(bars)
    idx = {b.bar_sequence: k for k, b in enumerate(bars)}
    out = []
    seen = set()
    for s in swings:
        start = idx.get(s["bar_sequence"])
        if start is None:
            continue
        lvl = s["price"]
        for b in bars[start + 1:]:
            if s["kind"] == "high":
                if b.high > lvl * (1 + eps) and b.close < lvl:
                    ev = (b.bar_sequence, "high")
                    if ev not in seen:
                        seen.add(ev)
                        out.append({"bar_sequence": b.bar_sequence, "price": round(lvl, 4),
                                    "side": "high", "penetration": round(b.high - lvl, 4)})
                    break
                if b.close > lvl * (1 + eps):
                    break                       # real breakout — level taken out
            else:
                if b.low < lvl * (1 - eps) and b.close > lvl:
                    ev = (b.bar_sequence, "low")
                    if ev not in seen:
                        seen.add(ev)
                        out.append({"bar_sequence": b.bar_sequence, "price": round(lvl, 4),
                                    "side": "low", "penetration": round(lvl - b.low, 4)})
                    break
                if b.close < lvl * (1 - eps):
                    break
    return sorted(out, key=lambda x: x["bar_sequence"])


def _side(close, lvl, eps):
    """Which side of a level a close sits on: +1 above, -1 below, 0 on it."""
    if close > lvl * (1 + eps):
        return 1
    if close < lvl * (1 - eps):
        return -1
    return 0


def failed_breakouts(bars, lvls, within=5, eps=0.0006):
    """The breakout that traps: price CROSSES a level (a close moves to the other
    side of where it was), then closes back to the original side within `within`
    bars. First such failure per level."""
    bars = list(bars)
    out = []
    for lv in lvls:
        lvl = lv["price"]
        prev = 0                                # last decided side (-1/+1)
        for k, b in enumerate(bars):
            side = _side(b.close, lvl, eps)
            if side == 0:
                continue
            if prev == 0:
                prev = side
                continue
            if side != prev:                    # a cross to the other side (breakout)
                recovered = None
                for f in bars[k + 1:k + 1 + within]:
                    if _side(f.close, lvl, eps) == prev:
                        recovered = f.bar_sequence
                        break
                if recovered is not None:
                    out.append({"bar_sequence": b.bar_sequence, "price": round(lvl, 4),
                                "side": "up" if side > 0 else "down",
                                "recovered_bar": recovered})
                    break                       # first failed breakout of this level
                prev = side                     # breakout held — carry on from new side
    return out


def annotate(bars):
    """Full structural read of a bar series (post-session use only)."""
    bars = list(bars)
    sw = swing_points(bars)
    lv = levels(sw)
    return {
        "swings": sw,
        "levels": lv,
        "sweeps": liquidity_sweeps(bars, sw),
        "failed_breakouts": failed_breakouts(bars, lv),
    }
