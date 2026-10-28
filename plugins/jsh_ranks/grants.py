"""
The grants lookup, shared with other plugins.

This module imports nothing from DCSServerBot, discord or psycopg, and
nothing from any other plugin. That is the point: jsh_ranks/commands.py
already imports `get_rank_progress` from plugins.rankstatus.ranks, so
rankstatus importing jsh_ranks.commands would close a cycle. Both import
THIS instead, and nothing imports back.
"""
from typing import Any, Optional

#: What /pilot status puts in the Promotion Progress field for a granted rank.
#: `{status}` is the rank's own `status:` label, `{rank}` its name. Both are
#: overridable in jsh_ranks.yaml -- see status_line().
DEFAULT_STATUS_FORMAT = '🏆 {status} — outside the campaign ladder.'

#: Used for a granted rank that sets no `status:` of its own.
DEFAULT_STATUS = 'Granted rank'

#: One pilot's grant, matched on either identifier.
#:
#: players.discord_id is NOT NULL DEFAULT -1 in DCSServerBot, so an unlinked
#: pilot carries -1 rather than NULL; without the `> 0` guard, a grant row
#: whose discord_id was somehow -1 would match every unlinked pilot.
#:
#: A pilot may hold both a UCID grant and a Discord grant. The ORDER BY
#: prefers the UCID one, being the more specific of the two.
GRANT_FOR_UCID = """
    SELECT g.rank, g.reason, g.granted_by, g.granted_at
      FROM jsh_rank_grants g
      LEFT JOIN players pl ON pl.ucid = %s
     WHERE g.ucid = %s
        OR (g.discord_id IS NOT NULL AND g.discord_id > 0
            AND g.discord_id = pl.discord_id)
     ORDER BY (g.ucid IS NOT NULL) DESC
     LIMIT 1
"""

#: Cheap guard for a bot that has jsh_ranks' config but not its table yet,
#: or does not run jsh_ranks at all.
GRANTS_TABLE_EXISTS = "SELECT to_regclass('public.jsh_rank_grants') IS NOT NULL"


def resolve_spec(granted_ranks: dict, name: str) -> Optional[dict[str, Any]]:
    """
    The definition of a granted-only rank, or None if `name` is not one.

    Matched case-insensitively, because the name travels through a Discord
    command and back and a config key is easy to mistype.

    Three shapes are accepted:

        Founder: 16                       a bare number is the level, with the
                                          Discord role named after the rank
        Server Moderator:                 a mapping, any key optional
          role: Server Moderator
          level: 16
          badge:
            name: Server Moderator
            img: https://.../badge.png

    `badge` mirrors creditsystem.yaml's own shape, so a rank moved out of
    `achievements` into `granted_ranks` keeps its artwork by being pasted
    across unchanged.
    """
    if not name or not granted_ranks:
        return None
    for key, spec in granted_ranks.items():
        if str(key).casefold() != str(name).casefold():
            continue
        if isinstance(spec, dict):
            badge = spec.get('badge')
            if isinstance(badge, dict):
                # Default the badge name to the rank, so only `img` is needed.
                badge = {'name': badge.get('name', str(key)), 'img': badge.get('img')}
            else:
                badge = None
            return {'role': spec.get('role', key), 'level': spec.get('level'),
                    'badge': badge, 'status': spec.get('status')}
        return {'role': key, 'level': spec, 'badge': None, 'status': None}
    return None


def status_line(name: str, spec: Optional[dict],
                fmt: Optional[str] = None,
                default_status: Optional[str] = None) -> str:
    """
    The line /pilot status shows in place of promotion progress.

    `status:` on the rank is a short label -- Admin, Staff, Moderator -- and
    the format string wraps it, so the wording is changed in one place rather
    than on every rank.

    The format string comes from config, so a typo in it must not take down
    /pilot status: an unknown placeholder falls back to the label alone.
    """
    label = (spec or {}).get('status') or default_status or DEFAULT_STATUS
    template = fmt or DEFAULT_STATUS_FORMAT
    try:
        return template.format(status=label, rank=name)
    except (KeyError, IndexError, ValueError):
        return str(label)


def as_achievement(name: str, spec: Optional[dict]) -> dict[str, Any]:
    """
    A granted rank in the shape creditsystem.yaml writes achievements in, so
    rankstatus can render it through the paths it already has -- the same
    `role` lookup for the name, the same `badge.img` for the thumbnail -- with
    no second code path for granted ranks.

    Deliberately carries no `credits` or `playtime`: a granted rank is not on
    the ladder, and get_rank_progress must never see one.
    """
    spec = spec or {}
    achievement: dict[str, Any] = {'role': spec.get('role') or name}
    badge = spec.get('badge')
    if badge and badge.get('img'):
        achievement['badge'] = {'name': badge.get('name') or name, 'img': badge['img']}
    return achievement
