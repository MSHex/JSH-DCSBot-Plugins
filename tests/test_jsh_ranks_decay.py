"""
jsh_ranks' decay demotions, checked without Discord or a database.

    python3 -B -I tests/test_jsh_ranks_decay.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / 'plugins' / 'jsh_ranks'))

import decay  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def eq(label, got, want):
    checks.append((label, got == want, f"got {got!r}, want {want!r}"))


EARNED = ['E-1', 'E-2', 'E-3', 'E-4', 'E-5', 'O-1', 'O-4', 'O-6', 'O-8']
OTHER = ['Pilot', 'Officer', 'General (O-10)']

eq("the user's case: decayed from O-6 to E-4 credits -- E-4 in, O-6 out",
   decay.demotion(EARNED, OTHER + ['O-6'], 'E-4'), (['E-4'], ['O-6']))
eq("a stray lower rung held alongside is cleared too, as CreditSystem would",
   decay.demotion(EARNED, ['O-6', 'E-2'], 'E-4'), (['E-4'], ['E-2', 'O-6']))
eq("the deserved rung already held is kept, not re-added",
   decay.demotion(EARNED, ['O-6', 'E-4'], 'E-4'), ([], ['O-6']))
eq("deserving exactly what is held: nothing", decay.demotion(EARNED, ['O-4'], 'O-4'), None)
eq("deserving MORE than held: nothing -- promotions are CreditSystem's",
   decay.demotion(EARNED, ['O-1'], 'O-6'), None)
eq("below the first rung: every earned rung goes",
   decay.demotion(EARNED, ['O-1', 'Pilot'], None), ([], ['O-1']))
eq("no earned rung held: nothing to lower", decay.demotion(EARNED, OTHER, 'E-1'), None)
eq("non-rank roles, granted ranks included, are never touched",
   decay.demotion(EARNED, OTHER + ['O-8'], 'O-1'), (['O-1'], ['O-8']))

eq("several UCIDs: the best of them counts", decay.best_rank(EARNED, ['E-2', None, 'O-4']), 'O-4')
eq("an alt with nothing does not drag the main down", decay.best_rank(EARNED, ['O-6', None]), 'O-6')
eq("all below the first rung: None", decay.best_rank(EARNED, [None, None]), None)

failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    if not ok:
        print(' FAIL ', label, f'-- {detail}')
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
