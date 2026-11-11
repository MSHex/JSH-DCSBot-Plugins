"""
Carrier qualification, decided from a pilot's traps -- no bot, no database.

A qualification is CURRENT while enough qualifying traps sit inside the
validity window. So the expiry date is not "granted + 90 days" but "the day
the oldest trap that still counts drops out of the window":

    day   : the 4th-most-recent qualifying DAY trap   + valid_days
    night : the 2nd-most-recent qualifying NIGHT trap + valid_days,
            and never later than the day qualification -- night CQ
            requires a current day CQ, as it does for real.

Every new trap pushes the expiry forward, which is what makes the
qualification renew itself for a pilot who keeps flying, and lapse for one
who stops.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional


@dataclass(frozen=True)
class Rules:
    day_traps: int = 4
    night_traps: int = 2
    valid_days: int = 90


@dataclass(frozen=True)
class Standing:
    day_count: int
    night_count: int
    day_expires: Optional[datetime]      # None: not qualified
    night_expires: Optional[datetime]


def standing(traps: Iterable[tuple[bool, datetime]], rules: Rules,
             now: datetime) -> Standing:
    """
    `traps` are the pilot's QUALIFYING traps -- already filtered for grade and
    deck -- as (night, time). Traps older than the window are ignored here
    too, so the caller may pass more than it needs to.
    """
    window = timedelta(days=rules.valid_days)
    day, night = [], []
    for is_night, when in traps:
        if when is None or when < now - window or when > now + timedelta(minutes=5):
            continue
        (night if is_night else day).append(when)
    day.sort(reverse=True)
    night.sort(reverse=True)

    day_expires = None
    if rules.day_traps > 0 and len(day) >= rules.day_traps:
        day_expires = day[rules.day_traps - 1] + window

    night_expires = None
    if day_expires is not None and rules.night_traps > 0 and len(night) >= rules.night_traps:
        night_expires = min(day_expires, night[rules.night_traps - 1] + window)

    return Standing(len(day), len(night), day_expires, night_expires)


def is_new_grant(old_expires: Optional[datetime], exists: bool, now: datetime) -> bool:
    """
    True when this write makes the pilot qualified who was not a moment ago:
    no row at all, or a row that had already lapsed (logbook only deletes
    expired rows when the pilot next connects). Only these are announced and
    given the ribbon; a renewal is silent.
    """
    if not exists:
        return True
    return old_expires is not None and old_expires <= now


def should_write(old_expires: Optional[datetime], exists: bool,
                 new_expires: datetime) -> bool:
    """
    Never shorten a qualification, and never touch a PERMANENT one -- a row
    with no expiry is an admin's manual grant, and not ours to put a date on.
    """
    if not exists:
        return True
    if old_expires is None:
        return False
    return new_expires > old_expires


def progress_line(st: Standing, rules: Rules) -> str:
    """'Day 3/4 · Night 0/2' -- what /cq status shows a pilot still working on it."""
    return (f"Day {min(st.day_count, rules.day_traps)}/{rules.day_traps} · "
            f"Night {min(st.night_count, rules.night_traps)}/{rules.night_traps}")
