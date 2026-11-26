"""
jsh_credits' sortie-streak bonus, checked without the bot.

    python3 -B -I tests/test_jsh_credits_streaks.py
"""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    'streaks', pathlib.Path(__file__).resolve().parent.parent / 'plugins' / 'jsh_credits' / 'streaks.py')
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)

results: list[tuple[str, bool, str]] = []


def eq(label, got, want):
    results.append((label, got == want, f"got {got!r}, want {want!r}"))


R = st.StreakRules()
eq("sortie 1 earns nothing", st.percent(1, R), 0.0)
eq("sortie 2 earns the base 10%", st.percent(2, R), 10.0)
eq("sortie 3: 15%", st.percent(3, R), 15.0)
eq("sortie 6: 30%", st.percent(6, R), 30.0)
eq("capped at max_percent", st.percent(40, R), 50.0)
eq("the bonus is a percent of THIS landing's payout", st.bonus(200, 2, R), 20)
eq("...rounded", st.bonus(33, 3, R), 5)
eq("nothing paid, no bonus", st.bonus(0, 5, R), 0)

flat = st.StreakRules.from_config({'step_percent': 0})
eq("step 0 keeps it flat, like Foothold", [st.percent(n, flat) for n in (2, 3, 9)], [10.0] * 3)
first = st.StreakRules.from_config({'min_sorties': 1})
eq("min_sorties 1 pays from the first sortie", st.percent(1, first), 10.0)
eq("disabled pays nothing", st.percent(5, st.StreakRules.from_config({'enabled': False})), 0.0)
eq("junk-proof config: negatives clamp to 0",
   st.StreakRules.from_config({'base_percent': -5, 'min_sorties': 0}).min_sorties, 1)

for why in ('crash', 'eject', 'pilot_death', 'self_kill', 'mission end'):
    eq(f"{why} ends the streak", st.ends_streak(why, R), True)
eq("slot change ends it with same_aircraft", st.ends_streak('slot change', R), True)
eq("...and not without",
   st.ends_streak('slot change', st.StreakRules.from_config({'same_aircraft': False})), False)
eq("disconnect ends it by default", st.ends_streak('disconnect', R), True)
eq("...unless keep_on_disconnect",
   st.ends_streak('disconnect', st.StreakRules.from_config({'keep_on_disconnect': True})), False)
eq("an unknown reason leaves it", st.ends_streak('something else', R), False)

failed = [x for x in results if not x[1]]
for label, ok, detail in failed:
    print(' FAIL ', label, '--', detail)
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
