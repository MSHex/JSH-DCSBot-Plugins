"""
The monthly awards board, decided and formatted without Discord or a database.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

#: Every category the board knows, in the order it is posted.
CATEGORIES = ('credits', 'csar', 'hours', 'kd_aa', 'kd_ag')

TITLES = {
    'credits': 'Most credits earned',
    'csar': 'Most CSAR rescues',
    'hours': 'Most flight hours',
    'kd_aa': 'Best K/D, air-to-air',
    'kd_ag': 'Best K/D, air-to-ground',
}

MEDALS = ['🥇', '🥈', '🥉']


def month_bounds(month: date) -> tuple[datetime, datetime]:
    """[first instant, first instant of the next month), naive UTC like the bot's columns."""
    start = datetime(month.year, month.month, 1)
    end = datetime(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return start, end


def clip(month_start: datetime, month_end: datetime, campaign_start: datetime,
         campaign_stop: Optional[datetime]) -> Optional[tuple[datetime, datetime]]:
    """
    The part of the month the campaign was running, or None if it never was.
    A campaign that began on the 12th counts from the 12th; one that ended on
    the 20th, up to the 20th.
    """
    start = max(month_start, campaign_start)
    end = month_end if campaign_stop is None else min(month_end, campaign_stop)
    return (start, end) if start < end else None


def previous_month(today: date) -> date:
    return date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)


def parse_month(text: Optional[str], today: date) -> Optional[date]:
    """'2026-10' -> 1 Oct 2026. None/'' -> last month. Anything else -> None."""
    if not text:
        return previous_month(today)
    try:
        y, m = (int(x) for x in text.strip().split('-'))
        return date(y, m, 1) if 1 <= m <= 12 and 2000 <= y <= 2100 else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Entry:
    ucid: str
    name: str
    discord_id: int
    value: float              # credits, rescues, hours, or K/D
    kills: int = 0
    deaths: int = 0


def kd(kills: int, deaths: int) -> float:
    """Kills over deaths; with no deaths, the kills themselves -- the bot's convention."""
    return kills / deaths if deaths > 0 else float(kills)


def ranked_kd(rows: list[dict], min_kills: int, top: int) -> list[Entry]:
    """K/D rows ({ucid,name,discord_id,kills,deaths}) ranked, below min_kills dropped."""
    out = [Entry(r['ucid'], r['name'], int(r['discord_id'] or -1), kd(r['kills'], r['deaths']),
                 int(r['kills']), int(r['deaths']))
           for r in rows if int(r['kills'] or 0) >= max(1, min_kills)]
    out.sort(key=lambda e: (-e.value, -e.kills, e.name.lower()))
    return out[:top]


def who(e: Entry) -> str:
    # A mention renders as the member's name; the board is sent with user
    # mentions suppressed, so nobody is pinged by it.
    return f"<@{e.discord_id}>" if e.discord_id > 0 else e.name


def value_text(category: str, e: Entry) -> str:
    if category == 'credits':
        return f"{int(e.value):,} credits"
    if category == 'csar':
        return f"{int(e.value)} rescue{'s' if int(e.value) != 1 else ''}"
    if category == 'hours':
        return f"{e.value:,.1f} h"
    return f"{e.value:.2f} ({e.kills} kills / {e.deaths} deaths)"


def field(category: str, entries: list[Entry]) -> str:
    if not entries:
        return "*No qualifying pilots this month.*"
    lines = []
    for i, e in enumerate(entries):
        mark = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        lines.append(f"{mark} {who(e)} — {value_text(category, e)}")
    return '\n'.join(lines)[:1024]


def citation(category: str, month: date, e: Entry) -> str:
    return f"{TITLES[category]}, {month:%B %Y}: {value_text(category, e)}."
