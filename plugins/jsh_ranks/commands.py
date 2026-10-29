import asyncio
import discord
import json
import os

from core import Plugin, Group, utils, Server, Status
from discord import app_commands
from psycopg.rows import dict_row
from services.bot import DCSServerBot
from typing import Optional

from . import grants, slots
from .listener import RanksEventListener

#: One UDP datagram. DCS reads with LuaSocket's 8192-byte default and
#: truncates anything larger without a word in either log -- so an oversized
#: script registers nothing and reports no error. jsh_credits 1.6 shipped over
#: this line and the symptom was simply that a feature stopped working.
DATAGRAM_LIMIT = 8192
DATAGRAM_MARGIN = 400


def minify_lua(script: str) -> str:
    """
    Drop whole-line comments and blank lines before sending.

    Only lines starting with `--` go; trailing comments stay, since a `--`
    inside a string literal would otherwise swallow the rest of the line.
    """
    return '\n'.join(line for line in script.splitlines()
                      if line.strip() and not line.lstrip().startswith('--'))


RUNNING = [Status.RUNNING, Status.PAUSED]
GM_ROLES = ['DCS Admin', 'GameMaster']


async def rank_autocomplete(interaction: discord.Interaction,
                            current: str) -> list[app_commands.Choice[str]]:
    """
    The rank ladder, lowest rung first, so the rank that cannot be earned is
    picked from a list rather than typed from memory.
    """
    if not await interaction.command._check_can_run(interaction):
        return []
    try:
        plugin = next((cog for cog in interaction.client.cogs.values()
                       if getattr(cog, 'plugin_name', None) == 'jsh_ranks'), None)
        if not plugin:
            return []
        needle = current.casefold()
        return [app_commands.Choice(name=name, value=name)
                for name in plugin.all_rank_names() if needle in name.casefold()][:25]
    except Exception:
        return []

# The ladder lives in rankstatus, so /pilot status and the in-mission rank can
# never disagree. A second implementation here would drift the moment anyone
# edited creditsystem.yaml.
from plugins.rankstatus.ranks import get_rank_progress  # noqa: E402


def lua_str(value) -> str:
    s = str(value)
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
    return f'"{s}"'


def lua_value(value) -> str:
    if value is None:
        return 'nil'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return '{' + ', '.join(f'[{lua_str(k)}] = {lua_value(v)}' for k, v in value.items()) + '}'
    if isinstance(value, (list, tuple)):
        return '{' + ', '.join(lua_value(v) for v in value) + '}'
    return lua_str(value)


class Ranks(Plugin[RanksEventListener]):
    """
    Makes Foothold use the Discord rank ladder from creditsystem.yaml instead of
    its own in-mission rank credits.

    Foothold funnels every rank gate through BattleCommander:getPlayerRank, so
    the mission-side script overrides that one function and the whole mission --
    shop, tankers, carrier navigation, menus -- follows.
    """

    def __init__(self, bot: DCSServerBot, eventlistener=None):
        super().__init__(bot, eventlistener)

    def enabled(self, server: Server) -> bool:
        return bool(self.get_config(server).get('enabled', True))

    # ------------------------------------------------------------ the ladder
    def achievements(self, server: Optional[Server] = None) -> list[dict]:
        """
        The rank ladder, straight from creditsystem.yaml.

        With no server this is the DEFAULT section, which is what the grant
        commands use -- a grant is global, so it cannot be read against one
        server's overrides.
        """
        return self.get_config(server, plugin_name='creditsystem').get('achievements') or []

    @staticmethod
    def _name_of(achievement: dict) -> Optional[str]:
        return achievement.get('role') or achievement.get('badge', {}).get('name')

    # --------------------------------------------------- granted-only ranks
    def granted_ranks(self, server: Optional[Server] = None) -> dict:
        """
        Ranks that exist ONLY to be granted, from jsh_ranks.yaml.

        These are the ranks commented out of creditsystem.yaml's achievements
        -- moderator and admin ranks that should not fall out of credits or
        flight hours. Being absent from `achievements` is what makes them safe
        for this plugin to own: CreditSystem's process_achievements iterates
        `config['achievements']` and calls manage_role only with an
        achievement's own `role`, so a role that is not on that list is never
        added or removed by it. Nothing to fight over.
        """
        return self.get_config(server).get('granted_ranks') or {}

    def granted_spec(self, name: str, server: Optional[Server] = None) -> Optional[dict]:
        """
        The definition of a granted-only rank, or None if it is not one.

        The resolution itself lives in grants.py, which rankstatus also reads
        so that `/pilot status` shows the granted rank and its badge. Keeping
        one implementation is what stops the two drifting.
        """
        return grants.resolve_spec(self.granted_ranks(server), name)

    def all_rank_names(self) -> list[str]:
        """
        Every rank that can be granted: the granted-only ranks first, then the
        earned ladder, lowest rung first.

        Granted-only ranks lead because they are what the command exists for.
        """
        names: list[str] = [str(key) for key in self.granted_ranks()]
        ladders = [self.achievements()]
        for server in self.bot.servers.values():
            try:
                ladders.append(self.achievements(server))
            except Exception:  # a server mid-registration has no config yet
                continue
        for ladder in ladders:
            for achievement in self._sorted(ladder):
                name = self._name_of(achievement)
                if name and name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _sorted(achievements: list[dict]) -> list[dict]:
        # Same ordering rankstatus uses, so indexes line up with /pilot status.
        return sorted(
            achievements,
            key=lambda a: a['credits'] if 'credits' in a else a['playtime'])

    def rank_name(self, server: Server, credits: int, playtime: float) -> Optional[str]:
        """The pilot's current EARNED rank, or None below the first rung."""
        achievements = self.achievements(server)
        if not achievements:
            return None
        current, _ = get_rank_progress(achievements, credits, playtime)
        if not current:
            return None
        return self._name_of(current)

    def level_for(self, server: Server, credits: int, playtime: float,
                  granted: Optional[str] = None) -> int:
        """
        Maps the pilot's rank onto Foothold's rank numbers.

        A grant wins outright rather than being compared against the earned
        rank, so the same mechanism pins a rank down as well as up -- useful,
        but it means a grant also FREEZES a pilot's progression until it is
        revoked. /ranks show marks a granted rank for exactly that reason.

        A granted-only rank carries its own level, so it needs no rank_map
        entry -- it is not on the ladder and rank_map is keyed on ladder names.
        """
        config = self.get_config(server)
        default = int(config.get('default_level', 1))
        name = granted or self.rank_name(server, credits, playtime)
        if not name:
            return default
        if granted:
            spec = self.granted_spec(granted, server)
            if spec and spec.get('level') is not None:
                try:
                    return int(spec['level'])
                except (TypeError, ValueError):
                    self.log.warning(
                        f"JSH Ranks: granted_ranks[{granted!r}].level is not a "
                        f"number, using rank_map instead")
        mapping = config.get('rank_map') or {}
        try:
            return int(mapping[name])
        except (KeyError, TypeError, ValueError):
            # An unmapped rank is a config gap, not a reason to lock someone out
            # of the shop, so fall back and say so once.
            self.log.warning(f"JSH Ranks: no rank_map entry for {name!r}, using level {default}")
            return default

    # ------------------------------------------------------------ pilot data
    async def campaign_stats(self, server: Server, ucids: list[str]) -> dict[str, dict]:
        """
        Credits and playtime for the campaign running on this server.

        Same shape as rankstatus' own query, narrowed to one server's campaign
        and to the pilots asked for.
        """
        if not ucids:
            return {}
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT p.ucid,
                           COALESCE(cr.points, 0) AS credits,
                           COALESCE(stats.playtime, 0) AS playtime
                    FROM (SELECT unnest(%s::TEXT[]) AS ucid) p
                    CROSS JOIN LATERAL (
                        SELECT c.id, c.start, c.stop
                        FROM campaigns c
                        JOIN campaigns_servers cs ON cs.campaign_id = c.id
                        WHERE cs.server_name = %s
                          AND (now() AT TIME ZONE 'utc')
                              BETWEEN c.start AND COALESCE(c.stop, now() AT TIME ZONE 'utc')
                        ORDER BY c.start DESC
                        LIMIT 1
                    ) c
                    LEFT JOIN credits cr
                           ON cr.campaign_id = c.id AND cr.player_ucid = p.ucid
                    LEFT JOIN LATERAL (
                        SELECT ROUND(SUM(EXTRACT(EPOCH FROM (s.hop_off - s.hop_on)))) AS playtime
                        FROM statistics s
                        JOIN missions m ON m.id = s.mission_id
                        JOIN campaigns_servers cs2
                          ON cs2.campaign_id = c.id AND cs2.server_name = m.server_name
                        WHERE s.player_ucid = p.ucid
                          AND tsrange(s.hop_on, s.hop_off) && tsrange(c.start, c.stop)
                    ) stats ON TRUE
                """, (ucids, server.name))
                rows = await cursor.fetchall()
        return {r['ucid']: r for r in rows}

    async def grants_for(self, ucids: list[str]) -> dict[str, dict]:
        """
        Manually granted ranks for these pilots, by UCID.

        A grant matches on its UCID or on the Discord account the UCID is
        linked to, so one written against a Discord ID follows the person onto
        a second DCS install. `players.discord_id` is NOT NULL DEFAULT -1 in
        DCSServerBot, so an unlinked pilot carries -1 rather than NULL and the
        query has to exclude it explicitly -- without that, every unlinked
        pilot would match every grant whose discord_id was somehow -1.

        DISTINCT ON keeps one row per pilot, preferring the UCID grant when
        both forms exist, because it is the more specific of the two.
        """
        if not ucids:
            return {}
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT DISTINCT ON (p.ucid)
                           p.ucid, g.rank, g.reason, g.granted_by, g.granted_at
                    FROM (SELECT unnest(%s::TEXT[]) AS ucid) p
                    LEFT JOIN players pl ON pl.ucid = p.ucid
                    JOIN jsh_rank_grants g
                      ON g.ucid = p.ucid
                      OR (g.discord_id IS NOT NULL AND g.discord_id > 0
                          AND g.discord_id = pl.discord_id)
                    ORDER BY p.ucid, (g.ucid = p.ucid) DESC NULLS LAST
                """, (ucids,))
                rows = await cursor.fetchall()
        return {r['ucid']: r for r in rows}

    async def details_for_server(self, server: Server) -> list[dict]:
        """
        Everything /ranks show needs, per player: the rank, where it came
        from, and the Foothold level it maps to.
        """
        players = [p for p in server.get_active_players() if getattr(p, 'ucid', None)]
        if not players:
            return []
        ucids = [p.ucid for p in players]
        stats = await self.campaign_stats(server, ucids)
        grants = await self.grants_for(ucids)
        details = []
        for player in players:
            row = stats.get(player.ucid) or {}
            credits = int(row.get('credits') or 0)
            # playtime is stored in seconds; the ladder is written in hours
            playtime = float(row.get('playtime') or 0) / 3600.0
            grant = grants.get(player.ucid)
            granted = grant['rank'] if grant else None
            details.append({
                'name': player.name,
                'ucid': player.ucid,
                # Carried so commander_levels() can tell a map-only slot from a
                # cockpit; both are read because DCSServerBot fills them
                # differently depending on how the slot was entered.
                'slot': getattr(player, 'slot', None),
                'unit_type': getattr(player, 'unit_type', None),
                'side': getattr(player, 'side', None),
                'rank': granted or self.rank_name(server, credits, playtime),
                'granted': bool(grant),
                'reason': grant.get('reason') if grant else None,
                'level': self.level_for(server, credits, playtime, granted=granted),
            })
        return details

    async def levels_for_server(self, server: Server) -> dict[str, int]:
        """Foothold rank level for every player currently on the server."""
        return {d['name']: d['level'] for d in await self.details_for_server(server)}

    # ------------------------------------------------------------ mission side
    async def load_lua(self, server: Server) -> None:
        if not self.enabled(server):
            return
        path = os.path.join(os.path.dirname(__file__), 'lua', 'mission.lua')
        try:
            with open(path, mode='r', encoding='utf-8') as f:
                script = f.read()
        except OSError as ex:
            self.log.error(f"JSH Ranks: could not read {path}: {ex}")
            return
        script = minify_lua(script)
        message = {'command': 'do_script', 'script': script}
        size = len(json.dumps(message).encode()) + DATAGRAM_MARGIN
        if size > DATAGRAM_LIMIT:
            self.log.error(
                f"JSH Ranks: mission.lua is too large to inject into {server.name} "
                f"({size} bytes with headroom, limit {DATAGRAM_LIMIT}). DCS would "
                f"truncate it silently and the rank hook would never register. Not sending.")
            return
        await server.send_to_dcs(message)
        self.log.debug(f"JSH Ranks: mission hook injected into {server.name} ({size} bytes)")

    async def push(self, server: Server) -> int:
        """Sends the current levels to the mission. Returns how many were sent."""
        if not self.enabled(server) or server.status not in RUNNING:
            return 0
        details = await self.details_for_server(server)
        levels = {d['name']: d['level'] for d in details}
        config = self.get_config(server)
        # The level a `cbuy:` from a commander slot is authorised against.
        # Mission-side Lua cannot identify such a player at all, so this is the
        # only way the mission can know what rank is commanding.
        commanders = slots.commander_levels(details)
        await server.send_to_dcs({
            'command': 'jshRanksRun',
            'lua': (f'if jsh_ranks then jsh_ranks.set({lua_value(levels)}, '
                    f'{lua_value(config.get("level_names") or {})}) '
                    f'if jsh_ranks.setCommanders then '
                    f'jsh_ranks.setCommanders({lua_value(commanders)}) end end')
        })
        self.log.debug(f"JSH Ranks: pushed {len(levels)} rank(s) to {server.name}"
                       + (f", commanders {commanders}" if commanders else ""))
        return len(levels)

    # ------------------------------------------------------------ commands
    ranks = Group(name="ranks", description="Discord ranks inside the mission")

    @ranks.command(description='Show the rank level each player currently has in the mission')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def show(self, interaction: discord.Interaction,
                   server: app_commands.Transform[Server, utils.ServerTransformer(status=RUNNING)]):
        await interaction.response.defer(ephemeral=True)
        if not self.enabled(server):
            await interaction.followup.send(
                f"JSH Ranks is disabled on {server.name}.", ephemeral=True)
            return
        details = await self.details_for_server(server)
        if not details:
            await interaction.followup.send(f"Nobody is on {server.name}.", ephemeral=True)
            return
        lines = []
        for entry in sorted(details, key=lambda d: d['name']):
            rank = entry['rank'] or 'unranked'
            mark = ' **(granted)**' if entry['granted'] else ''
            lines.append(f"{entry['name']} - level {entry['level']} - {rank}{mark}")
        await interaction.followup.send(
            f"**{server.name}**\n" + "\n".join(lines), ephemeral=True)

    @ranks.command(description='Re-send every player\'s rank to the mission now')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def refresh(self, interaction: discord.Interaction,
                      server: app_commands.Transform[Server, utils.ServerTransformer(status=RUNNING)]):
        await interaction.response.defer(ephemeral=True)
        if not self.enabled(server):
            await interaction.followup.send(
                f"JSH Ranks is disabled on {server.name}.", ephemeral=True)
            return
        await self.load_lua(server)
        await asyncio.sleep(1)
        sent = await self.push(server)
        await interaction.followup.send(
            f"Sent {sent} rank(s) to {server.name}.", ephemeral=True)

    # ------------------------------------------------------- manual grants
    async def _subject(self, who) -> tuple[Optional[str], Optional[int], str]:
        """
        Turn whatever the transformer gave us into (ucid, discord_id, label).

        Both identifiers are stored when both are known, so the grant survives
        a Discord account being unlinked AND follows the person onto a second
        DCS install. UserTransformer hands back a Member whenever the UCID it
        was given is linked, so the Member branch is the common one.
        """
        if isinstance(who, discord.Member):
            ucid = await self.bot.get_ucid_by_member(who)
            return ucid, who.id, who.display_name
        ucid = str(who)
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                'SELECT name, discord_id FROM players WHERE ucid = %s', (ucid,))
            row = await cursor.fetchone()
        name = row[0] if row else ucid
        discord_id = row[1] if row and row[1] and row[1] > 0 else None
        return ucid, discord_id, name

    async def _member_for(self, ucid: Optional[str],
                          discord_id: Optional[int]) -> Optional[discord.Member]:
        """The Discord member behind a grant, by either identifier."""
        if discord_id and self.bot.guilds:
            member = self.bot.guilds[0].get_member(int(discord_id))
            if member:
                return member
        if ucid:
            return await self.bot.get_member_by_ucid(ucid)
        return None

    async def apply_role(self, member: Optional[discord.Member], rank: str,
                         action: str) -> Optional[str]:
        """
        Add or remove the Discord role for a GRANTED-ONLY rank.

        Returns a message to show the admin, or None when all is well.

        A rank that is on the earned ladder is deliberately left alone: its
        role belongs to CreditSystem, which recomputes it from credits and
        playtime and would strip anything we added on its next pass. Touching
        it would produce a role that flickers. Only ranks absent from
        `achievements` -- the ones defined under granted_ranks -- are ours.
        """
        spec = self.granted_spec(rank)
        if not spec:
            return None
        role_name = spec.get('role')
        if not role_name:
            return None
        if not member:
            return (f"No Discord account is linked, so the **{role_name}** role was not "
                    f"{'assigned' if action == 'add' else 'removed'}. The rank still "
                    f"applies in the mission. Run `/ranks sync` once they link.")
        role = self.bot.get_role(role_name)
        if not role:
            return (f"The rank is recorded, but there is no Discord role called "
                    f"**{role_name}**. Create it, or correct `granted_ranks` in "
                    f"jsh_ranks.yaml, then run `/ranks sync`.")
        try:
            if action == 'add' and role not in member.roles:
                await member.add_roles(role, reason='JSH Ranks: granted rank')
            elif action == 'remove' and role in member.roles:
                await member.remove_roles(role, reason='JSH Ranks: grant revoked')
        except discord.Forbidden:
            return (f"The rank is recorded, but Discord would not let me change "
                    f"**{role.name}**. The bot needs *Manage Roles*, and its own role "
                    f"must sit **above** {role.name} in Server Settings -> Roles.")
        except discord.HTTPException as ex:
            return f"The rank is recorded, but Discord refused the role change: {ex}"
        return None

    @ranks.command(description='Assign a rank by hand, including one that cannot be earned')
    @app_commands.guild_only()
    @app_commands.autocomplete(rank=rank_autocomplete)
    @app_commands.describe(user='A Discord member or a DCS player',
                           rank='A granted-only rank, or a rung of the earned ladder',
                           reason='Recorded with the grant, and shown by /ranks grants')
    @utils.app_has_roles(GM_ROLES)
    async def grant(self, interaction: discord.Interaction,
                    user: app_commands.Transform[
                        discord.Member | str, utils.UserTransformer()],
                    rank: str, reason: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        known = self.all_rank_names()
        if known and rank not in known:
            await interaction.followup.send(
                f"{rank!r} is neither a granted-only rank nor a rung of the ladder. "
                f"An unknown name would quietly fall back to the default level instead "
                f"of granting anything.\n\nPick one of: {', '.join(known)}\n\n"
                f"To add a moderator or admin rank, define it under `granted_ranks` in "
                f"jsh_ranks.yaml.", ephemeral=True)
            return
        spec = self.granted_spec(rank)
        ucid, discord_id, label = await self._subject(user)
        if not ucid and not discord_id:
            await interaction.followup.send(
                "I could not work out who that is -- no UCID and no Discord account.",
                ephemeral=True)
            return

        async with self.apool.connection() as conn:
            async with conn.transaction():
                # Replaces any earlier grant for either identifier. Two unique
                # partial indexes mean ON CONFLICT could only cover one of them.
                await conn.execute("""
                    DELETE FROM jsh_rank_grants
                     WHERE (ucid IS NOT NULL AND ucid = %s)
                        OR (discord_id IS NOT NULL AND discord_id = %s)
                """, (ucid, discord_id))
                await conn.execute("""
                    INSERT INTO jsh_rank_grants (ucid, discord_id, rank, reason, granted_by)
                    VALUES (%s, %s, %s, %s, %s)
                """, (ucid, discord_id, rank, reason, str(interaction.user)))

        self.log.info(f"JSH Ranks: {interaction.user} granted {rank!r} to {label} "
                      f"(ucid={ucid}, discord_id={discord_id})")

        held_by = [part for part in
                   (f"UCID `{ucid}`" if ucid else None,
                    f"Discord <@{discord_id}>" if discord_id else None) if part]

        notes = []
        # The Discord role, for a granted-only rank. A ladder rank's role is
        # CreditSystem's and apply_role leaves it alone; say so, because being
        # given the rank without the role would otherwise look broken.
        member = await self._member_for(ucid, discord_id)
        problem = await self.apply_role(member, rank, 'add')
        if problem:
            notes.append(f"**{problem}**")
        elif spec and spec.get('role'):
            notes.append(f"Assigned the **{spec['role']}** Discord role.")
        elif not spec:
            notes.append(
                f"{rank} is a rung of the earned ladder, so its Discord role stays with "
                f"CreditSystem -- only the in-mission rank is pinned. A moderator rank "
                f"should be defined under `granted_ranks` instead.")

        # A granted-only rank carries its own level, so rank_map is irrelevant
        # to it; only warn for a ladder rank that has no mapping.
        if not (spec and spec.get('level') is not None):
            unmapped = []
            for target in self.bot.servers.values():
                try:
                    if self.enabled(target) and rank not in (self.get_config(target).get('rank_map') or {}):
                        unmapped.append(target.name)
                except Exception:  # a server with no instance attached yet
                    continue
            if unmapped:
                notes.append(
                    f"**Heads up:** `rank_map` has no entry for {rank!r} on "
                    f"{', '.join(unmapped)}, so the mission will fall back to the "
                    f"default level there. Give it a `level:` under `granted_ranks`, "
                    f"or a `rank_map` line.")

        body = '\n\n'.join(notes)
        await interaction.followup.send(
            f"Granted **{rank}** to **{label}**, held by {' and '.join(held_by)}."
            f"{chr(10) + chr(10) + body if body else ''}"
            f"\n\nThe in-mission rank applies on their next slot change, or run "
            f"`/ranks refresh` now.", ephemeral=True)

    @ranks.command(description='Remove a manually assigned rank')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def revoke(self, interaction: discord.Interaction,
                     user: app_commands.Transform[
                         discord.Member | str, utils.UserTransformer()]):
        await interaction.response.defer(ephemeral=True)
        ucid, discord_id, label = await self._subject(user)
        # Read the rank before deleting: the role to remove is named by it.
        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                DELETE FROM jsh_rank_grants
                 WHERE (ucid IS NOT NULL AND ucid = %s)
                    OR (discord_id IS NOT NULL AND discord_id = %s)
             RETURNING rank
            """, (ucid, discord_id))
            ranks_removed = [row[0] for row in await cursor.fetchall()]
        if not ranks_removed:
            await interaction.followup.send(
                f"**{label}** has no granted rank.", ephemeral=True)
            return

        notes = []
        member = await self._member_for(ucid, discord_id)
        for rank in ranks_removed:
            spec = self.granted_spec(rank)
            problem = await self.apply_role(member, rank, 'remove')
            if problem:
                notes.append(f"**{problem}**")
            elif spec and spec.get('role'):
                notes.append(f"Removed the **{spec['role']}** Discord role.")

        self.log.info(f"JSH Ranks: {interaction.user} revoked "
                      f"{', '.join(ranks_removed)} from {label}")
        body = '\n\n'.join(notes)
        await interaction.followup.send(
            f"Removed **{', '.join(ranks_removed)}** from **{label}**."
            f"{chr(10) + chr(10) + body if body else ''}"
            f"\n\nThey are back on what they have earned from their next slot change, "
            f"or run `/ranks refresh` now.", ephemeral=True)

    @ranks.command(name='sync', description='Re-apply the Discord role for every granted rank')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def sync(self, interaction: discord.Interaction):
        """
        Reconciles Discord roles with the grants table.

        Worth having because roles drift in ways a grant cannot see: someone
        leaves and rejoins the server and loses every role, a role is deleted
        and recreated, a grant was written by Discord ID before that person had
        linked, or the bot lacked permission at the time and the write went
        through without the role.
        """
        await interaction.response.defer(ephemeral=True)
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    'SELECT ucid, discord_id, rank FROM jsh_rank_grants ORDER BY rank')
                rows = await cursor.fetchall()
        if not rows:
            await interaction.followup.send("No ranks have been granted.", ephemeral=True)
            return

        applied = ladder = 0
        problems = []
        for row in rows:
            if not self.granted_spec(row['rank']):
                ladder += 1          # CreditSystem's role; not ours to touch
                continue
            member = await self._member_for(row['ucid'], row['discord_id'])
            problem = await self.apply_role(member, row['rank'], 'add')
            if problem:
                who = member.display_name if member else (row['ucid'] or row['discord_id'])
                problems.append(f"- {who}: {problem}")
            else:
                applied += 1
        lines = [f"Checked {len(rows)} grant(s): {applied} Discord role(s) in place."]
        if ladder:
            lines.append(f"{ladder} are ladder ranks, whose roles belong to CreditSystem "
                         f"and were left alone.")
        if problems:
            lines.append("\n**Could not apply:**\n" + "\n".join(problems[:10]))
        await interaction.followup.send("\n".join(lines)[:1990], ephemeral=True)

    @ranks.command(name='grants', description='List every manually assigned rank')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def grants(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT g.rank, g.reason, g.granted_by, g.granted_at,
                           g.ucid, g.discord_id, p.name
                      FROM jsh_rank_grants g
                      LEFT JOIN players p ON p.ucid = g.ucid
                     ORDER BY g.granted_at DESC
                """)
                rows = await cursor.fetchall()
        if not rows:
            await interaction.followup.send("No ranks have been granted.", ephemeral=True)
            return
        lines = []
        for row in rows:
            who = row['name'] or (f"<@{row['discord_id']}>" if row['discord_id']
                                  else f"`{row['ucid']}`")
            when = row['granted_at'].strftime('%Y-%m-%d')
            reason = f" - {row['reason']}" if row['reason'] else ''
            lines.append(f"**{row['rank']}** - {who} "
                         f"(by {row['granted_by']}, {when}){reason}")
        text = f"**Granted ranks ({len(rows)})**\n" + "\n".join(lines)
        # Discord rejects a message body over 2000 characters outright.
        if len(text) > 1990:
            text = text[:1900].rsplit('\n', 1)[0] + f"\n... and more; {len(rows)} in total."
        await interaction.followup.send(text, ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Ranks(bot, RanksEventListener))
