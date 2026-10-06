import math
from collections.abc import Iterable, Mapping
from typing import Any


def meets_achievement(achievement: dict[str, Any], credits: int, playtime: float) -> bool:
    """Return whether credits and playtime satisfy an achievement definition."""
    credit_requirement_met = 'credits' in achievement and credits >= achievement['credits']
    playtime_requirement_met = 'playtime' in achievement and playtime >= achievement['playtime']

    if achievement.get('combined'):
        return credit_requirement_met and playtime_requirement_met
    return credit_requirement_met or playtime_requirement_met


def get_rank_progress(achievements: list[dict[str, Any]], credits: int,
                      playtime: float) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the current and next achievement using CreditSystem's role-assignment order."""
    ordered = sorted(
        achievements,
        key=lambda achievement: achievement['credits']
        if 'credits' in achievement else achievement['playtime']
    )

    current_index = None
    for index, achievement in enumerate(ordered):
        if meets_achievement(achievement, credits, playtime):
            current_index = index

    if current_index is None:
        return None, ordered[0] if ordered else None
    if current_index == len(ordered) - 1:
        return ordered[current_index], None
    return ordered[current_index], ordered[current_index + 1]


def format_playtime(hours: float, *, round_up: bool = False) -> str:
    """Format decimal hours without rounding a player into a rank early."""
    total_minutes = max(0, math.ceil(hours * 60) if round_up else math.floor(hours * 60))
    full_hours, minutes = divmod(total_minutes, 60)
    if full_hours and minutes:
        return f"{full_hours}h {minutes}m"
    if full_hours:
        return f"{full_hours}h"
    return f"{minutes}m"


def calculate_kd_ratio(kills: int, deaths: int) -> float:
    """Match DCSServerBot's existing zero-death K/D convention."""
    return kills / deaths if deaths > 0 else float(kills)


def format_decimal(value: float, precision: int = 2) -> str:
    """Format a decimal value without unnecessary trailing zeroes."""
    return f"{value:.{precision}f}".rstrip('0').rstrip('.')


def calculate_matching_credit_loss(rows: Iterable[Mapping[str, Any]], reasons: Iterable[str]) -> int:
    """Return actual credit deductions whose audit remark contains an FK reason."""
    normalized_reasons = {reason.strip().casefold() for reason in reasons if reason and reason.strip()}
    if not normalized_reasons:
        return 0

    total = 0
    for row in rows:
        remark = str(row.get('remark') or '').casefold()
        if any(reason in remark for reason in normalized_reasons):
            total += max(0, int(row.get('old_points') or 0) - int(row.get('new_points') or 0))
    return total
