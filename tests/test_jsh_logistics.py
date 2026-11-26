"""
jsh_logistics' payout rules, checked without DCS, Discord or a database.

The stat names are Foothold's own, as zoneCommanderv2.lua and Foothold CTLD.lua
pass them to BattleCommander:addTempStat().

    python3 -B -I tests/test_jsh_logistics.py
"""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    'rewards', pathlib.Path(__file__).resolve().parent.parent / 'plugins' / 'jsh_logistics' / 'rewards.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

results: list[tuple[str, bool, str]] = []


def eq(label, got, want):
    results.append((label, got == want, f"got {got!r}, want {want!r}"))


T = r.DEFAULT_DELIVERIES

# ------------------------------------------------------------- reward_for
eq("an exact stat", r.reward_for('Zone capture', T), 50)
eq("troops are their own stat, not 'Zone capture'", r.reward_for('Zone capture (troops)', T), 30)
eq("a CTLD build by wildcard", r.reward_for('Built NASAMS', T), 30)
eq("an exact build beats the wildcard", r.reward_for('Built FARP', T), 40)
eq("upgrades by their own wildcard", r.reward_for('Upgraded HAWK', T), 20)
eq("a non-logistics stat is not paid", r.reward_for('Pilot Rescue', T), None)
eq("...nor a kill stat", r.reward_for('Demolition kill', T), None)
eq("the longest wildcard wins",
   r.reward_for('Built SA-8', {'Built *': 30, 'Built SA-*': 50, '*': 1}), 50)
eq("brackets in a stat name are literal for an exact key",
   r.reward_for('Zone supply delivery (troops)', T), 15)

# ------------------------------------------------------------- deliveries
stats = {'Zone supply delivery': 2, 'Built FARP': 1, 'Pilot Rescue': 3, 'Flight time': 40}
total, lines = r.deliveries(stats, T)
eq("one landing: logistics stats paid, the rest ignored", total, 2 * 25 + 40)
eq("...line by line", lines, [('Built FARP', 1, 40), ('Zone supply delivery', 2, 50)])
eq("a price of 0 switches a delivery off",
   r.deliveries({'Warehouse delivery': 3}, dict(T, **{'Warehouse delivery': 0})), (0, []))
eq("junk counts are skipped, not fatal",
   r.deliveries({'Zone capture': 'x', 'Zone upgrade': -1}, T), (0, []))

# ---------------------------------------------------------------- airtime
eq("1 credit a minute, 10 minutes", r.airtime(600, 0, 1, 1), (10, 0.0))
eq("leftover seconds are carried, not lost", r.airtime(90, 0, 1, 1), (1, 30.0))
eq("...and paid on a later landing", r.airtime(30, 30.0, 1, 1), (1, 0.0))
paid, carried = 0, 0.0
for _ in range(10):
    got, carried = r.airtime(50, carried, 1, 1)
    paid += got
eq("ten 50-second hops pay 8 minutes in the end", paid, 8)
eq("5 credits per 10 minutes", r.airtime(25 * 60, 0, 10, 5), (10, 300.0))
eq("credits 0 pays nothing and carries nothing", r.airtime(600, 50, 1, 0), (0, 0.0))

# ----------------------------------------------------------------- phrase
eq("one delivery", r.phrase([('Zone capture', 1, 50)]), "zone capture")
eq("several, joined",
   r.phrase([('Built FARP', 1, 40), ('Zone supply delivery', 2, 50)]),
   "FARP built and 2 × zone supply delivery")

failed = [x for x in results if not x[1]]
for label, ok, detail in failed:
    print(' FAIL ', label, '--', detail)
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
