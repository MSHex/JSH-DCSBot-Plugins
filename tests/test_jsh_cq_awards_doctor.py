"""
jsh_cq's rules, jsh_awards' board and jsh_doctor's checks, without a bot.

    python3 -B -I tests/test_jsh_cq_awards_doctor.py
"""
import importlib.util
import pathlib
import sys
from datetime import date, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'plugins'


def load(plugin, module):
    spec = importlib.util.spec_from_file_location(f"{plugin}_{module}", ROOT / plugin / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rules = load('jsh_cq', 'rules')
board = load('jsh_awards', 'board')
checks = load('jsh_doctor', 'checks')

results: list[tuple[str, bool, str]] = []


def eq(label, got, want):
    results.append((label, got == want, f"got {got!r}, want {want!r}"))


# ================================================================ jsh_cq
NOW = datetime(2026, 11, 12, 12, 0)
R = rules.Rules(day_traps=4, night_traps=2, valid_days=90)


def ago(days):
    return NOW - timedelta(days=days)


DAY4 = [(False, ago(d)) for d in (1, 10, 20, 30)]
st = rules.standing(DAY4, R, NOW)
eq("4 day traps: day CQ", st.day_expires is not None, True)
eq("...expiring when the OLDEST counting trap leaves the window", st.day_expires, ago(30) + timedelta(days=90))
eq("...and no night CQ without night traps", st.night_expires, None)
eq("3 day traps: not yet", rules.standing(DAY4[:3], R, NOW).day_expires, None)
eq("a trap outside the window does not count",
   rules.standing(DAY4[:3] + [(False, ago(91))], R, NOW).day_expires, None)
eq("night traps do not count towards DAY",
   rules.standing(DAY4[:3] + [(True, ago(2))], R, NOW).day_expires, None)
eq("a 5th, newer trap pushes expiry forward -- the CQ renews",
   rules.standing(DAY4 + [(False, ago(0.5))], R, NOW).day_expires, ago(20) + timedelta(days=90))

NIGHT2 = [(True, ago(5)), (True, ago(60))]
st = rules.standing(DAY4 + NIGHT2, R, NOW)
eq("4 day + 2 night: night CQ", st.night_expires is not None, True)
eq("...expiring with the 2nd night trap, or the day CQ if sooner",
   st.night_expires, min(ago(60), ago(30)) + timedelta(days=90))
eq("night traps with NO day CQ: no night CQ",
   rules.standing(DAY4[:2] + NIGHT2, R, NOW).night_expires, None)
eq("night_traps 0 turns night CQ off",
   rules.standing(DAY4 + NIGHT2, rules.Rules(4, 0, 90), NOW).night_expires, None)
eq("progress line", rules.progress_line(rules.standing(DAY4[:3] + NIGHT2[:1], R, NOW), R),
   "Day 3/4 · Night 1/2")

future, past = NOW + timedelta(days=10), NOW - timedelta(days=1)
eq("no row: a new grant", rules.is_new_grant(None, False, NOW), True)
eq("a lapsed row not yet deleted by logbook: a new grant", rules.is_new_grant(past, True, NOW), True)
eq("a current row: a renewal, not announced", rules.is_new_grant(future, True, NOW), False)
eq("a permanent (manual) grant is never touched",
   rules.should_write(None, True, NOW + timedelta(days=99)), False)
eq("a qualification is never shortened", rules.should_write(future, True, NOW + timedelta(days=5)), False)
eq("...but is extended", rules.should_write(future, True, NOW + timedelta(days=50)), True)

# ============================================================ jsh_awards
eq("month bounds", board.month_bounds(date(2026, 10, 1)),
   (datetime(2026, 10, 1), datetime(2026, 11, 1)))
eq("December runs into January", board.month_bounds(date(2026, 12, 1))[1], datetime(2027, 1, 1))
eq("last month, from January", board.previous_month(date(2027, 1, 5)), date(2026, 12, 1))
eq("last month", board.previous_month(date(2026, 11, 1)), date(2026, 10, 1))
eq("parse 2026-10", board.parse_month('2026-10', date(2026, 11, 12)), date(2026, 10, 1))
eq("parse nothing: last month", board.parse_month(None, date(2026, 11, 12)), date(2026, 10, 1))
eq("parse rubbish", board.parse_month('October', date(2026, 11, 12)), None)
eq("parse month 13", board.parse_month('2026-13', date(2026, 11, 12)), None)

MS, ME = datetime(2026, 10, 1), datetime(2026, 11, 1)
eq("clip: a campaign running all month", board.clip(MS, ME, datetime(2026, 1, 1), None), (MS, ME))
eq("clip: begun on the 12th counts from the 12th",
   board.clip(MS, ME, datetime(2026, 10, 12), None), (datetime(2026, 10, 12), ME))
eq("clip: ended on the 20th counts to the 20th",
   board.clip(MS, ME, datetime(2026, 1, 1), datetime(2026, 10, 20)), (MS, datetime(2026, 10, 20)))
eq("clip: ended before the month: nothing",
   board.clip(MS, ME, datetime(2026, 1, 1), datetime(2026, 9, 30)), None)
eq("clip: starts after the month: nothing", board.clip(MS, ME, datetime(2026, 11, 2), None), None)
eq("K/D", board.kd(10, 4), 2.5)
eq("K/D with no deaths is the kills, as the bot does it", board.kd(7, 0), 7.0)
rows = [
    dict(ucid='a', name='Able', discord_id=1, kills=20, deaths=4),     # 5.0
    dict(ucid='b', name='Baker', discord_id=-1, kills=6, deaths=0),    # 6.0
    dict(ucid='c', name='Charlie', discord_id=3, kills=2, deaths=0),   # too few kills
    dict(ucid='d', name='Dog', discord_id=4, kills=12, deaths=2),      # 6.0, more kills
]
ranked = board.ranked_kd(rows, min_kills=5, top=3)
eq("K/D ranked, ties broken by kills, below min_kills dropped",
   [e.ucid for e in ranked], ['d', 'b', 'a'])
eq("an unlinked pilot is shown by name", board.who(ranked[1]), 'Baker')
eq("a linked pilot by mention", board.who(ranked[0]), '<@4>')
eq("an empty category says so", board.field('hours', []), "*No qualifying pilots this month.*")
eq("hours text", board.value_text('hours', board.Entry('x', 'X', 1, 12.34)), "12.3 h")
eq("one rescue, singular", board.value_text('csar', board.Entry('x', 'X', 1, 1)), "1 rescue")
eq("the citation names the month -- what stops a re-post awarding twice",
   board.citation('hours', date(2026, 10, 1), board.Entry('x', 'X', 1, 40.0)),
   "Most flight hours, October 2026: 40.0 h.")

# ============================================================ jsh_doctor
NODES = checks.instances_by_node({
    'NASA-SERVER': {'instances': {'DCS.Foothold': {}, 'DCS.Training': {}}},
    'GoyaGang': {'instances': {'DCS.Dynamic': {}}},
})
eq("instances read from nodes.yaml", NODES['GoyaGang'], {'DCS.Dynamic'})
SERVERS = ['JSH Foothold Alpha', 'JSH Training']
f = checks.config_sections('jsh_ranks', {'DEFAULT': {}, 'DCS.Foothold': {}}, NODES, SERVERS)
eq("sections on instance names: nothing to report", f, [])
f = checks.config_sections('jsh_ranks', {'DEFAULT': {}, 'JSH Foothold Alpha': {}}, NODES, SERVERS)
eq("a section named after a SERVER is a failure", [x.level for x in f], ['fail'])
f = checks.config_sections('jsh_ranks', {'DCS.Foothld': {}}, NODES, SERVERS)
eq("a typo'd instance is a warning naming the real ones",
   (f[0].level, 'DCS.Foothold' in f[0].text), ('warn', True))
f = checks.config_sections('x', {'GoyaGang': {'DCS.Dynamic': {}, 'DCS.Foothold': {}}}, NODES, SERVERS)
eq("node -> instance: an instance from ANOTHER node is flagged",
   [x.text.split('`')[3] for x in f], ['GoyaGang → DCS.Foothold'])
eq("strict validation passes", checks.validation({'validation': 'strict'}).level, 'ok')
eq("lazy validation warns", checks.validation({}).level, 'warn')

eq("Foothold config with a space: ok",
   [x.level for x in checks.foothold_files('I', ['Foothold Config.lua', 'save.lua'])], ['ok'])
eq("with an underscore: failure",
   [x.level for x in checks.foothold_files('I', ['Foothold_Config.lua'])], ['fail'])
eq("no Foothold config at all: nothing said", checks.foothold_files('I', ['other.lua']), [])
eq("RankingSystem = true", checks.ranking_system('I', "RankingSystem = true\n").level, 'ok')
eq("RankingSystem = false", checks.ranking_system('I', "x=1\nRankingSystem = false -- off\n").level, 'fail')
eq("a COMMENTED-OUT line is not the setting",
   checks.ranking_system('I', "-- RankingSystem = true\nRankingSystem = false\n").level, 'fail')
eq("...nor one inside a block comment",
   checks.ranking_system('I', "--[[\nRankingSystem = false\n]]\nRankingSystem = true\n").level, 'ok')

f = checks.role_order(10, {'Major (O-4)': 5, 'Colonel (O-6)': 12, 'Ghost': None}, "Rank roles")
eq("roles above the bot, and missing roles, are failures",
   [(x.level, 'Colonel' in x.text or 'Ghost' in x.text) for x in f], [('fail', True), ('fail', True)])
eq("all below the bot: ok", [x.level for x in checks.role_order(10, {'A': 1, 'B': 2}, "R")], ['ok'])
eq("members intent off is a failure", checks.members_intent(False).level, 'fail')
eq("database: reversed sessions fail, stale open warn",
   [x.level for x in checks.database(3, 2, ['CQ_CV_DAY'])], ['fail', 'warn', 'warn'])
eq("database: clean", [x.level for x in checks.database(0, 0, [])], ['ok'])
eq("summary", checks.summary(checks.database(3, 2, [])), "1 problem(s), 1 warning(s).")

failed = [r for r in results if not r[1]]
for label, ok, detail in failed:
    print(' FAIL ', label, '--', detail)
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
