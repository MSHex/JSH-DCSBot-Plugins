"""
Which slots DCS gives no controllable unit for.

Deliberately a copy of battlegroundbridge/domain.py's version rather than an
import of it. Both plugins need this, either can be installed without the
other, and importing across plugin packages runs the other's __init__ -- which
is how rankstatus nearly ended up unable to load without jsh_ranks. Fifteen
duplicated lines are cheaper than that coupling; if they ever disagree, this
module and that one are the two places to look.
"""

#: A mark placed from one of these carries NO initiator, which is why
#: Foothold's own `buy:` refuses every gated item from a Tactical Commander.
#:
#: Matched as PREFIXES: DCS numbers them per coalition --
#: artillery_commander_blue_1, forward_observer_red_2 -- so an exact-name list
#: matches nothing on a real server.
COMMANDER_SLOTS = frozenset({
    'instructor',
    'artillery_commander',
    'forward_observer',
    'game-master',
    'gamemaster',
    'observer',
    'spectator',
})


def is_commander_slot(unit_type, slot=None, slots=COMMANDER_SLOTS) -> bool:
    """
    True when the player occupies a slot with no controllable unit.

    Both the unit type and the slot name are checked, because DCSServerBot
    reports these differently depending on how the slot was entered and only
    one of them needs to name it.
    """
    for value in (unit_type, slot):
        name = str(value or '').strip().lower()
        if not name:
            continue
        for commander in slots:
            if name == commander or name.startswith(commander + '_'):
                return True
    return False


def commander_levels(details, slots=COMMANDER_SLOTS) -> dict[str, int]:
    """
    The level to authorise a `cbuy:` against, per coalition.

    `details` is what Ranks.details_for_server() returns, with `side` added.
    Keyed by the coalition as a STRING because that is how the level table
    crosses into Lua, and a Lua table literal built from a dict writes every
    key as a string.

    The HIGHEST level among the commanders on a side wins. Two commanders is
    not a reason to refuse here, unlike attributing a report to one of them:
    nobody's name is going on anything, and the alternative -- refusing while
    two admins are on the map -- is the failure this exists to remove.
    """
    levels: dict[str, int] = {}
    for entry in details:
        if not is_commander_slot(entry.get('unit_type'), entry.get('slot'), slots):
            continue
        side = entry.get('side')
        side = getattr(side, 'value', side)
        if side not in (1, 2):
            continue
        key = str(side)
        level = int(entry.get('level') or 0)
        if level > levels.get(key, 0):
            levels[key] = level
    return levels
