"""
Bring rank roles down to what credits and hours still earn -- demotions only.

Why this exists
---------------
InactivityDecay deducts credits from inactive pilots every month, in its
configured campaign, and leaves Discord roles alone. CreditSystem only
re-checks a pilot's rank in process_achievements(), which runs when they
DISCONNECT from a server -- and an inactive pilot is not connecting. So a
pilot decayed from O-6 credits to E-4 credits keeps the Colonel role, and
every role that follows from it, until they next fly.

This applies the demotion CreditSystem would apply on that next disconnect,
without waiting for it.

Why it cannot fight CreditSystem
--------------------------------
  * Same inputs: the `credits` table, and flight time by the same formula as
    CreditSystem's get_flighttime() -- statistics joined to the campaign's
    servers, overlapping the campaign's dates. INPUTS below.
  * Same rule: rankstatus' get_rank_progress(), which takes the HIGHEST
    achievement met, as process_achievements() does, combined ones included.
    Playtime is in HOURS there, as CreditSystem divides by 3600.
  * Demotions only. Promotions stay CreditSystem's, on disconnect. So the
    two can only ever agree, or this does nothing.
  * Granted ranks are not on the earned ladder and are never touched.

Imports nothing from DCSServerBot or discord, so it can be tested alone.
"""
from typing import Iterable, Optional

#: Credits and flight time, in one campaign, for the Discord members asked
#: about. Every UCID linked to a member comes back; the caller keeps the
#: best, so an alt with no credits can never demote anyone.
#:
#: Returns nothing at all unless the campaign is RUNNING: CreditSystem does no
#: achievements without a running campaign, and neither may this.
#:
#: Flight time is CreditSystem's get_flighttime() verbatim in substance: the
#: campaign's servers, sessions overlapping the campaign's dates, open
#: sessions skipped by SUM. Seconds; the caller converts to hours.
#:
#: One deliberate difference: a REVERSED session (hop_off < hop_on, left by
#: the bot's close_all_statistics -- see CLAUDE.md) is skipped. CreditSystem's
#: tsrange() raises on one, so it never demotes that pilot at all; counting
#: it negative here would demote on hours the pilot never lost. GREATEST()
#: keeps tsrange() from raising whatever order the planner evaluates in.
INPUTS = """
    WITH c AS (
        SELECT id, start, stop
          FROM campaigns
         WHERE id = %(campaign_id)s
           AND (now() AT TIME ZONE 'utc')
               BETWEEN start AND COALESCE(stop, now() AT TIME ZONE 'utc')
    )
    SELECT p.discord_id,
           p.ucid,
           COALESCE(cr.points, 0) AS credits,
           COALESCE((
               SELECT ROUND(SUM(EXTRACT(EPOCH FROM (s.hop_off - s.hop_on))))
                 FROM statistics s
                 JOIN missions m ON m.id = s.mission_id
                 JOIN campaigns_servers cs
                   ON cs.campaign_id = c.id AND cs.server_name = m.server_name
                WHERE s.player_ucid = p.ucid
                  AND (s.hop_off IS NULL OR s.hop_off >= s.hop_on)
                  AND tsrange(s.hop_on, GREATEST(s.hop_on, s.hop_off))
                      && tsrange(c.start, c.stop)
           ), 0) AS playtime
      FROM c
      JOIN players p ON p.discord_id = ANY(%(discord_ids)s)
      LEFT JOIN credits cr ON cr.campaign_id = c.id AND cr.player_ucid = p.ucid
     WHERE p.discord_id > 0
"""


def _highest(ladder: list, keys: Iterable[str]) -> Optional[str]:
    held = set(keys)
    best = None
    for rank in ladder:
        if rank in held:
            best = rank
    return best


def demotion(earned: list, held: Iterable[str], deserved: Optional[str]) -> Optional[tuple]:
    """
    (roles to add, roles to remove) to bring a member DOWN to `deserved`, or
    None if nothing should change.

    `earned` is the earned ladder, lowest first; `held` the member's roles;
    `deserved` the rung their credits and hours earn now, or None below the
    first rung. All three in the same form -- commands.py uses role IDs.

    Only ever lowers. A member at or below what they deserve is left alone:
    raising them is CreditSystem's, on their next disconnect. When it does
    lower, it leaves exactly the deserved rung and no other earned rung, as
    process_achievements() does. Below the first rung, every earned rung
    goes, as CreditSystem would remove them all.
    """
    held = set(held)
    top = _highest(earned, held)
    if top is None:
        return None                       # no earned rank to lower
    if deserved is not None and earned.index(deserved) >= earned.index(top):
        return None                       # deserved at least what they hold
    add = [deserved] if deserved is not None and deserved not in held else []
    remove = [rank for rank in earned if rank in held and rank != deserved]
    return add, remove


def best_rank(earned: list, ranks: Iterable[Optional[str]]) -> Optional[str]:
    """The highest of several deserved ranks -- one per linked UCID."""
    return _highest(earned, [r for r in ranks if r is not None])
