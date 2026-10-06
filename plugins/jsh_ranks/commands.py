import asyncio
import discord
import os

from core import Plugin, Group, utils, Server, Status
from discord import app_commands
from psycopg.rows import dict_row
from services.bot import DCSServerBot
from typing import Optional

from .listener import RanksEventListener

RUNNING = [Status.RUNNING, Status.PAUSED]
GM_ROLES = ['DCS Admin', 'GameMaster']

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
    def achievements(self, server: Server) -> list[dict]:
        """The rank ladder, straight from creditsystem.yaml."""
        return self.get_config(server, plugin_name='creditsystem').get('achievements') or []

    @staticmethod
    def _sorted(achievements: list[dict]) -> list[dict]:
        # Same ordering rankstatus uses, so indexes line up with /pilot status.
        return sorted(
            achievements,
            key=lambda a: a['credits'] if 'credits' in a else a['playtime'])

    def rank_name(self, server: Server, credits: int, playtime: float) -> Optional[str]:
        """The pilot's current rank, or None below the first rung."""
        achievements = self.achievements(server)
        if not achievements:
            return None
        current, _ = get_rank_progress(achievements, credits, playtime)
        if not current:
            return None
        return current.get('role') or current.get('badge', {}).get('name')

    def level_for(self, server: Server, credits: int, playtime: float) -> int:
        """Maps the pilot's rank onto Foothold's 1..7 scale."""
        config = self.get_config(server)
        default = int(config.get('default_level', 1))
        name = self.rank_name(server, credits, playtime)
        if not name:
            return default
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

    async def levels_for_server(self, server: Server) -> dict[str, int]:
        """Foothold rank level for every player currently on the server."""
        players = [p for p in server.get_active_players() if getattr(p, 'ucid', None)]
        if not players:
            return {}
        stats = await self.campaign_stats(server, [p.ucid for p in players])
        levels = {}
        for player in players:
            row = stats.get(player.ucid) or {}
            credits = int(row.get('credits') or 0)
            # playtime is stored in seconds; the ladder is written in hours
            playtime = float(row.get('playtime') or 0) / 3600.0
            levels[player.name] = self.level_for(server, credits, playtime)
        return levels

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
        await server.send_to_dcs({'command': 'do_script', 'script': script})

    async def push(self, server: Server) -> int:
        """Sends the current levels to the mission. Returns how many were sent."""
        if not self.enabled(server) or server.status not in RUNNING:
            return 0
        levels = await self.levels_for_server(server)
        config = self.get_config(server)
        await server.send_to_dcs({
            'command': 'jshRanksRun',
            'lua': (f'if jsh_ranks then jsh_ranks.set({lua_value(levels)}, '
                    f'{lua_value(config.get("level_names") or {})}) end')
        })
        self.log.debug(f"JSH Ranks: pushed {len(levels)} rank(s) to {server.name}")
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
        levels = await self.levels_for_server(server)
        if not levels:
            await interaction.followup.send(f"Nobody is on {server.name}.", ephemeral=True)
            return
        lines = [f"{name} - level {level}" for name, level in sorted(levels.items())]
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


async def setup(bot: DCSServerBot):
    await bot.add_cog(Ranks(bot, RanksEventListener))
