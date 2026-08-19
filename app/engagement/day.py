"""Local-day arithmetic for the engagement layer.

Every day boundary in this layer — activity rollup, daily goals, the paper XP
cap, and (Phase 4) streak periods — is computed in the LEARNER'S timezone, not
the server's. Render runs UTC, so `date.today()` would roll a learner in
Auckland over at 12pm their time and one in Los Angeles at 5pm the day before.

An unknown or malformed timezone falls back to the default rather than raising:
a bad profile value must never 500 a session that is otherwise fine.
"""
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.engagement import DEFAULT_TIMEZONE


def zone_for(tz_name):
    """ZoneInfo for a name, falling back to the default (then UTC) if unusable."""
    for candidate in (tz_name, DEFAULT_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return timezone.utc


def local_date(tz_name, when=None):
    """The learner's calendar date at `when` (default: now)."""
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:                 # stored datetimes are naive UTC
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone_for(tz_name)).date()


def local_now(tz_name):
    """Current wall-clock time in the learner's zone (for quiet hours, Phase 6)."""
    return datetime.now(timezone.utc).astimezone(zone_for(tz_name))


def is_weekend(day):
    return isinstance(day, date) and day.weekday() >= 5
