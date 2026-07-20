"""Setup quality grading (Phase 5) — A / B / C, server-side.

A trade's *setup* is graded on the confluence a disciplined trader looks for at
entry, scored from the price series + the Phase-3 structure read:

  * defined risk   — a stop is set (undefined risk caps the grade at C),
  * trend alignment — trading with the prevailing trend, not against it,
  * reward:risk    — the planned R multiple,
  * location       — entering off a level vs straight into an opposing one,
  * trigger        — entering just after a liquidity sweep reversed in your favour.

Grades are earned by SKILL (selection), not profit — a bad setup that happened to
win is still a C. Everything is deterministic and derived, so it needs no storage
and surfaces post-session in the replay/coach.
"""

TREND_EPS = 0.001          # fast/slow SMA gap to call a trend
NEAR_LEVEL = 0.01          # within 1% of a level counts as "at" it


def _sma(closes, end, n):
    lo = max(0, end - n + 1)
    window = closes[lo:end + 1]
    return sum(window) / len(window) if window else None


def _trend_at(series, idx):
    """Local trend at bar idx: +1 up, -1 down, 0 flat (fast vs slow SMA)."""
    closes = [b.close for b in series]
    fast, slow = _sma(closes, idx, 10), _sma(closes, idx, 30)
    if fast is None or slow is None or slow == 0:
        return 0
    diff = (fast - slow) / slow
    return 1 if diff > TREND_EPS else (-1 if diff < -TREND_EPS else 0)


def _nearest_level(levels, price):
    return min(levels, key=lambda lv: abs(lv["price"] - price)) if levels else None


def grade_trade(trade, series, structure):
    """Grade one trade's setup. `trade` is a replay trade dict (direction,
    entry_price, bar_entered, stop_loss, planned_r); `series` the ordered bars;
    `structure` the annotate() output. Returns {grade, score, factors}."""
    idx = trade.get("bar_entered")
    direction = trade.get("direction")
    entry = trade.get("entry_price")
    factors = []
    score = 0

    def add(name, delta, met, note):
        nonlocal score
        score += delta
        factors.append({"name": name, "delta": delta, "met": met, "note": note})

    # 1) defined risk — the gate
    has_stop = trade.get("stop_loss") is not None
    if has_stop:
        add("Defined risk", 1, True, "stop-loss set")
    else:
        add("Defined risk", -3, False, "no stop — undefined risk")

    # 2) trend alignment
    tr = _trend_at(series, idx) if idx is not None else 0
    want = 1 if direction == "long" else -1
    if tr == want and tr != 0:
        add("Trend alignment", 2, True, "with the prevailing trend")
    elif tr == -want:
        add("Trend alignment", -2, False, "counter-trend entry")
    else:
        add("Trend alignment", 0, None, "no clear trend")

    # 3) reward:risk
    pr = trade.get("planned_r")
    if pr is None:
        add("Reward:risk", -1, False, "no target — R:R undefined")
    elif pr >= 2:
        add("Reward:risk", 2, True, f"{pr:.1f}R planned")
    elif pr >= 1:
        add("Reward:risk", 1, True, f"{pr:.1f}R planned")
    else:
        add("Reward:risk", -1, False, f"only {pr:.1f}R planned")

    # 4) location vs the nearest structural level
    lv = _nearest_level(structure.get("levels", []), entry) if entry else None
    if lv is not None and entry and abs(lv["price"] - entry) / entry < NEAR_LEVEL:
        above = lv["price"] >= entry
        if direction == "long":
            add("Location", -1, False, "entering into resistance") if above \
                else add("Location", 1, True, "entering off support")
        else:
            add("Location", 1, True, "entering off resistance") if above \
                else add("Location", -1, False, "entering into support")
    else:
        add("Location", 0, None, "no level nearby")

    # 5) trigger — a liquidity sweep that reversed in our favour, just before entry
    if idx is not None:
        for s in structure.get("sweeps", []):
            if 0 <= idx - s["bar_sequence"] <= 3 and (
                    (direction == "long" and s["side"] == "low") or
                    (direction == "short" and s["side"] == "high")):
                add("Trigger", 2, True, "entered just after a liquidity sweep reversed")
                break

    grade = "A" if (has_stop and score >= 4) else ("B" if score >= 1 else "C")
    return {"grade": grade, "score": score, "factors": factors}


def summarize(trades):
    """Grade distribution for a set of graded trades."""
    grades = [t["setup"]["grade"] for t in trades if t.get("setup")]
    return {"A": grades.count("A"), "B": grades.count("B"),
            "C": grades.count("C"), "total": len(grades)}
