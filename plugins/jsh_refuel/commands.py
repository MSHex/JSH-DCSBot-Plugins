import asyncio
import discord

from core import Plugin, Group, utils, Server
from discord import app_commands
from psycopg.rows import dict_row
from services.bot import DCSServerBot
from typing import Optional

from .listener import RefuelEventListener

GM_ROLES = ['DCS Admin', 'GameMaster']


def lua_str(value) -> str:
    s = str(value)
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
    return f'"{s}"'


class Refuel(Plugin[RefuelEventListener]):
    """
    Pays campaign credits for fuel taken from a tanker.

    The mission side only measures pounds; every policy decision -- the rate,
    the minimum worth paying, the per-sortie cap -- is made here from
    jsh_refuel.yaml, so changing the economy never needs a DCS restart.
    """

    def __init__(self, bot: DCSServerBot, eventlistener=None):
        super().__init__(bot, eventlistener)
        self._credits_log = None      # cached credits_log column names

    def enabled(self, server: Server) -> bool:
        """enabled: false in DEFAULT or in the server's instance section turns the plugin off there."""
        return bool(self.get_config(server).get('enabled', True))

    # ------------------------------------------------------------ the economy
    def rate(self, server: Server) -> tuple[int, int, int, int]:
        """(block_lbs, credits_per_block, minimum_lbs, max_lbs_per_sortie)."""
        config = self.get_config(server)
        block = max(1, int(config.get('block_lbs', 100)))
        per_block = int(config.get('credits_per_block', 5))
        minimum = max(0, int(config.get('minimum_lbs', 100)))
        cap = max(0, int(config.get('max_lbs_per_sortie', 0)))
        return block, per_block, minimum, cap

    # ------------------------------------------------------------ paying
    async def pay(self, server: Server, name: str, points: int, reason: str,
                  ucid: Optional[str] = None) -> None:
        announce = bool(self.get_config(server).get('announce_in_game', True))
        await server.send_to_dcs({
            "command": "jshRefuelRun",
            "lua": (f'if jsh_refuel then jsh_refuel.pay({lua_str(name)}, {int(points)}, '
                    f'{lua_str(reason)}, {"true" if announce else "false"}) end')
        })
        self.log.info(f"Refuel: paid {name} {points} on {server.name} ({reason})")
        if ucid:
            asyncio.create_task(self.write_remark(server, ucid, reason))

    async def credits_log_schema(self) -> Optional[dict]:
        """Finds the credits_log columns once, so a schema change is a log line, not a crash."""
        if self._credits_log is not None:
            return self._credits_log or None
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'credits_log'")
            cols = {r[0] for r in await cursor.fetchall()}
        if not cols:
            self.log.warning("Refuel: credits_log not found, remarks are disabled.")
            self._credits_log = {}
            return None

        def pick(*candidates: str) -> Optional[str]:
            for c in candidates:
                if c in cols:
                    return c
            return None

        schema = {
            'ucid': pick('player_ucid', 'ucid'),
            'remark': pick('remark', 'reason', 'comment'),
            'time': pick('time', 'event_time', 'timestamp', 'created_at', 'logged_at'),
            'event': pick('event'),
        }
        if not all(schema[k] for k in ('ucid', 'remark', 'time')):
            self.log.warning(f"Refuel: unexpected credits_log schema {cols}, remarks are disabled.")
            self._credits_log = {}
            return None
        self._credits_log = schema
        return schema

    async def write_remark(self, server: Server, ucid: str, reason: str) -> None:
        """
        Labels the credit entry the payment just created.

        Sets the event (default "refuel") and the remark on the newest unlabelled
        entry for this player, so the ledger says what the credits were for. The
        event name must stay distinct from every other writer -- see the
        'who writes credits' table in the repository README.
        """
        schema = await self.credits_log_schema()
        if not schema:
            return
        event = self.get_config(server).get('credits_log_event', 'refuel')
        await asyncio.sleep(1)      # let the credit system write its row first
        sets = [f"{schema['remark']} = %s"]
        params: list = [reason]
        if schema['event'] and event:
            sets.append(f"{schema['event']} = %s")
            params.append(event)
        params += [reason, ucid]
        async with self.apool.connection() as conn:
            async with conn.transaction():
                await conn.execute(f"""
                    UPDATE credits_log SET {', '.join(sets)}
                    WHERE ctid = (
                        SELECT ctid FROM credits_log
                        WHERE ({schema['remark']} IS NULL OR {schema['remark']} = ''
                               OR {schema['remark']} = %s)
                          AND {schema['ucid']} = %s
                          AND {schema['time']} > (now() AT TIME ZONE 'utc') - interval '2 minutes'
                        ORDER BY {schema['time']} DESC LIMIT 1
                    )
                """, params)

    async def record(self, server: Server, ucid: Optional[str], name: str, aircraft: Optional[str],
                     lbs: int, paid_lbs: int, points: int) -> None:
        """Writes the event row and the lifetime total. Unpaid transfers are kept too."""
        async with self.apool.connection() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO jsh_refuel_log
                        (server_name, player_ucid, player_name, aircraft, lbs, paid_lbs, points)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (server.name, ucid, name, aircraft, lbs, paid_lbs, points))
                if not ucid:
                    return
                await conn.execute("""
                    INSERT INTO jsh_refuel_totals
                        (player_ucid, refuellings, lbs, points, last_aircraft)
                    VALUES (%s, 1, %s, %s, %s)
                    ON CONFLICT (player_ucid) DO UPDATE
                    SET refuellings = jsh_refuel_totals.refuellings + 1,
                        lbs = jsh_refuel_totals.lbs + EXCLUDED.lbs,
                        points = jsh_refuel_totals.points + EXCLUDED.points,
                        last_aircraft = EXCLUDED.last_aircraft,
                        last_refuel = now() AT TIME ZONE 'utc'
                """, (ucid, lbs, points, aircraft))

    # ------------------------------------------------------------ commands
    refuel = Group(name="refuel", description="Air-to-air refuelling credits")

    @refuel.command(description='Fuel taken and credits earned from tankers')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def stats(self, interaction: discord.Interaction,
                    user: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        member = user or interaction.user
        ucid = await self.bot.get_ucid_by_member(member)
        if not ucid:
            await interaction.followup.send(
                f"{member.display_name} has no linked DCS account.", ephemeral=True)
            return
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT refuellings, lbs, points, last_aircraft, last_refuel
                    FROM jsh_refuel_totals WHERE player_ucid = %s
                """, (ucid,))
                row = await cursor.fetchone()
        if not row or not row['refuellings']:
            await interaction.followup.send(
                f"{member.display_name} has not taken fuel from a tanker yet.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Air-to-air refuelling - {member.display_name}",
                              color=discord.Color.blue())
        embed.add_field(name="Refuellings", value=f"{row['refuellings']:,}", inline=True)
        embed.add_field(name="Fuel taken", value=f"{row['lbs']:,} lbs", inline=True)
        embed.add_field(name="Credits", value=f"{row['points']:,}", inline=True)
        if row['last_aircraft']:
            embed.set_footer(text=f"Last: {row['last_aircraft']} on {row['last_refuel']:%d %b %Y}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @refuel.command(description='Who has taken the most fuel')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def top(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT p.name, t.refuellings, t.lbs, t.points
                    FROM jsh_refuel_totals t JOIN players p ON p.ucid = t.player_ucid
                    ORDER BY t.lbs DESC LIMIT 15
                """)
                rows = await cursor.fetchall()
        if not rows:
            await interaction.followup.send("Nobody has taken fuel from a tanker yet.",
                                            ephemeral=True)
            return
        embed = discord.Embed(title="Air-to-air refuelling - most fuel taken",
                              color=discord.Color.blue())
        embed.add_field(name="Pilot", value="\n".join(r['name'] for r in rows), inline=True)
        embed.add_field(name="Fuel", value="\n".join(f"{r['lbs']:,} lbs" for r in rows), inline=True)
        embed.add_field(name="Credits", value="\n".join(f"{r['points']:,}" for r in rows), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Refuel(bot, RefuelEventListener))
