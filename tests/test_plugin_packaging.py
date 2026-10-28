"""
The packaging rules DCSServerBot enforces at load time, checked here instead.

Both of these fail at BOT STARTUP, not at import, and the traceback points
into core/plugin.py rather than at the plugin that is wrong -- so they cost a
restart and a puzzled log read each time. Both have now bitten this repo once:

  __version__ on the PACKAGE. core/plugin.py:242 does

      getattr(sys.modules['plugins.' + self.plugin_name], '__version__')

  which reads the attribute off the package, not off version.py. A plugin
  whose __init__.py imports its cog but not its version raises
  "module 'plugins.X' has no attribute '__version__'" and does not load.
  jsh_statfix 1.0 shipped that way.

  TWO-PART versions. core/plugin.py:365 does `ver, rev = installed.split('.')`
  while migrating, so a three-part "1.2.3" raises ValueError there.

Parsed as text rather than imported: these packages need discord, psycopg and
DCSServerBot's core on the path, none of which are here.

    python3 -I tests/test_plugin_packaging.py
"""
import ast
import pathlib
import re
import sys

PLUGINS = pathlib.Path(__file__).resolve().parent.parent / 'plugins'

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = '') -> None:
    checks.append((label, ok, detail))


def exports_version(init_path: pathlib.Path) -> bool:
    """True when `__version__` ends up as an attribute of the package."""
    tree = ast.parse(init_path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == '__version__' or alias.asname == '__version__'
                   for alias in node.names):
                return True
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
                return True
    return False


packages = sorted(p for p in PLUGINS.iterdir()
                  if p.is_dir() and (p / '__init__.py').exists())
check('found plugin packages', bool(packages), f"{len(packages)} found")

for pkg in packages:
    name = pkg.name
    init = pkg / '__init__.py'
    version_py = pkg / 'version.py'

    check(f"{name}: package exposes __version__",
          exports_version(init),
          "add `from .version import __version__` to __init__.py")

    if not version_py.exists():
        check(f"{name}: has version.py", False, 'missing')
        continue

    match = re.search(r'''__version__\s*=\s*["']([^"']+)["']''',
                      version_py.read_text(encoding='utf-8'))
    if not match:
        check(f"{name}: version.py sets __version__", False, 'not found')
        continue

    version = match.group(1)
    check(f"{name}: version {version} is MAJOR.MINOR",
          bool(re.fullmatch(r'\d+\.\d+', version)),
          "core/plugin.py does `ver, rev = installed.split('.')`")

failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    print(('  ok  ' if ok else ' FAIL '), label, f"-- {detail}" if detail and not ok else '')
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
