"""
Rank-up announcements and officer roles, decided without Discord.

Like grants.py, this imports nothing from DCSServerBot, discord or psycopg.
commands.py feeds it role NAMES and acts on what it returns, so every rule
here can be tested against plain lists.

Where a rank-up is seen
-----------------------
CreditSystem promotes a pilot in process_achievements(), run when they
disconnect: it adds the new rank's role, then removes every lower one. It
raises no event and posts nothing -- only an audit line. Discord, though,
reports each role change as a member update, and the ADD arrives first: for
that one update the pilot holds both the old rank and the new. That is what
promotion() reads.
"""
import re

from typing import Iterable, Optional

#: The pay-grade code at the end of a rank's name: "Major (O-4)" -> "O-4",
#: "Command Chief Master Sergeant (E-9S)" -> "E-9S".
_CODE = re.compile(r'\(([A-Z]+-\d+[A-Z]*)\)\s*$')

DEFAULT_MESSAGE = 'Congratulations {mention}, you ranked up from {old_rank} to {new_rank}!'


def code_of(rank: str) -> Optional[str]:
    """The pay-grade code in a rank's name, or None if it carries none."""
    m = _CODE.search(str(rank or ''))
    return m.group(1) if m else None


def _highest(ladder: list, names: Iterable[str]) -> Optional[str]:
    """The highest rung of `ladder` among `names`, or None."""
    held = set(names)
    best = None
    for rank in ladder:
        if rank in held:
            best = rank
    return best


def promotion(earned: list, before: Iterable[str],
              after: Iterable[str]) -> Optional[tuple]:
    """
    (old rank, new rank) if this role change is an earned promotion, else None.

    `earned` is the earned ladder, lowest rung first -- creditsystem's own
    achievements -- so a granted rank is never announced. Only an ADDED rung
    counts, and only above the highest one held just before.

    A pilot who held no rung before is NOT announced. That is a brand-new
    pilot's first rank, or a pilot re-earning ranks after a campaign reset
    stripped them all -- announcing those would flood the channel after
    every reset.
    """
    # "The highest rung held went up" -- which can only happen by a higher
    # rung being added, so no separate check for what was added is needed.
    old = _highest(earned, before)
    new = _highest(earned, after)
    if old is None or new is None or earned.index(new) <= earned.index(old):
        return None
    return old, new


def message(template: Optional[str], **fields) -> str:
    """
    The announcement, from a template in config.

    Placeholders: {mention} {name} {old_rank} {new_rank} {old_code} {new_code}.
    A template with a typo in it falls back to the default rather than costing
    the announcement.
    """
    try:
        return (template or DEFAULT_MESSAGE).format(**fields)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_MESSAGE.format(**fields)


def _position(ladder: list, key: str) -> Optional[int]:
    """Where a step's key sits on the ladder: by full rank name or by code."""
    for i, rank in enumerate(ladder):
        if rank == key or code_of(rank) == key:
            return i
    return None


def officer_roles(ladder: list, steps: dict, held: Iterable[str]) -> tuple:
    """
    (roles to add, roles to remove, step keys not on the ladder).

    `ladder` is every rank in order, earned then granted, so a granted O-10
    counts as above O-7. `steps` is keyed by code or rank name:

        O-1: {add: [Officer, Junior Officer]}
        O-4: {add: [Senior Officer], remove: [Junior Officer]}
        O-7: {add: [General Officer], remove: [Senior Officer]}

    This works out the roles a pilot SHOULD hold for their highest rank --
    every step at or below it, applied in ladder order -- rather than
    reacting to the step just crossed. That is what keeps it right when a
    pilot skips a rung (O-3 straight to O-5 still passes O-4), is demoted, or
    loses every rank in a campaign reset.
    """
    held = set(held)
    placed, unknown = [], []
    for key, step in (steps or {}).items():
        pos = _position(ladder, str(key))
        if pos is None:
            unknown.append(str(key))
        else:
            placed.append((pos, step or {}))
    placed.sort(key=lambda p: p[0])

    top = _highest(ladder, held)
    current = ladder.index(top) if top is not None else -1

    managed, want = set(), set()
    for pos, step in placed:
        adds = set(step.get('add') or [])
        removes = set(step.get('remove') or [])
        managed |= adds | removes
        if pos <= current:
            want = (want | adds) - removes

    return sorted(want - held), sorted((managed - want) & held), unknown


def resolve_steps(steps: dict, resolve) -> tuple:
    """
    (steps with every role as a canonical id, role tokens that did not resolve).

    Roles in officer_roles may be names or Discord IDs, mixed freely. They are
    all turned into ONE form first -- the role's ID, as a string -- because
    the set arithmetic in officer_roles() compares them: a role added by ID at
    O-1 and removed by name at O-4 must be recognised as the same role, or the
    removal never happens.

    `resolve(token)` returns the canonical id string, or None if no such role
    exists. A token that does not resolve is kept as written, so it can still
    be reported, and is never acted on.
    """
    out, unresolved = {}, []
    for key, step in (steps or {}).items():
        step = step or {}
        fixed = {}
        for action in ('add', 'remove'):
            roles = []
            for token in step.get(action) or []:
                canon = resolve(token)
                if canon is None:
                    unresolved.append(str(token))
                    roles.append(str(token))
                else:
                    roles.append(canon)
            fixed[action] = roles
        out[key] = fixed
    return out, sorted(set(unresolved))
