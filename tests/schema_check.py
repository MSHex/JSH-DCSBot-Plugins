"""
Checks every plugin schema against the sample config it describes.

DCSServerBot validates a plugin's config against `schemas/<name>_schema.yaml`
on load (`core/plugin.py:450`), and with `validation: strict` in main.yaml a
mismatch stops the plugin. The same schemas drive the 3.0.5.0 web admin GUI's
config forms and the documentation site's configuration reference, so a key
missing from the schema is invisible in all three places.

pykwalify is not installable in this environment, so this does not run the
bot's own validator. It checks the three things that actually go wrong:

  * every `include:` resolves to a declared `schema;<name>` in the same file
  * every key in the sample config is reachable from the schema -- the drift
    that makes `validation: strict` refuse a config
  * every leaf carries `desc` and `example`, which the docs build needs; a
    missing `desc` is how a reference table ends up with a blank cell

    python3 -I tests/schema_check.py
"""
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS = ROOT / 'plugins'

#: Partials the bot supplies itself; `core/plugin.py` appends
#: schemas/commands_schema.yaml to every plugin's schema set.
EXTERNAL_PARTIALS = {'commands_schema', 'chat_commands_schema'}

#: Keys that are structure rather than a configurable leaf.
STRUCTURAL = {'type', 'nullable', 'required', 'mapping', 'sequence', 'include',
              'desc', 'example', 'enum', 'range', 'func', 'unique', 'matching',
              'pattern', 'name', 'default'}

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = '') -> None:
    checks.append((label, ok, detail))


def partials(doc: dict) -> dict:
    return {k.split(';', 1)[1]: v for k in doc for v in [doc[k]] if k.startswith('schema;')}


def resolve(rule, parts: dict, seen: frozenset = frozenset()):
    """Follow `include:` until an inline rule is reached."""
    while isinstance(rule, dict) and 'include' in rule:
        name = rule['include']
        if name in EXTERNAL_PARTIALS or name in seen:
            return None
        rule = parts.get(name)
        seen = seen | {name}
    return rule


def mapping_of(rule, parts):
    rule = resolve(rule, parts)
    return rule.get('mapping') if isinstance(rule, dict) else None


def covers(mapping: dict, key: str):
    """The rule for `key`, honouring pykwalify's regex; keys."""
    if key in mapping:
        return mapping[key]
    for pattern, rule in mapping.items():
        if pattern.startswith('regex;') and re.fullmatch(pattern.split(';', 1)[1], str(key)):
            return rule
    return None


def walk_config(value, rule, parts, path: str, plugin: str) -> None:
    """Every key in the sample config must be reachable from the schema."""
    if not isinstance(value, dict):
        return
    mapping = mapping_of(rule, parts)
    if mapping is None:
        return
    for key, child in value.items():
        found = covers(mapping, key)
        check(f"{plugin}: schema covers {path}/{key}", found is not None,
              'add it to the schema, or the bot refuses the config under '
              'validation: strict')
        if found is not None:
            walk_config(child, found, parts, f"{path}/{key}", plugin)


def walk_docs(rule, parts, path: str, plugin: str, seen: set) -> None:
    """Every configurable leaf needs desc and example for the docs build."""
    rule = resolve(rule, parts)
    if not isinstance(rule, dict):
        return
    marker = id(rule)
    if marker in seen:
        return
    seen.add(marker)

    if 'mapping' in rule:
        for key, child in rule['mapping'].items():
            walk_docs(child, parts, f"{path}/{key}", plugin, seen)
        return
    if 'sequence' in rule:
        for child in rule['sequence']:
            walk_docs(child, parts, f"{path}[]", plugin, seen)
        return
    if rule.get('type') in (None, 'map', 'seq'):
        return
    for field in ('desc', 'example'):
        check(f"{plugin}: {path} has {field}", bool(rule.get(field)),
              'the docs build renders this as a blank cell')


for plugin_dir in sorted(p for p in PLUGINS.iterdir() if p.is_dir()):
    name = plugin_dir.name
    schema_file = plugin_dir / 'schemas' / f'{name}_schema.yaml'
    config_files = sorted((plugin_dir / 'config').glob('*.yaml'))

    if not schema_file.exists():
        # A plugin with no config of its own needs no schema.
        check(f"{name}: has a schema for its config",
              not config_files,
              f'{config_files[0].name} exists but schemas/{name}_schema.yaml does not'
              if config_files else '')
        continue

    doc = yaml.safe_load(schema_file.read_text(encoding='utf-8')) or {}
    parts = partials(doc)
    check(f"{name}: schema parses and declares a root", bool(doc.get('mapping')))

    # Every include must resolve.
    def includes(node):
        if isinstance(node, dict):
            if 'include' in node:
                yield node['include']
            for v in node.values():
                yield from includes(v)
        elif isinstance(node, list):
            for v in node:
                yield from includes(v)

    for target in sorted(set(includes(doc))):
        check(f"{name}: include '{target}' resolves",
              target in parts or target in EXTERNAL_PARTIALS,
              'no matching schema;<name> block in this file')

    root_mapping = doc.get('mapping') or {}
    element = root_mapping.get('DEFAULT')
    if element is None:
        check(f"{name}: root declares DEFAULT", False, 'plugin configs are keyed on DEFAULT')
    else:
        walk_docs(element, parts, 'DEFAULT', name, set())
        for cfg in config_files:
            data = yaml.safe_load(cfg.read_text(encoding='utf-8')) or {}
            walk_config(data.get('DEFAULT') or {}, element, parts, 'DEFAULT', name)

failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    if not ok:
        print(' FAIL ', label, f'-- {detail}' if detail else '')
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed"
      f"{'' if not failed else '  (failures listed above)'}")
sys.exit(1 if failed else 0)
