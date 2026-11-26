"""
jsh_logistics' payout rules, decided without Discord, a database or DCS.
"""
import fnmatch
from typing import Optional

#: Foothold's own stat names for its logistics jobs, as it counts them in
#: BattleCommander temp stats (zoneCommanderv2.lua and Foothold CTLD.lua).
#: A key may use * as a wildcard: "Built *" matches every CTLD build.
DEFAULT_DELIVERIES = {
    'Zone capture': 50,                     # supplies land in a neutral zone and take it
    'Zone upgrade': 30,                     # supplies upgrade a friendly zone
    'Zone supply delivery': 25,             # supplies restock a friendly zone
    'Zone capture (troops)': 30,            # troops dropped take a neutral zone
    'Zone supply delivery (troops)': 15,    # troops dropped into a friendly zone
    'Warehouse delivery': 25,               # weapons/fuel stock to an airbase warehouse
    'Built FARP': 40,                       # CTLD crates built into a FARP
    'Built *': 30,                          # any other CTLD build: HAWK, NASAMS, SA-8 ...
    'Upgraded *': 20,                       # CTLD upgrade of a built SAM system
}

#: Aircraft whose airborne time is paid by default -- the same list
#: awardautomation's Airman's Medal uses. DCS type names, case-insensitive.
DEFAULT_AIRCRAFT = ['C-130J-30', 'Hercules', 'CH-47Fbl1', 'UH-1H', 'Mi-8MT']


def reward_for(stat: str, table: dict) -> Optional[int]:
    """
    Credits for one of `stat`, or None if nothing in `table` matches it.
    An exact key wins over a wildcard; among wildcards, the longest pattern --
    the most specific -- wins, so "Built FARP" beats "Built *".
    """
    if stat in table:
        return int(table[stat])
    matches = [k for k in table if any(c in k for c in '*?[') and fnmatch.fnmatchcase(stat, k)]
    if not matches:
        return None
    return int(table[max(matches, key=len)])


def deliveries(stats: dict, table: dict) -> tuple[int, list[tuple[str, int, int]]]:
    """
    (total credits, [(stat, count, credits)]) for one banked landing.
    Unmatched stats -- kills, rescues, achievements -- are ignored, and so is
    anything priced at 0, which is how a delivery type is switched off.
    """
    total, lines = 0, []
    for stat in sorted(stats):
        try:
            count = int(stats[stat])
        except (TypeError, ValueError):
            continue
        each = reward_for(stat, table)
        if count <= 0 or not each or each <= 0:
            continue
        lines.append((stat, count, each * count))
        total += each * count
    return total, lines


def airtime(seconds: float, carried: float, minutes: float, credits: int) -> tuple[int, float]:
    """
    (credits to pay, seconds carried forward).

    `credits` are paid for every full `minutes` of airborne time; what is left
    over is carried to the pilot's next landing rather than rounded away, so
    ten 50-second hops at "1 credit per minute" still pay 8.
    """
    if minutes <= 0 or credits <= 0:
        return 0, 0.0
    total = max(0.0, float(seconds)) + max(0.0, float(carried))
    block = minutes * 60.0
    units = int(total // block)
    return units * credits, total - units * block


def phrase(lines: list[tuple[str, int, int]]) -> str:
    """'FARP built and 2 × zone supply delivery' -- for the in-game message."""
    words = []
    for stat, count, _ in lines:
        label = stat
        if stat.startswith('Built '):
            label = f"{stat[6:]} built"
        elif stat.startswith('Upgraded '):
            label = f"{stat[9:]} upgraded"
        label = label[0].lower() + label[1:] if label[:1].isupper() and not label[:2].isupper() else label
        words.append(f"{count} × {label}" if count != 1 else label)
    if not words:
        return ''
    return words[0] if len(words) == 1 else ', '.join(words[:-1]) + ' and ' + words[-1]
