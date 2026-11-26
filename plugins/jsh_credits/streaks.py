"""
The sortie-streak bonus: more for pilots who keep coming home.

Foothold has a cousin of this, its "same aircraft bonus"
(TurnaroundRewardPercent, 10% by default): land a qualifying sortie, take
off again in the same airframe, and the next landing pays 10% extra. It is
flat, it only ever covers the next sortie, and it is paid in Foothold's own
coalition funds -- none of it reaches DCSServerBot credits.

This one is ours, on server credits, and it GROWS: every consecutive sortie
that ends in a paid landing raises the bonus, and dying, ejecting or
crashing resets it. Imports nothing from the bot, so it can be tested alone.

A "sortie" here is a landing that PAID something. Landing with nothing
pending neither counts nor resets, so touch-and-goes cannot farm a streak.
"""
import re

from dataclasses import dataclass, field

#: How a sortie can end without a landing. Each always ends the streak.
LOSSES = ('crash', 'eject', 'pilot_death', 'self_kill')


@dataclass(frozen=True)
class StreakRules:
    enabled: bool = True
    base_percent: float = 10.0      # bonus on the first sortie that earns one
    step_percent: float = 5.0       # added for each consecutive sortie after that
    max_percent: float = 50.0       # never more than this
    min_sorties: int = 2            # the streak length that earns the base bonus
    same_aircraft: bool = True      # a slot change ends the streak, as in Foothold
    keep_on_disconnect: bool = False
    exclude_aircraft: tuple = field(default_factory=tuple)

    @classmethod
    def from_config(cls, cfg: dict) -> 'StreakRules':
        cfg = cfg or {}
        d = cls()
        return cls(enabled=bool(cfg.get('enabled', d.enabled)),
                   base_percent=max(0.0, float(cfg.get('base_percent', d.base_percent))),
                   step_percent=max(0.0, float(cfg.get('step_percent', d.step_percent))),
                   max_percent=max(0.0, float(cfg.get('max_percent', d.max_percent))),
                   min_sorties=max(1, int(cfg.get('min_sorties', d.min_sorties))),
                   same_aircraft=bool(cfg.get('same_aircraft', d.same_aircraft)),
                   keep_on_disconnect=bool(cfg.get('keep_on_disconnect', d.keep_on_disconnect)),
                   exclude_aircraft=tuple(str(a) for a in (cfg.get('exclude_aircraft') or [])))


def _norm(name: str) -> str:
    """'F/A-18C', 'fa-18c' and 'FA 18C' are the same: letters and digits only, lower case."""
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def excluded(rules: StreakRules, unit_type: str, display_name: str = '') -> bool:
    """
    Whether this aircraft is left out of the streak altogether.

    Names may be written as the Mission Editor shows them ('CH-47F',
    'Mi-8MTV2', 'F/A-18C') or as DCS's internal type ('CH-47Fbl1', 'Mi-8MT',
    'FA-18C_hornet'): each entry is matched against BOTH the unit's type and
    its display name, ignoring case, spaces and punctuation.
    """
    wanted = {_norm(a) for a in rules.exclude_aircraft if _norm(a)}
    return bool(wanted) and (_norm(unit_type) in wanted or _norm(display_name) in wanted)


def percent(streak: int, rules: StreakRules) -> float:
    """
    The bonus percentage for the landing that brings the streak to `streak`.

    With the defaults: sortie 1 earns nothing, sortie 2 earns 10%, sortie 3
    15%, sortie 4 20% ... up to 50%.
    """
    if not rules.enabled or streak < rules.min_sorties:
        return 0.0
    pct = rules.base_percent + rules.step_percent * (streak - rules.min_sorties)
    return min(rules.max_percent, pct)


def bonus(points: int, streak: int, rules: StreakRules) -> int:
    """Credits on top of `points`, the landing's own payout, rounded."""
    return int(round(points * percent(streak, rules) / 100.0)) if points > 0 else 0


def ends_streak(why: str, rules: StreakRules) -> bool:
    """Whether a sortie ending for `why` resets the streak to zero."""
    if why in LOSSES or why in ('mission end', 'mission load'):
        return True
    if why == 'slot change':
        return rules.same_aircraft
    if why == 'disconnect':
        return not rules.keep_on_disconnect
    return False
