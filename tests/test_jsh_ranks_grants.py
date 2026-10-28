"""
Granted-rank resolution: the config shapes accepted, and the achievement-shaped
dict rankstatus renders from them.

grants.py imports nothing from DCSServerBot or discord, which is what lets this
run as a plain script and what lets rankstatus read it without a cycle.

    python3 -I tests/test_jsh_ranks_grants.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'plugins' / 'jsh_ranks'))
import grants as G  # noqa: E402

checks, fails = [], []
def eq(label, got, want):
    ok = got == want
    checks.append((label, got, want, ok))
    if not ok: fails.append(label)

GR = {
    'Lieutenant General (O-9)': {
        'role': 'Lieutenant General (O-9)', 'level': 18, 'status': 'Staff',
        'badge': {'name': 'Lieutenant General (O-9)', 'img': 'https://x/o9.png'}},
    'General (O-10)': {
        'role': 'General (O-10)', 'level': 19,
        'badge': {'img': 'https://x/o10.png'}},      # name omitted on purpose
    'Founder': 20,                                    # bare-number shorthand
    'Bare': {'level': 7},                             # mapping, no role, no badge
}

eq('full spec resolves', G.resolve_spec(GR, 'Lieutenant General (O-9)'),
   {'role': 'Lieutenant General (O-9)', 'level': 18, 'status': 'Staff',
    'badge': {'name': 'Lieutenant General (O-9)', 'img': 'https://x/o9.png'}})
eq('badge name defaults to the rank', G.resolve_spec(GR, 'General (O-10)')['badge'],
   {'name': 'General (O-10)', 'img': 'https://x/o10.png'})
eq('bare number is the level', G.resolve_spec(GR, 'Founder'),
   {'role': 'Founder', 'level': 20, 'badge': None, 'status': None})
eq('role defaults to the rank', G.resolve_spec(GR, 'Bare')['role'], 'Bare')
eq('case-insensitive', G.resolve_spec(GR, 'general (o-10)')['level'], 19)
eq('unknown rank is not granted-only', G.resolve_spec(GR, 'Captain (O-3)'), None)
eq('empty name', G.resolve_spec(GR, ''), None)
eq('empty config', G.resolve_spec({}, 'Founder'), None)

# as_achievement must match the shape rankstatus already renders
eq('achievement carries role and badge',
   G.as_achievement('General (O-10)', G.resolve_spec(GR, 'General (O-10)')),
   {'role': 'General (O-10)', 'badge': {'name': 'General (O-10)', 'img': 'https://x/o10.png'}})
eq('no badge -> no badge key (thumbnail falls back to earned)',
   G.as_achievement('Founder', G.resolve_spec(GR, 'Founder')), {'role': 'Founder'})
eq('unconfigured grant still renders its name',
   G.as_achievement('Mystery Rank', None), {'role': 'Mystery Rank'})

ach = G.as_achievement('General (O-10)', G.resolve_spec(GR, 'General (O-10)'))
eq('never carries credits or playtime (must not reach get_rank_progress)',
   [k for k in ach if k in ('credits', 'playtime')], [])

# --- the status line shown in place of promotion progress ---------------
o9 = G.resolve_spec(GR, 'Lieutenant General (O-9)')
o10 = G.resolve_spec(GR, 'General (O-10)')

eq('rank status is wrapped by the default format',
   G.status_line('Lieutenant General (O-9)', o9),
   '🏆 Staff — outside the campaign ladder.')
eq('a rank with no status falls back to the default label',
   G.status_line('General (O-10)', o10),
   '🏆 Granted rank — outside the campaign ladder.')
eq('default label is overridable',
   G.status_line('General (O-10)', o10, default_status='Command'),
   '🏆 Command — outside the campaign ladder.')
eq('format is overridable and can use {rank}',
   G.status_line('Lieutenant General (O-9)', o9, fmt='{rank} · {status} team'),
   'Lieutenant General (O-9) · Staff team')
eq('a format with no placeholders is taken literally',
   G.status_line('X', o9, fmt='Staff rank.'), 'Staff rank.')
# A typo in config must never take down /pilot status.
eq('an unknown placeholder degrades to the label alone',
   G.status_line('X', o9, fmt='{nonsense} rank'), 'Staff')
eq('an unterminated placeholder degrades too',
   G.status_line('X', o9, fmt='{status rank'), 'Staff')
eq('no spec at all still produces a line',
   G.status_line('Mystery', None), '🏆 Granted rank — outside the campaign ladder.')

for label, got, want, ok in checks:
    print(('  ok  ' if ok else ' FAIL '), label, '' if ok else f'-> {got!r} (wanted {want!r})')
print(f"\n{len(checks)-len(fails)}/{len(checks)} passed")
sys.exit(1 if fails else 0)
