"""
The checks behind /jsh doctor, as pure functions over plain data.

Each one is a mistake that has already cost JSH an evening, and that the bot
itself either does not report or reports somewhere unhelpful. See CLAUDE.md.
Nothing here imports DCSServerBot, discord or psycopg.
"""
import re
from dataclasses import dataclass
from typing import Iterable, Optional

OK, WARN, FAIL, SKIP = 'ok', 'warn', 'fail', 'skip'
ICONS = {OK: '✅', WARN: '⚠️', FAIL: '❌', SKIP: '➖'}


@dataclass(frozen=True)
class Finding:
    level: str
    area: str
    text: str

    def line(self) -> str:
        return f"{ICONS[self.level]} {self.text}"


# ------------------------------------------------------------ nodes.yaml
def instances_by_node(nodes_yaml: dict) -> dict[str, set[str]]:
    """{node: {instance, ...}} from nodes.yaml as loaded."""
    out: dict[str, set[str]] = {}
    for node, body in (nodes_yaml or {}).items():
        names = set(((body or {}).get('instances') or {}).keys())
        out[str(node)] = {str(n) for n in names}
    return out


#: Top-level keys of a plugin config that are not instances.
RESERVED = {'DEFAULT', 'commands', 'chat_commands'}


def config_sections(plugin: str, config: dict, nodes: dict[str, set[str]],
                    server_names: Iterable[str]) -> list[Finding]:
    """
    A plugin config keys its sections on the INSTANCE name from nodes.yaml --
    or on a node, with instances inside. A section named after a SERVER
    validates, matches nothing and silently does nothing.
    """
    found = []
    instances = set().union(*nodes.values()) if nodes else set()
    servers = set(server_names)
    for key, body in (config or {}).items():
        key = str(key)
        if key in RESERVED or key in instances:
            continue
        if key in nodes:
            for inner in (body or {}):
                inner = str(inner)
                if inner in RESERVED or inner in nodes[key]:
                    continue
                found.append(_bad_section(plugin, f"{key} → {inner}", inner, servers, nodes[key]))
            continue
        found.append(_bad_section(plugin, key, key, servers, instances))
    return found


def _bad_section(plugin: str, where: str, key: str, servers: set[str],
                 instances: set[str]) -> Finding:
    if key in servers:
        return Finding(FAIL, 'config',
                       f"`{plugin}.yaml`: section `{where}` is a SERVER name. Sections key on the "
                       f"instance name from nodes.yaml, so this one does nothing.")
    hint = f" Instances are: {', '.join(sorted(instances))}." if instances else ""
    return Finding(WARN, 'config',
                   f"`{plugin}.yaml`: section `{where}` matches no instance or node, so it "
                   f"does nothing.{hint}")


def validation(main_yaml: dict) -> Finding:
    mode = str((main_yaml or {}).get('validation', 'lazy')).lower()
    if mode == 'strict':
        return Finding(OK, 'config', "`validation: strict` -- a bad config stops at load.")
    return Finding(WARN, 'config',
                   f"`validation: {mode}` in main.yaml. Under it a misnamed section or key is "
                   f"only a log line; `strict` stops the plugin and says why.")


# ---------------------------------------------------------------- Foothold
_CONFIG = re.compile(r'^Foothold[ _]Config.*\.lua$', re.IGNORECASE)


def foothold_files(instance: str, names: Optional[Iterable[str]]) -> list[Finding]:
    """
    The external config must be `Foothold Config.lua`, with a SPACE. With an
    underscore Foothold quietly uses the copy inside the .miz instead.
    `names` is the Missions/Saves listing, or None if it is not readable here.
    """
    if names is None:
        return [Finding(SKIP, 'foothold', f"{instance}: Missions/Saves not readable from this node.")]
    configs = [n for n in names if _CONFIG.match(n)]
    out = []
    for n in configs:
        if n.lower().startswith('foothold_config'):
            out.append(Finding(FAIL, 'foothold',
                               f"{instance}: `{n}` has an UNDERSCORE. Foothold only loads "
                               f"`{n.replace('_', ' ', 1)}`, so the .miz copy is used instead."))
    if configs and not out:
        out.append(Finding(OK, 'foothold', f"{instance}: external config `{configs[0]}` is named right."))
    return out


_RANKING = re.compile(r'^\s*RankingSystem\s*=\s*(\w+)', re.MULTILINE)


def ranking_system(instance: str, text: Optional[str]) -> Optional[Finding]:
    """RankingSystem = false turns every rank gate off; jsh_ranks then changes nothing."""
    if text is None:
        return None
    m = _RANKING.search(_strip_lua_comments(text))
    if not m:
        return Finding(WARN, 'foothold', f"{instance}: no `RankingSystem =` line in the external config.")
    if m.group(1).lower() == 'true':
        return Finding(OK, 'foothold', f"{instance}: `RankingSystem = true`.")
    return Finding(FAIL, 'foothold',
                   f"{instance}: `RankingSystem = {m.group(1)}` -- every rank gate is off, so "
                   f"nothing jsh_ranks sends has any effect.")


def _strip_lua_comments(text: str) -> str:
    text = re.sub(r'--\[(=*)\[.*?\]\1\]', '', text, flags=re.DOTALL)
    return re.sub(r'--[^\n]*', '', text)


# ----------------------------------------------------------------- Discord
def role_order(bot_top: int, roles: dict[str, Optional[int]], what: str) -> list[Finding]:
    """
    The bot can only add or remove roles BELOW its own highest one. `roles`
    is {name: position}, position None for a role that does not exist.
    """
    out = []
    missing = sorted(n for n, p in roles.items() if p is None)
    above = sorted(n for n, p in roles.items() if p is not None and p >= bot_top)
    if missing:
        out.append(Finding(FAIL, 'discord', f"{what}: no such role: {', '.join(missing)}."))
    if above:
        out.append(Finding(FAIL, 'discord',
                           f"{what}: the bot's role is not above {', '.join(above)}, so it "
                           f"cannot give or take {'it' if len(above) == 1 else 'them'}."))
    if roles and not missing and not above:
        out.append(Finding(OK, 'discord', f"{what}: the bot can manage all {len(roles)} role(s)."))
    return out


def members_intent(enabled: bool) -> Finding:
    if enabled:
        return Finding(OK, 'discord', "Members intent is on.")
    return Finding(FAIL, 'discord',
                   "Members intent is OFF: role changes are not seen, so rank-up announcements "
                   "and officer roles do nothing.")


# ---------------------------------------------------------------- database
def database(reversed_sessions: int, stale_open: int, cq_requirements: list[str]) -> list[Finding]:
    out = []
    if reversed_sessions:
        out.append(Finding(FAIL, 'database',
                           f"{reversed_sessions} session(s) end before they start. Every "
                           f"`tsrange(hop_on, hop_off)` in the bot raises on them -- /pilot status "
                           f"and AwardAutomation break. `/statfix run` repairs them."))
    else:
        out.append(Finding(OK, 'database', "No reversed sessions."))
    if stale_open:
        out.append(Finding(WARN, 'database',
                           f"{stale_open} session(s) open for over a day. The bot will close them "
                           f"at the MISSION's end time, crediting hours nobody flew; jsh_statfix "
                           f"closes them properly."))
    for name in cq_requirements:
        out.append(Finding(WARN, 'database',
                           f"logbook qualification `{name}` has requirements set, so logbook also "
                           f"grants it from a lifetime landing count, alongside jsh_cq."))
    return out


# ----------------------------------------------------------------- summary
def summary(findings: list[Finding]) -> str:
    fails = sum(f.level == FAIL for f in findings)
    warns = sum(f.level == WARN for f in findings)
    if not fails and not warns:
        return "All clear."
    return f"{fails} problem(s), {warns} warning(s)."
