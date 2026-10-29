"""
Commander-slot detection and the per-coalition level a `cbuy:` is authorised
against. Pure module, no DCSServerBot or discord needed.

    python3 -I tests/test_jsh_ranks_slots.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugins' / 'jsh_ranks'))
import slots as S  # noqa: E402


class Side:
    """Stands in for DCSServerBot's Side enum (RED = 1, BLUE = 2)."""
    def __init__(self, value):
        self.value = value


checks, fails = [], []


def eq(label, got, want):
    ok = got == want
    checks.append((label, got, want, ok))
    if not ok:
        fails.append(label)


# --- slot detection -------------------------------------------------------
# These are the names a real server reports; the coalition numbering is the
# reason they are matched as prefixes rather than exact names.
for unit_type, slot, want, label in [
    ('artillery_commander_blue_1', None, True, 'numbered CA slot, as our dcs.log prints it'),
    (None, 'artillery_commander_blue_2', True, 'same, seen as the slot name'),
    ('instructor', None, True, 'instructor'),
    ('forward_observer_red_1', None, True, 'numbered forward observer'),
    ('gamemaster', None, True, 'game master'),
    ('game-master', None, True, 'hyphenated game master'),
    ('INSTRUCTOR', None, True, 'case-insensitive'),
    ('FA-18C_hornet', None, False, 'an aircraft is never a commander slot'),
    ('AH-64D_BLK_II (Crew)', None, False, 'nor a crew seat'),
    ('C-130J-30', 'C-130J-30', False, 'nor a transport'),
    (None, None, False, 'nothing at all'),
    ('', '', False, 'empty strings'),
]:
    eq(f'is_commander_slot: {label}', S.is_commander_slot(unit_type, slot), want)

eq('every default slot matches bare and numbered',
   all(S.is_commander_slot(b) and S.is_commander_slot(f'{b}_blue_1')
       for b in S.COMMANDER_SLOTS), True)

# --- the level pushed to the mission --------------------------------------
eq('no commanders means nothing is pushed',
   S.commander_levels([
       {'unit_type': 'FA-18C_hornet', 'slot': 'FA-18C_hornet', 'side': Side(2), 'level': 14},
   ]), {})

eq('one commander sets their coalition',
   S.commander_levels([
       {'unit_type': 'instructor', 'slot': 'instructor', 'side': Side(2), 'level': 20},
   ]), {'2': 20})

eq('the HIGHEST commander wins, not the first',
   S.commander_levels([
       {'unit_type': 'instructor', 'slot': 'instructor', 'side': Side(2), 'level': 18},
       {'unit_type': 'artillery_commander_blue_1', 'slot': None, 'side': Side(2), 'level': 20},
       {'unit_type': 'instructor', 'slot': None, 'side': Side(2), 'level': 12},
   ]), {'2': 20})

eq('the two coalitions are kept apart',
   S.commander_levels([
       {'unit_type': 'instructor', 'slot': None, 'side': Side(2), 'level': 20},
       {'unit_type': 'forward_observer_red_1', 'slot': None, 'side': Side(1), 'level': 9},
   ]), {'2': 20, '1': 9})

eq('pilots in cockpits never contribute',
   S.commander_levels([
       {'unit_type': 'FA-18C_hornet', 'slot': 'FA-18C_hornet', 'side': Side(2), 'level': 20},
       {'unit_type': 'instructor', 'slot': None, 'side': Side(2), 'level': 3},
   ]), {'2': 3})

eq('neutral and spectator-side entries are dropped',
   S.commander_levels([
       {'unit_type': 'instructor', 'slot': None, 'side': Side(0), 'level': 20},
       {'unit_type': 'instructor', 'slot': None, 'side': Side(-1), 'level': 20},
   ]), {})

eq('a bare int side works as well as the enum',
   S.commander_levels([
       {'unit_type': 'instructor', 'slot': None, 'side': 2, 'level': 15},
   ]), {'2': 15})

eq('keys are strings, because that is how they cross into Lua',
   list(S.commander_levels([
       {'unit_type': 'instructor', 'slot': None, 'side': Side(2), 'level': 1},
   ])), ['2'])

for label, got, want, ok in checks:
    print(('  ok  ' if ok else ' FAIL '), label, '' if ok else f'-> {got!r} (wanted {want!r})')
print(f"\n{len(checks) - len(fails)}/{len(checks)} passed")
sys.exit(1 if fails else 0)
