"""
jsh_ranks' rank-up announcements and officer roles, checked without Discord.

The ladder below is JSH's own, from the live jsh_ranks.yaml rank_map, so the
cases are the promotions pilots will actually make. CreditSystem promotes by
ADDING the new rank's role and then removing the lower ones, so a promotion
is seen as one update where both are held -- the fixtures reproduce that.

    python3 -B -I tests/test_jsh_ranks_promotions.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / 'plugins' / 'jsh_ranks'))

import promotions as p  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(label, ok, detail=''):
    checks.append((label, ok, detail))


def eq(label, got, want):
    check(label, got == want, f"got {got!r}, want {want!r}")


EARNED = [
    'Airman Basic (E-1)', 'Airman (E-2)', 'Airman First Class (E-3)',
    'Senior Airman (E-4)', 'Staff Sergeant (E-5)', 'Technical Sergeant (E-6)',
    'Master Sergeant (E-7)', 'Senior Master Sergeant (E-8)',
    'Chief Master Sergeant (E-9)', 'Command Chief Master Sergeant (E-9S)',
    'Second Lieutenant (O-1)', 'First Lieutenant (O-2)', 'Captain (O-3)',
    'Major (O-4)', 'Lieutenant Colonel (O-5)', 'Colonel (O-6)',
    'Brigadier General (O-7)', 'Major General (O-8)',
]
GRANTED = ['Lieutenant General (O-9)', 'General (O-10)', 'General of the Air Force (O-11)']
LADDER = EARNED + GRANTED
E8, E9, E9S = EARNED[7], EARNED[8], EARNED[9]
O1, O3, O4, O5, O7 = EARNED[10], EARNED[12], EARNED[13], EARNED[14], EARNED[16]
OTHER = ['Pilot', 'Verified']          # roles everyone has that aren't ranks

# ------------------------------------------------------------- codes
eq("code from a rank name", p.code_of(O4), 'O-4')
eq("suffix codes survive", p.code_of(E9S), 'E-9S')
eq("a role with no code has none", p.code_of('Pilot'), None)

# --------------------------------------------------------- promotion()
# The user's own example: E-8 to E-9, as CreditSystem does it.
eq("E-8 -> E-9: the add, while E-8 is still held",
   p.promotion(EARNED, OTHER + [E8], OTHER + [E8, E9]), (E8, E9))
eq("...and the removal of E-8 that follows is NOT a second announcement",
   p.promotion(EARNED, OTHER + [E8, E9], OTHER + [E9]), None)
eq("E-9 -> E-9S counts, though rank_map gives both level 9",
   p.promotion(EARNED, [E9], [E9, E9S]), (E9, E9S))
eq("a jump of several rungs announces the highest",
   p.promotion(EARNED, [EARNED[6]], [EARNED[6], O1]), (EARNED[6], O1))
eq("a pilot's first rank is not announced",
   p.promotion(EARNED, OTHER, OTHER + [EARNED[0]]), None)
eq("re-earning after a campaign reset stripped every rank is not announced",
   p.promotion(EARNED, OTHER, OTHER + [O3]), None)
eq("a demotion is not announced",
   p.promotion(EARNED, [O4], [O4, O3]), None)
eq("a GRANTED rank is not announced -- earned rank-ups only",
   p.promotion(EARNED, [EARNED[-1]], [EARNED[-1], GRANTED[1]]), None)
eq("a change that adds no rank is not a promotion",
   p.promotion(EARNED, [O4], [O4, 'Pilot']), None)

# ----------------------------------------------------------- message()
fields = dict(mention='<@1>', name='Curse', old_rank=E8, new_rank=E9,
              old_code='E-8', new_code='E-9')
eq("the default message",
   p.message(None, **fields),
   f"Congratulations <@1>, you ranked up from {E8} to {E9}!")
eq("a custom template, with codes",
   p.message("{name}: {old_code} -> {new_code}", **fields), "Curse: E-8 -> E-9")
eq("a template with an unknown placeholder falls back rather than failing",
   p.message("Well done {pilot}!", **fields), p.message(None, **fields))
eq("a template with a stray brace falls back too",
   p.message("Well done {mention", **fields), p.message(None, **fields))

# ------------------------------------------------------- officer_roles()
STEPS = {
    'O-1': {'add': ['Officer', 'Junior Officer']},
    'O-4': {'add': ['Senior Officer'], 'remove': ['Junior Officer']},
    'O-7': {'add': ['General Officer'], 'remove': ['Senior Officer']},
}


def roles(held):
    add, remove, unknown = p.officer_roles(LADDER, STEPS, held)
    return add, remove


eq("reaching O-1: Officer and Junior Officer",
   roles([E9S, O1]), (['Junior Officer', 'Officer'], []))
eq("reaching O-4: Senior Officer in, Junior Officer out",
   roles([O4, 'Officer', 'Junior Officer']), (['Senior Officer'], ['Junior Officer']))
eq("reaching O-7: General Officer in, Senior Officer out",
   roles([O7, 'Officer', 'Senior Officer']), (['General Officer'], ['Senior Officer']))
eq("already right: nothing to do -- our own role change comes back as a no-op",
   roles([O4, 'Officer', 'Senior Officer']), ([], []))
eq("skipping O-4 (O-3 straight to O-5) still passes it",
   roles([O5, 'Officer', 'Junior Officer']), (['Senior Officer'], ['Junior Officer']))
eq("a demotion below O-4 gives Junior Officer back",
   roles([O3, 'Officer', 'Senior Officer']), (['Junior Officer'], ['Senior Officer']))
eq("a campaign reset that strips every rank strips the officer roles too",
   roles(['Officer', 'Junior Officer', 'Pilot']), ([], ['Junior Officer', 'Officer']))
eq("enlisted ranks get no officer roles", roles([E9]), ([], []))
eq("a GRANTED O-10 is above O-7: General Officer",
   roles([GRANTED[1]]), (['General Officer', 'Officer'], []))
eq("only managed roles are ever removed",
   roles([E9, 'Pilot', 'Verified']), ([], []))

_, _, unknown = p.officer_roles(LADDER, {'O-12': {'add': ['X']}, 'O-1': {'add': ['Officer']}}, [O1])
eq("a step key matching no rank is reported, not silently ignored", unknown, ['O-12'])
eq("steps may be keyed by full rank name as well as code",
   p.officer_roles(LADDER, {O4: {'add': ['Senior Officer']}}, [O5])[:2],
   (['Senior Officer'], []))
eq("step order in config does not matter -- ladder order does",
   p.officer_roles(LADDER, dict(reversed(list(STEPS.items()))), [O5, 'Officer', 'Junior Officer'])[:2],
   (['Senior Officer'], ['Junior Officer']))

# ------------------------------------------------- inactivity decay
O6, E4 = EARNED[15], EARNED[3]
held = [O6, 'Officer', 'Senior Officer']
eq("decay O-6 -> E-4, new rank added first: all officer roles go once O-6 does",
   roles([E4, 'Officer', 'Senior Officer']), ([], ['Officer', 'Senior Officer']))
eq("...and while both are still held, nothing changes yet",
   roles([O6, E4, 'Officer', 'Senior Officer']), ([], []))
eq("decay with O-6 removed first: officer roles go at once",
   roles(['Officer', 'Senior Officer']), ([], ['Officer', 'Senior Officer']))
eq("a decay demotion is never announced as a rank-up",
   p.promotion(EARNED, [O6], [O6, E4]), None)

# ------------------------------------------------------ names and IDs
IDS = {'Officer': '101', 'Junior Officer': '102', 'Senior Officer': '103',
       '101': '101', '102': '102', '103': '103', 101: '101', 103: '103'}
MIXED = {
    'O-1': {'add': [101, 'Junior Officer']},                 # Officer by ID
    'O-4': {'add': ['103'], 'remove': ['Junior Officer']},   # Senior by ID string
}
steps, bad = p.resolve_steps(MIXED, IDS.get)
eq("names and IDs all become the role's ID",
   steps, {'O-1': {'add': ['101', '102'], 'remove': []},
           'O-4': {'add': ['103'], 'remove': ['102']}})
eq("everything resolved", bad, [])
held_ids = [O4, 'Officer', '101', 'Junior Officer', '102']   # names AND ids, as Discord gives
eq("added by name at O-1, removed by name at O-4, matched by ID: Junior goes",
   p.officer_roles(LADDER, steps, held_ids)[:2], (['103'], ['102']))
steps, bad = p.resolve_steps({'O-1': {'add': ['Officer', 'Ghost Role']}}, IDS.get)
eq("a role that does not exist is reported", bad, ['Ghost Role'])
eq("...and kept as written, so it is never mistaken for a real role",
   steps['O-1']['add'], ['101', 'Ghost Role'])

failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    if not ok:
        print(' FAIL ', label, f'-- {detail}' if detail else '')
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed"
      f"{'' if not failed else '  (failures listed above)'}")
sys.exit(1 if failed else 0)
