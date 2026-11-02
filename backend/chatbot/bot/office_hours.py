"""
Office hours of the agency's human advisors. Computed in code: the model used
to be asked to work out from the clock whether the office was open, which it
got wrong often enough to promise callbacks on a Sunday.
"""

from datetime import datetime, time, timedelta

# weekday() -> opening blocks. Monday is 0, Sunday is 6.
SCHEDULE = {
    0: [(time(10), time(14)), (time(16), time(18))],
    1: [(time(10), time(14)), (time(16), time(18))],
    2: [(time(10), time(14)), (time(16), time(18))],
    3: [(time(10), time(14)), (time(16), time(18))],
    4: [(time(10), time(14)), (time(16), time(18))],
    5: [(time(10), time(13))],
    6: [],
}

DAY_NAMES = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']

# The bot does not send follow-up messages at night.
QUIET_BEFORE = time(8)
QUIET_AFTER = time(21)


def is_open(now: datetime) -> bool:
    return any(start <= now.time() < end for start, end in SCHEDULE[now.weekday()])


def next_opening(now: datetime) -> datetime:
    """The start of the next block when advisors are at their desks."""
    for days_ahead in range(8):
        day = (now + timedelta(days=days_ahead)).date()
        for start, _end in SCHEDULE[day.weekday()]:
            candidate = datetime.combine(day, start, tzinfo=now.tzinfo)
            if candidate > now:
                return candidate
    raise RuntimeError('The schedule has no opening hours')


def describe(now: datetime) -> str:
    """A sentence for the system prompt stating, as fact, whether advisors are available."""
    if is_open(now):
        return 'Human advisors are at the office right now and will contact the traveller shortly.'
    opening = next_opening(now)
    when = DAY_NAMES[opening.weekday()] + ' a las ' + opening.strftime('%H:%M')
    return (
        'The office is CLOSED right now. Tell the traveller their data was received securely and that an advisor '
        f'will contact them when the office opens: {when}.'
    )


def is_quiet_time(now: datetime) -> bool:
    return not (QUIET_BEFORE <= now.time() < QUIET_AFTER)
