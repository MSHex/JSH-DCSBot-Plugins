from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping


class CycleState(StrEnum):
    COLLECTING = "collecting"
    AWARDED_LOCKED = "awarded_locked"


class CycleAction(StrEnum):
    NONE = "none"
    AWARD = "award"
    BASELINE_LOCK = "baseline_lock"
    RESET = "reset"


@dataclass(frozen=True)
class QualificationSnapshot:
    aar_valid: bool
    ifr_valid: bool
    aar_names: tuple[str, ...] = ()
    ifr_names: tuple[str, ...] = ()

    @property
    def both_valid(self) -> bool:
        return self.aar_valid and self.ifr_valid

    @property
    def both_invalid(self) -> bool:
        return not self.aar_valid and not self.ifr_valid


def hardcore_session_streak(rows: Iterable[Mapping[str, Any]]) -> int:
    """Count the current successful Hardcore streak from newest to oldest.

    Zero-block sessions are ignored. A revoked Hardcore session or a billable
    Normal session ends the streak.
    """
    streak = 0
    for row in rows:
        hardcore_at_start = bool(row.get('hardcore_at_start'))
        hardcore_revoked = bool(row.get('hardcore_revoked'))
        gross_half_units = int(row.get('gross_half_units') or 0)

        if hardcore_at_start and hardcore_revoked:
            break
        if gross_half_units <= 0:
            continue
        if not hardcore_at_start:
            break
        streak += 1
    return streak


def hardcore_milestone_action(
    state: CycleState,
    *,
    streak: int,
    required_sessions: int,
    newly_registered: bool = False,
    bootstrap_existing: bool = False,
    previously_awarded: bool = False,
) -> CycleAction:
    """Return the transition for a one-time Hardcore streak milestone.

    A first-scan baseline remains locked until that inherited streak breaks.
    A genuinely awarded milestone remains permanently locked.
    """
    if state == CycleState.AWARDED_LOCKED:
        if previously_awarded:
            return CycleAction.NONE
        return CycleAction.RESET if streak < required_sessions else CycleAction.NONE

    if streak < required_sessions:
        return CycleAction.NONE
    if newly_registered and not bootstrap_existing:
        return CycleAction.BASELINE_LOCK
    return CycleAction.AWARD


def one_time_milestone_action(
    state: CycleState,
    *,
    qualified: bool,
    newly_registered: bool = False,
    bootstrap_existing: bool = False,
) -> CycleAction:
    """Return the transition for a permanent, one-time statistical award."""
    if state == CycleState.AWARDED_LOCKED or not qualified:
        return CycleAction.NONE
    if newly_registered and not bootstrap_existing:
        return CycleAction.BASELINE_LOCK
    return CycleAction.AWARD


def completed_milestones(total_units: int, units_per_award: int) -> int:
    """Return how many complete recurring milestones have been reached."""
    if units_per_award <= 0:
        raise ValueError("units_per_award must be positive")
    return max(0, int(total_units)) // units_per_award


def scoped_rule_state_key(
    rule_key: str,
    campaign_id: int,
    server_name: str | None = None,
) -> str:
    """Build a stable progress key for campaign- or server-scoped rules."""
    key = f"{rule_key}:campaign:{int(campaign_id)}"
    if server_name:
        key += f":server:{server_name}"
    return key


def kill_death_ratio(kills: int, deaths: int) -> float:
    """Match DCSServerBot's zero-death K/D convention."""
    return kills / deaths if deaths > 0 else float(kills)


def air_force_cross_qualified(
    *,
    flight_seconds: int,
    kills: int,
    deaths: int,
    minimum_flight_hours: int,
    kd_ratio_over: float,
) -> bool:
    """Require both the minimum hours and a strictly greater K/D ratio."""
    return (
        flight_seconds >= minimum_flight_hours * 3600
        and kill_death_ratio(kills, deaths) > kd_ratio_over
    )


def recurring_milestone_plan(
    completed: int,
    next_cycle: int,
    *,
    newly_registered: bool = False,
    bootstrap_existing: bool = False,
) -> tuple[int | None, tuple[int, ...]]:
    """Plan baseline advancement or every newly completed recurring cycle.

    The first tuple item is the next cycle to store when existing progress is
    baselined. The second item lists cycles that must be awarded in order.
    """
    completed = max(0, int(completed))
    next_cycle = max(1, int(next_cycle))
    if completed < next_cycle:
        return None, ()
    if newly_registered and not bootstrap_existing:
        return completed + 1, ()
    return None, tuple(range(next_cycle, completed + 1))


def combat_readiness_action(
    state: CycleState,
    snapshot: QualificationSnapshot,
    *,
    newly_registered: bool = False,
    bootstrap_existing: bool = False,
) -> CycleAction:
    """Return the only state transition allowed for Combat Readiness.

    A completed cycle stays locked until AAR and IFR are both invalid at the
    same observation. Partial expiry and partial renewal never unlock it.
    """
    if state == CycleState.COLLECTING:
        if not snapshot.both_valid:
            return CycleAction.NONE
        if newly_registered and not bootstrap_existing:
            return CycleAction.BASELINE_LOCK
        return CycleAction.AWARD

    if state == CycleState.AWARDED_LOCKED and snapshot.both_invalid:
        return CycleAction.RESET

    return CycleAction.NONE
