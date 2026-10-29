"""
Block-balance check for every Lua file we inject into DCS.

There is no Lua interpreter in CI or in the dev container, and an unbalanced
block is not caught until DCS loads the script and the mission silently does
nothing -- the failure mode that is most expensive to diagnose, because the bot
log shows the script was sent and dcs.log shows nothing at all.

Strings and comments are removed first, so an `end` inside a message or a
`--` in a URL cannot throw the count off.

    python3 -I tests/lua_balance_check.py plugins/*/lua/*.lua
"""
import re, sys

def strip(src):
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith('--[[', i):
            j = src.find(']]', i); i = n if j < 0 else j + 2; continue
        if src.startswith('--', i):
            j = src.find('\n', i); i = n if j < 0 else j; continue
        c = src[i]
        if c in '"\'':
            i += 1
            while i < n and src[i] != c:
                i += 2 if src[i] == '\\' else 1
            i += 1; out.append(' '); continue
        if src.startswith('[[', i):
            j = src.find(']]', i); i = n if j < 0 else j + 2; out.append(' '); continue
        out.append(c); i += 1
    return ''.join(out)

def balance(src):
    toks = re.findall(r'\b\w+\b', strip(src))
    depth, pending_do, stack = 0, 0, []
    for t in toks:
        if t == 'function':
            depth += 1; stack.append('function')
        elif t == 'if':
            depth += 1; stack.append('if')
        elif t in ('for', 'while'):
            depth += 1; stack.append(t); pending_do += 1
        elif t == 'do':
            if pending_do: pending_do -= 1      # belongs to for/while
            else: depth += 1; stack.append('do')
        elif t == 'repeat':
            stack.append('repeat')
        elif t == 'until':
            if stack and stack[-1] == 'repeat': stack.pop()
        elif t == 'end':
            depth -= 1
            if stack: stack.pop()
            if depth < 0: return depth, stack, 'more `end` than blocks'
    return depth, stack, None

import glob
paths = sys.argv[1:] or sorted(glob.glob('plugins/*/lua/*.lua'))
failed = 0
for path in paths:
    src = open(path, encoding='utf-8').read()
    depth, stack, err = balance(src)
    ok = depth == 0 and not err
    failed += not ok
    status = 'BALANCED' if ok else f'UNBALANCED depth={depth} {err or ""} open={stack[-3:]}'
    print(f"{status:<28} {path}")
print(f"\n{len(paths) - failed}/{len(paths)} balanced")
sys.exit(1 if failed else 0)
