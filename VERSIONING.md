# Versioning

Two version numbers live in this repository and they follow **different
rules**. Mixing them up breaks the bot at startup, so the distinction is worth
reading once.

## Repository releases — `vMAJOR.MINOR.PATCH`

Git tags and GitHub releases use full [semantic versioning](https://semver.org),
three components, always.

| | when |
| --- | --- |
| **MAJOR** | a change that requires action before upgrading — a config key renamed or removed, a schema change needing a manual step, a command removed |
| **MINOR** | a new plugin, a new command, a new capability. Nothing existing breaks |
| **PATCH** | bug fixes and documentation only. Deploy and forget |

Earlier tags (`v0.14`, `v0.16`) used two components. From `v0.17.1` onward
every tag has three. `v0.17` is treated as `v0.17.0`.

A release that fixes a bug **we shipped** is still a PATCH, however
embarrassing. `v0.17.1` is two of those.

## Plugin versions — `MAJOR.MINOR`, exactly two

Each plugin's `version.py` must have **two** components. This is not a style
choice; DCSServerBot enforces it. `core/plugin.py` migrates a plugin's schema
with:

```python
ver, rev = installed.split('.')
```

A three-part version raises `ValueError` there and the plugin fails to
migrate. `tests/test_plugin_packaging.py` checks this across every plugin, so
a mistake is caught before deploy rather than at bot startup.

The same test also checks that each plugin package exposes `__version__` from
its `__init__.py` — `core/plugin.py:254` reads the attribute off the
**package**, not off `version.py`, and a plugin missing that import does not
load at all. We shipped `v0.16` with exactly that bug.

| | when to bump the plugin |
| --- | --- |
| **MAJOR** | the plugin's own config or database shape changes incompatibly |
| **MINOR** | everything else — features and fixes alike |

A plugin's MINOR bump drives the repository's PATCH or MINOR, depending on
whether anything new appeared for the user.

## Database migrations

A plugin that ships `db/tables.sql` needs `db/update_v<installed>.sql` for
every upgrade step, named after the version being upgraded **from**.
`core/plugin.py` runs `tables.sql` only on a first install, so an existing
deployment never sees it. Both should use `IF NOT EXISTS` so the order never
matters.

## Release checklist

1. Every changed plugin's `version.py` bumped, two components.
2. `python3 -I tests/test_plugin_packaging.py` — packaging rules.
3. `python3 -I tests/schema_check.py` — every config key is in its schema.
4. `python3 -I tests/lua_balance_check.py` — every injected Lua file.
5. The plugin's own tests, including any that need PostgreSQL.
6. Tag `vMAJOR.MINOR.PATCH` on `main`, with notes saying what to copy where
   and whether a mission reload or DCS restart is needed.


## Plugin layout

DCSServerBot's own `plugins/sample` is the reference. Every plugin here
carries:

| | |
| --- | --- |
| `__init__.py` | exports the cog **and** `__version__` |
| `version.py` | two components, as above |
| `README.md` | becomes the plugin's page on the docs site; its first prose paragraph becomes the index summary |
| `schemas/<name>_schema.yaml` | required for any plugin with a config |
| `config/<name>.yaml` | the documented sample |
| `db/tables.sql`, `db/update_v<x.y>.sql` | only if the plugin owns tables |

### Schemas are not optional

`core/plugin.py:450` validates a plugin's config against its schema on load,
and with `validation: strict` in `main.yaml` a key missing from the schema
**stops the plugin**. The same file also drives the 3.0.5.0 web admin GUI's
config form and the documentation site's configuration reference, so a key
with no schema entry is invisible in all three places.

Every configurable leaf needs `desc` and `example`. A missing `desc` renders
as a blank cell in the reference table; `tests/schema_check.py` catches both
that and any config key the schema has drifted away from.

`func: is_node` on the instance section is worth having for its own sake: it
rejects a section named after a *server* rather than an instance, which
otherwise silently does nothing at all.
