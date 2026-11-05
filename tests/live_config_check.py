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


def walk(value, rule, parts: dict, path: str, found: list) -> None:
    """Report any key in the live config that the schema cannot reach."""
    if not isinstance(value, dict):
        return
    resolved = resolve(rule, parts)
    mapping = resolved.get('mapping') if isinstance(resolved, dict) else None
    if mapping is None:
        # A free-form or externally-supplied rule: nothing to check below here.
        return
    for key, child in value.items():
        rule_for_key = covers(mapping, key)
        if rule_for_key is None:
            found.append(f"{path}/{key}  -- not in the schema")
            continue
        walk(child, rule_for_key, parts, f"{path}/{key}", found)


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
    checked = 0

    for pkg in sorted(p for p in PLUGINS.iterdir() if p.is_dir()):
        name = pkg.name
        schema_file = pkg / 'schemas' / f'{name}_schema.yaml'
        live_file = plugin_dir / f'{name}.yaml'
        if not schema_file.exists() or not live_file.exists():
            continue

        checked += 1
        found: list[str] = []
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
                walk(body, element, parts, f"[{section}]", found)

        if found:
            findings[name] = found

    for name in sorted(findings):
        print(f"{name}.yaml")
        for line in findings[name]:
            print(f"    {line}")
        print()

    live_present = sorted(p.stem for p in plugin_dir.glob('*.yaml')
                          if (PLUGINS / p.stem / 'schemas' / f'{p.stem}_schema.yaml').exists())
    print(f"checked {checked} of our plugins with a live config"
          f"{': ' + ', '.join(live_present) if live_present else ''}")
    if findings:
        print(f"{sum(len(v) for v in findings.values())} finding(s) in "
              f"{len(findings)} file(s) -- fix these before validation: strict")
        return 1
    print("no findings; these configs will survive validation: strict")
    return 0


if __name__ == '__main__':
    sys.exit(main())
