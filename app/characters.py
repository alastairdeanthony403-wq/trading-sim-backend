"""Psychology character voices (Phase E step 4).

A small cast whose conflicting one-liners surface at decision points — a hype
voice pulling you in, a sober voice pulling you back. They are flavour that
dramatises the emotional pull of a moment; the actual consequences come from
the player's own decisions and are scored server-side. When those decisions go
wrong, the coach's psychology read (see coach.build_findings) names the impulse.

Nothing here is advice — the voices deliberately include BAD takes so the player
learns to recognise and resist them.
"""

# id -> display metadata
CAST = {
    "risk_manager": {"name": "Vera", "role": "Risk Manager", "stance": "cautious"},
    "aggressive":   {"name": "Rex", "role": "Momentum Trader", "stance": "aggressive"},
    "guru":         {"name": "MoonBoy", "role": "Hype Guru", "stance": "hype"},
    "analyst":      {"name": "Dr. Sol", "role": "Analyst", "stance": "sober"},
}


def _voice(char_id, line):
    c = CAST[char_id]
    return {"character": char_id, "name": c["name"], "role": c["role"],
            "stance": c["stance"], "line": line}


# Conflicting takes for in-session decision points (from /advance).
CONTEXT_LINES = {
    "after_stopout": [
        ("aggressive", "Make it back right now — size up and get it straight back."),
        ("risk_manager", "You just got stopped. Step away before you revenge trade."),
    ],
    "no_stop": [
        ("risk_manager", "You're holding with no stop. Define what you'll lose first."),
        ("aggressive", "Stops are for people who doubt the trade. Hold."),
    ],
}


def voices_for_context(context):
    pair = CONTEXT_LINES.get(context)
    if not pair:
        return []
    return [_voice(cid, line) for cid, line in pair]


def voices_for_event(category, sentiment=0):
    """Conflicting takes attached to a breaking headline (the decision point)."""
    if category == "hype":
        return [
            _voice("guru", "This is THE one. Don't miss generational wealth. 🚀"),
            _voice("analyst", "Thin volume, pure hype. This is what a trap looks like."),
        ]
    if category == "rug":
        return [
            _voice("analyst", "There's the rug. The loudest voices went quiet at the top."),
            _voice("risk_manager", "Nothing to catch here — preserve your capital."),
        ]
    # ordinary news: split by sentiment
    if sentiment is not None and sentiment < 0:
        return [
            _voice("guru", "Buy the dip! It always bounces."),
            _voice("analyst", "Bad news, spreads widening. No edge catching a falling knife."),
        ]
    return [
        _voice("guru", "It's ripping — get in before it's gone!"),
        _voice("analyst", "The move already happened. Chasing is how you buy the top."),
    ]


# Impulse -> (label, the character whose voice 'won'). Used by the coach to tie
# a detected pattern back to the in-session voices.
IMPULSE_VOICE = {
    "revenge": ("revenge trading", "Rex"),
    "fomo": ("FOMO / oversizing", "MoonBoy"),
    "fear": ("cutting winners out of fear", "Rex"),
}
