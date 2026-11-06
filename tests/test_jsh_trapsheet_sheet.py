"""
jsh_trapsheet's parsing and array building, checked without the bot.

plugins/jsh_trapsheet/sheet.py imports nothing from DCSServerBot, discord or
psycopg, so it runs here directly. The cases that matter are the ones that
would otherwise only show up as a wrong picture or a corrupted greenieboard:

  * an UNGRADED pass must store an EMPTY grade, never '--'. A '--' is a real
    LSO no-grade; writing it for a legacy deck that cannot grade at all would
    drag every pilot's average down with passes nobody flew badly.
  * plot_key() must reject an airframe the plotter has no AoA band for.
    greenieboard's plot_trapsheet() has no else branch and raises
    UnboundLocalError on `xmax` partway through drawing, so this is the only
    thing standing between an unsupported type and a traceback per trap.

    python3 -I tests/test_jsh_trapsheet_sheet.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / 'plugins' / 'jsh_trapsheet'))

import sheet  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = '') -> None:
    checks.append((label, ok, detail))


def eq(label: str, got, want) -> None:
    check(label, got == want, f"got {got!r}, want {want!r}")


# ------------------------------------------------------------------- grades
SC = "LSO: GRADE:OK: LUL(IC) WIRE# 3"
eq("grade from a normal SC comment", sheet.grade_of(SC), 'OK')
eq("wire from a normal SC comment", sheet.wire_of(SC), 3)
eq("points for OK", sheet.points_of('OK'), 4.0)

eq("--- is normalised to --",
   sheet.grade_of("LSO: GRADE:---: LOLOLO WIRE# 1"), '--')
eq("_OK_ survives the character class",
   sheet.grade_of("LSO: GRADE:_OK_: WIRE# 3"), '_OK_')
eq("(OK) survives the character class",
   sheet.grade_of("LSO: GRADE:(OK): WIRE# 2"), '(OK)')
eq("a bolter has no wire", sheet.wire_of("LSO: GRADE:B: LOIM"), None)

# Verbatim from dcs.log, Foothold Alpha, 2026-11-06 09:53:38, the first live
# trap after deploying. The grade did not parse: the old pattern listed the
# allowed characters and `N` was not one of them.
LIVE = "LSO: GRADE: NC : No proper communications"
eq("the first live grade parses -- a no-count, with a space after GRADE:",
   sheet.grade_of(LIVE), 'NC')
eq("a no-count has no wire", sheet.wire_of(LIVE), None)
eq("a no-count scores nothing", sheet.points_of('NC'), 0.0)

# greenieboard rewrites these before storing. The attach matches its row on
# the grade, so ours must be rewritten identically, or it never finds the row
# and inserts a second one.
for raw, stored in [('OWO', 'NC'), ('WOP', 'NC'), ('TWO', 'NC'),
                    ('TLU', 'NC'), ('WOFD', 'WO')]:
    eq(f"{raw} is stored as {stored}, as greenieboard stores it",
       sheet.grade_of(f"LSO: GRADE:{raw} : something"), stored)

# The legacy-deck case: no comment at all.
eq("no comment yields no grade", sheet.grade_of(''), None)
eq("no comment yields no wire", sheet.wire_of(''), None)
eq("no grade yields no points", sheet.points_of(None), None)
check("an ungraded pass must NOT score as a no-grade",
      sheet.points_of(None) != sheet.points_of('--'),
      "an ungraded pass scoring 2.0 would pull down every average")

# --------------------------------------------------------------- plot_key
for code, utype in [('FA-18', 'FA-18C_hornet'), ('FA-18', 'FA-18E'),
                    ('EA-18', 'EA-18G'), ('T-45', 'T-45'),
                    ('F-14B', 'F-14B'), ('AV8BNA', 'AV8BNA'),
                    ('A-4E-C', 'A-4E-C')]:
    eq(f"plot_key({utype})", sheet.plot_key(utype), code)

for utype in ['Su-33', 'MiG-29K', 'Ka-50', '', None]:
    check(f"plot_key({utype!r}) refuses an airframe the plotter cannot draw",
          sheet.plot_key(utype) is None,
          "plot_trapsheet() has no else branch and would raise on xmax")

check("the synthetic trapfile carries the code the AoA panel matches on",
      sheet.plot_key(sheet.trapfile_name('FA-18C_hornet', 'Hexe')) == 'FA-18',
      "the plotter reads the airframe out of the FILENAME, not pinfo")
check("a callsign with punctuation cannot break the filename",
      '/' not in sheet.trapfile_name('FA-18C_hornet', 'Dagger 1-1 | Joker')
      and '|' not in sheet.trapfile_name('FA-18C_hornet', 'Dagger 1-1 | Joker'))

# ------------------------------------------------------------- deck filter
# Prefixes, not exact names: the Super Carrier hulls are numbered one by one,
# so an exact-name list would match nothing on a real server. This is the
# same mistake the CA slot names cost us once already.
def sampled(typename: str) -> bool:
    return any(typename.startswith(p) for p in sheet.DEFAULT_DECKS)


# The four Super Carrier hulls, by their AIRBOSS CarrierType type names.
for hull in ['CVN_71', 'CVN_72', 'CVN_73', 'CVN_75']:
    check(f"{hull} is sampled", sampled(hull),
          "a Super Carrier hull that is not sampled gets no trapsheet at all")

# Stennis and Enterprise are NOT Super Carrier; Forrestal is Heatblur's and
# opt-in until it is confirmed to be graded. 'cvn-65' is lower case with a
# hyphen, and must not slip through a careless prefix.
for hull in ['Stennis', 'KUZNECOW', 'LHA_Tarawa', 'CVN', 'cvn-65',
             'Forrestal', 'USS Enterprise 1966']:
    check(f"{hull} is not sampled", not sampled(hull),
          "only Super Carrier decks raise S_EVENT_LANDING_QUALITY_MARK, so a "
          "pass here could never be graded")

eq("the deck list renders as a Lua table",
   sheet.lua_list(('CVN_', 'Forrestal')), '{"CVN_","Forrestal"}')
eq("an empty deck list is still valid Lua", sheet.lua_list([]), '{}')
check("a quote in a deck name cannot break out of the literal",
      sheet.lua_list(['a"b']) == '{"a\\"b"}')

# ------------------------------------------------------------------- arrays
pts = [[1852.0, 10.0, 400.0, 8.1], [926.0, 5.0, 200.0, 7.9], [0.0, 0.0, 0.0, 8.4]]
ts = sheet.to_trapsheet(pts)
eq("to_trapsheet yields exactly the plotter's columns",
   sorted(ts), sorted(sheet.COLUMNS))
eq("X is carried through", list(ts['X']), [1852.0, 926.0, 0.0])
eq("AoA is carried through", list(ts['AoA']), [8.1, 7.9, 8.4])
eq("invert_x mirrors the along-deck axis",
   list(sheet.to_trapsheet(pts, {'invert_x': True})['X']), [-1852.0, -926.0, -0.0])
eq("invert_z mirrors only the lateral axis",
   list(sheet.to_trapsheet(pts, {'invert_z': True})['Z']), [-10.0, -5.0, -0.0])
check("inverting Z leaves X alone",
      list(sheet.to_trapsheet(pts, {'invert_z': True})['X']) == [1852.0, 926.0, 0.0])

eq("an empty pass yields empty arrays, not a crash",
   [len(v) for v in sheet.to_trapsheet([]).values()], [0, 0, 0, 0])
eq("a malformed row is dropped rather than poisoning the array",
   len(sheet.to_trapsheet([[1.0, 2.0], [1.0, 2.0, 3.0, 4.0]])['X']), 1)

# ------------------------------------------------------------- groove time
# 3/4 NM is 1389 m. At 0.1 s a sample, 180 samples inside the wake is 18.0 s.
inside = [[1389.0 * i / 180.0, 0.0, 0.0, 8.1] for i in range(1, 181)]
eq("groove time counts only what is inside the wake",
   sheet.groove_time(inside, 0.1), 18.0)
eq("samples outside the wake do not count",
   sheet.groove_time([[3000.0, 0, 0, 8.1]] * 50 + inside, 0.1), 18.0)
eq("no samples yields no groove time", sheet.groove_time([], 0.1), None)
eq("a zero rate cannot divide anything", sheet.groove_time(inside, 0), None)

# ------------------------------------------------------------ touchdown
# The last 16 samples of the first live trap (row 36), verbatim: touchdown at
# 72.5 m and 0.4 ft, then the rollout in the wires at -0.5 ft while the nose
# comes down and the computed AoA falls through zero.
TAIL = [[78, -7.3, 1.2, 9.73], [72.5, -8.1, 0.4, 9.65], [67, -8.9, 0.1, 9.52],
        [61.6, -9.7, 0, 9.34], [56.1, -10.5, -0.2, 8.89], [50.7, -11.3, -0.5, 8.21],
        [45.4, -12.1, -0.6, 7.21], [40.1, -12.8, -0.5, 5.95], [35, -13.5, -0.4, 4.6],
        [30, -14.2, -0.4, 3.35], [25.2, -14.8, -0.5, 2.08], [20.7, -15.4, -0.6, 1.02],
        [16.4, -16, -0.6, -0.01], [12.4, -16.5, -0.6, -0.83], [8.7, -17, -0.6, -1.58],
        [0, -18.3, -0.6, -3.04]]
AIR = [[1800 - i * 10, 0.0, 400 - i * 2.0, 8.1] for i in range(150)]
cut = sheet.trim_touchdown(AIR + TAIL)
eq("the trace ends at touchdown, the first sample at or below 0.5 ft",
   cut[-1], [72.5, -8.1, 0.4, 9.65])
check("no rollout sample survives -- the AoA no longer falls through zero",
      min(p[3] for p in cut) > 6, f"min AoA kept {min(p[3] for p in cut)}")
eq("a pass that never touches down is left whole",
   len(sheet.trim_touchdown(AIR)), len(AIR))
eq("an empty pass stays empty", sheet.trim_touchdown([]), [])

# --------------------------------------------------------------- details
eq("the LSO comment after the grade, without the wire",
   sheet.details_of("LSO: GRADE:C :  _LOIC_  _LOAR_  WIRE# 2 _EGIW_ [BC]"),
   "_LOIC_ _LOAR_ _EGIW_ [BC]")
eq("a no-count's prose survives details_of",
   sheet.details_of("LSO: GRADE: NC : No proper communications"),
   "No proper communications")
eq("mission clock as hh:mm:ss", sheet.clock_text(10 * 3600 + 56 * 60 + 44), "10:56:44")
eq("a clock past midnight wraps", sheet.clock_text(86400 + 61), "00:01:01")
eq("no clock, no text", sheet.clock_text(None), None)
eq("CVN_72 is the Lincoln, per AIRBOSS", sheet.CARRIERS['CVN_72'], 'Lincoln')
eq("prose is passed through, not decoded into letters",
   sheet.decode("No proper communications", {'N': 'Nose', 'O': 'x'}, {'IC': 'In close'}),
   [('Overall', ['No proper communications'])])

# ------------------------------------------------------------ presentation
eq("a graded pass shows the grade and what it means",
   sheet.grade_line('_OK_'), '**_OK_** \u2014 Unicorn \u2014 a perfect pass')
eq("an ungraded pass says so rather than inventing one",
   sheet.grade_line(None), 'Not graded')
check("every grade that scores points also has wording",
      all(g in sheet.GRADE_TEXT for g in sheet.POINTS),
      "a grade with points but no text would post a bare letter")
check("every grade that scores points also has a colour",
      all(g in sheet.GRADE_COLOUR for g in sheet.POINTS))
check("an unknown grade still gets a usable colour",
      isinstance(sheet.colour_of('???'), int))
check("no grade gets the neutral colour, not the good one",
      sheet.colour_of(None) != sheet.colour_of('OK'))

failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    if not ok:
        print(' FAIL ', label, f'-- {detail}' if detail else '')
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed"
      f"{'' if not failed else '  (failures listed above)'}")
sys.exit(1 if failed else 0)
