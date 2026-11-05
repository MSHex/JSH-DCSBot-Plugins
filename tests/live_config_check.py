"""
Validates a LIVE DCSServerBot config/plugins/*.yaml against our schemas.

tests/schema_check.py checks the sample configs that ship in this repo.
Those always pass, because they are written alongside the schema. This checks
the configs that are actually deployed, which is where drift lives -- a key
renamed in the schema but not on the node, a section left over from an
instance that was removed, or a section named after a SERVER from
servers.yaml instead of an INSTANCE from nodes.yaml.

Run it on the node BEFORE setting `validation: strict` in main.yaml. Under
strict validation each of these findings stops the plugin at load.

    python3 -I tests/live_config_check.py --config-dir ~/DCSServerBot/config

It prints key PATHS and SECTION NAMES only, never values, so its output is
safe to paste. It reads nothing but YAML and writes nothing at all.

Exit status is 1 if anything was found, so it can gate a deploy step.
"""
import argparse
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS = ROOT / 'plugins'

#: Partials the bot supplies itself rather than the plugin.
EXTERNAL_PARTIALS = {'commands_schema', 'chat_commands_schema'}

#: Top-level keys that are never an instance section.
NON_INSTANCE_SECTIONS = {'DEFAULT'}

#: Subtrees a schema declares `type: any` because the value is genuinely
#: polymorphic -- `granted_ranks` takes either a mapping or a bare integer,
#: and a threat ring takes either a mapping or a bare radius. pykwalify
#: accepts anything there, and so will the bot under `validation: strict`,
#: which means a misspelled inner key passes validation and is then silently
#: dropped by our own code: a rank quietly loses its badge or its Foothold
#: level, and nothing is logged. The schema cannot catch that without giving
#: up the polymorphism, so the key sets our code actually reads are declared
#: here instead, keyed by the path the walk reaches them at.
#:
#: Keep these in step with the code named beside each one.
#: Keyed by the containing path, with instance sections stripped. Each entry
#: is (keys the code reads, what reads them, nested mappings and their keys).
ANY_SUBTREES = {
    'granted_ranks': (
        {'role', 'level', 'status', 'badge'},
        'jsh_ranks/grants.py resolve_spec()',
        {'badge': {'name', 'img'}},          # as_achievement(), badge shape
    ),
    'threat_rings/systems': (
        {'radius', 'colour', 'color', 'label'},
        'battlegroundbridge threat rings',
        {},
    ),
}


def partials(doc: dict) -> dict:
    return {k.split(';', 1)[1]: doc[k] for k in doc if k.startswith('schema;')}


def resolve(rule, parts: dict, seen: frozenset = frozenset()):
    while isinstance(rule, dict) and 'include' in rule:
        name = rule['include']
        if name in EXTERNAL_PARTIALS or name in seen:
            return None
        rule = parts.get(name)
        seen = seen | {name}
    return rule


def covers(mapping: dict, key: str):
    """The rule for `key`, honouring pykwalify's `regex;` keys."""
    if key in mapping:
        return mapping[key]
    for pattern, rule in mapping.items():
        if pattern.startswith('regex;') and re.fullmatch(pattern.split(';', 1)[1], str(key)):
            return rule
    return None


def container_of(path: str) -> str:
    """The path of the mapping an entry sits in, instance sections stripped.

    `[DCS.Foothold]/granted_ranks/General (O-10)` -> `granted_ranks`
    `[DEFAULT]/threat_rings/systems/Patriot`      -> `threat_rings/systems`
    """
    segs = [p for p in path.split('/') if p and not p.startswith('[')]
    return '/'.join(segs[:-1])


def check_any_subtree(value, path: str, found: list, stats: dict) -> None:
    """Validate ONE entry of a `type: any` mapping against what our code reads.

    `value` is the entry itself -- the rank's spec, or the threat ring's --
    because the walk descends into it before finding no schema rule to apply.
    """
    entry = ANY_SUBTREES.get(container_of(path))
    if entry is None:
        stats['unvalidated'].append(path)
        return
    allowed_keys, owner, nested = entry
    stats['any_checked'] += 1
    if not isinstance(value, dict):
        # A bare scalar: the documented shorthand. Nothing to check.
        return
    for key, child in value.items():
        if key not in allowed_keys:
            found.append(
                f"{path}/{key}  -- not a key {owner} reads; the schema "
                f"declares this subtree `type: any`, so the bot accepts it "
                f"under strict validation and our code then ignores it")
            continue
        if key in nested and isinstance(child, dict):
            for sub in child:
                if sub not in nested[key]:
                    found.append(
                        f"{path}/{key}/{sub}  -- not a key {owner} reads")


def walk(value, rule, parts: dict, path: str, found: list,
         stats: dict | None = None) -> None:
    """Report any key in the live config that the schema cannot reach."""
    stats = stats if stats is not None else {
        'checked': 0, 'any_checked': 0, 'unvalidated': []}
    if not isinstance(value, dict):
        return
    resolved = resolve(rule, parts)
    mapping = resolved.get('mapping') if isinstance(resolved, dict) else None
    if mapping is None:
        # `type: any` or an externally-supplied rule. The schema permits
        # anything here, so this is where a typo survives validation.
        check_any_subtree(value, path, found, stats)
        return
    for key, child in value.items():
        rule_for_key = covers(mapping, key)
        if rule_for_key is None:
            found.append(f"{path}/{key}  -- not in the schema")
            continue
        stats['checked'] += 1
        walk(child, rule_for_key, parts, f"{path}/{key}", found, stats)


def known_names(config_dir: pathlib.Path) -> tuple[set, set]:
    """(node names, instance names) from nodes.yaml, and server names."""
    nodes_file = config_dir / 'nodes.yaml'
    servers_file = config_dir / 'servers.yaml'
    nodes, instances, servers = set(), set(), set()
    if nodes_file.exists():
        doc = yaml.safe_load(nodes_file.read_text(encoding='utf-8')) or {}
        for node, body in doc.items():
            if node == 'DEFAULT' or not isinstance(body, dict):
                continue
            nodes.add(str(node))
            instances.update(str(i) for i in (body.get('instances') or {}))
    if servers_file.exists():
        doc = yaml.safe_load(servers_file.read_text(encoding='utf-8')) or {}
        servers.update(str(s) for s in doc if s != 'DEFAULT')
    return nodes | instances, servers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--config-dir', required=True,
                    help="the bot's config directory, the one holding nodes.yaml")
    args = ap.parse_args()

    config_dir = pathlib.Path(args.config_dir).expanduser()
    plugin_dir = config_dir / 'plugins'
    if not plugin_dir.is_dir():
        print(f"no plugins directory under {config_dir}")
        return 2

    allowed, servers = known_names(config_dir)
    if not allowed:
        print(f"warning: no node or instance names found in {config_dir / 'nodes.yaml'};"
              f" section names cannot be checked\n")

    findings: dict[str, list[str]] = {}
    coverage: dict[str, dict] = {}
    checked = 0

    for pkg in sorted(p for p in PLUGINS.iterdir() if p.is_dir()):
        name = pkg.name
        schema_file = pkg / 'schemas' / f'{name}_schema.yaml'
        live_file = plugin_dir / f'{name}.yaml'
        if not schema_file.exists() or not live_file.exists():
            continue

        checked += 1
        found: list[str] = []
        stats = {'checked': 0, 'any_checked': 0, 'unvalidated': []}
        doc = yaml.safe_load(schema_file.read_text(encoding='utf-8')) or {}
        parts = partials(doc)
        element = (doc.get('mapping') or {}).get('DEFAULT')
        live = yaml.safe_load(live_file.read_text(encoding='utf-8')) or {}

        for section, body in live.items():
            if section in NON_INSTANCE_SECTIONS:
                pass
            elif section in servers and section not in allowed:
                found.append(
                    f"[{section}]  -- named after a SERVER from servers.yaml; "
                    f"sections key on the INSTANCE name from nodes.yaml, so this "
                    f"section is silently doing nothing today and will be "
                    f"rejected under validation: strict")
                continue
            elif allowed and section not in allowed:
                found.append(
                    f"[{section}]  -- not a node or instance name in nodes.yaml")
                continue
            if element is not None:
                walk(body, element, parts, f"[{section}]", found, stats)

        if found:
            findings[name] = found
        coverage[name] = stats

    print(f"{'plugin':22s} {'keys':>5s} {'any':>4s}  unvalidated")
    for name in sorted(coverage):
        s = coverage[name]
        print(f"{name:22s} {s['checked']:5d} {s['any_checked']:4d}  "
              f"{len(s['unvalidated']) or ''}")
        for p in s['unvalidated'][:3]:
            print(f"{'':22s}       ?  {p}")
    total_keys = sum(s['checked'] for s in coverage.values())
    unvalidated = sum(len(s['unvalidated']) for s in coverage.values())
    print(f"\n{checked} live config(s), {total_keys} key(s) validated against "
          f"the schemas")

    if findings:
        print()
        for name in sorted(findings):
            print(f"{name}.yaml")
            for line in findings[name]:
                print(f"    {line}")
            print()
        print(f"{sum(len(v) for v in findings.values())} finding(s) in "
              f"{len(findings)} file(s) -- fix these before validation: strict")
        return 1

    if unvalidated:
        print(f"no findings, but {unvalidated} subtree(s) marked ? above were "
              f"not validated: the schema declares them `type: any` and no "
              f"ANY_SUBTREES entry names the keys our code reads there. Add "
              f"one, or accept that a typo inside them passes silently.")
        return 1
    print("no findings; these configs will survive validation: strict")
    return 0


if __name__ == '__main__':
    sys.exit(main())
