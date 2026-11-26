import discord

from core import Plugin, Group, utils, Server
from discord import app_commands
from psycopg.rows import dict_row
from services.bot import DCSServerBot
from typing import Optional

from . import rewards
from .listener import LogisticsEventListener


def lua_str(value) -> str:
    s = str(value)
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
    return f'"{s}"'


class Logistics(Plugin[LogisticsEventListener]):
    """
    Server credits for logistics pilots: each Foothold delivery they bank by
    landing, and every minute they fly a logistics aircraft. Both priced per
    instance in jsh_logistics.yaml.
    """

    # ---------------------------------------------------------------- config
    def enabled(self, server: Server) -> bool:
        return bool(self.get_config(server).get('enabled', True))

    def delivery_table(self, server: Server) -> dict:
        """
        The instance's prices over the defaults: an instance lists only what it
        changes. A price of 0 switches that delivery off.
        """
        table = dict(rewards.DEFAULT_DELIVERIES)
        table.update({str(k): int(v) for k, v in (self.get_config(server).get('deliveries') or {}).items()})
        return table

    def airtime_config(self, server: Server) -> dict:
        cfg = self.get_config(server).get('airtime') or {}
        return {'enabled': bool(cfg.get('enabled', True)),
                'minutes': float(cfg.get('minutes', 1)),
                'credits': int(cfg.get('credits', 1)) if cfg.get('enabled', True) else 0,
                'aircraft': list(cfg.get('aircraft') or rewards.DEFAULT_AIRCRAFT),
                'sample_seconds': max(2, int(cfg.get('sample_seconds', 10)))}

    async def configure(self, server: Server) -> None:
        """Tells the mission which aircraft count and how often to sample."""
        cfg = self.airtime_config(server)
        types = cfg['aircraft'] if cfg['enabled'] and cfg['credits'] > 0 else []
        table = '{' + ', '.join(lua_str(t) for t in types) + '}'
        await server.send_to_dcs({
            "command": "jshLogisticsRun",
            "lua": (f'if jsh_logistics then jsh_logistics.configure({{ types = {table}, '
                    f'interval = {cfg["sample_seconds"]} }}) end')})

    # --------------------------------------------------------------- payment
    async def pay(self, server: Server, name: str, points: int, reason: str) -> None:
        if points <= 0:
            return
        announce = bool(self.get_config(server).get('announce_in_game', True))
        await server.send_to_dcs({
            "command": "jshLogisticsRun",
            "lua": (f'if jsh_logistics then jsh_logistics.pay({lua_str(name)}, {int(points)}, '
                    f'{lua_str(reason)}, {"true" if announce else "false"}) end')})
        self.log.info(f"JSH Logistics: paid {name} {points} on {server.name} ({reason})")

    async def record(self, server: Server, ucid: Optional[str], name: str, kind: str,
                     what: str, amount: int, points: int) -> None:
        """One row per delivery type per landing, or per airtime flush."""
        try:
            async with self.apool.connection() as conn:
                await conn.execute("""
                    INSERT INTO jsh_logistics_events
                        (server_name, player_ucid, player_name, kind, what, amount, points)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (server.name, ucid, name, kind, what, amount, points))
        except Exception as ex:
            self.log.warning(f"JSH Logistics: could not record {kind} for {name}: {ex}")

    # -------------------------------------------------------------- commands
    logistics = Group(name="logistics", description="Logistics rewards")

    @logistics.command(name='status', description='Your logistics deliveries and airtime')
    @app_commands.guild_only()
    @utils.app_has_role('DCS')
    @app_commands.describe(member='Someone else (default: you)',
                           days='How far back to count (default: 30)')
    async def status(self, interaction: discord.Interaction,
                     member: Optional[discord.Member] = None,
                     days: app_commands.Range[int, 1, 3650] = 30):
        await interaction.response.defer(ephemeral=True)
        member = member or interaction.user
        ucid = await self.bot.get_ucid_by_member(member)
        if not ucid:
            await interaction.followup.send(f"{member.display_name} has no linked DCS account.",
                                            ephemeral=True)
            return
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT kind, what, SUM(amount) AS amount, SUM(points) AS points
                      FROM jsh_logistics_events
                     WHERE player_ucid = %s
                       AND time > (now() AT TIME ZONE 'utc') - make_interval(days => %s)
                     GROUP BY kind, what ORDER BY kind, SUM(points) DESC
                """, (ucid, days))
                rows = await cursor.fetchall()
        embed = discord.Embed(title=f"Logistics: {member.display_name}",
                              description=f"Last {days} days", colour=discord.Colour.blue())
        deliveries = [r for r in rows if r['kind'] == 'delivery']
        flights = [r for r in rows if r['kind'] == 'airtime']
        embed.add_field(name="Deliveries", inline=False, value='\n'.join(
            f"{r['what']}: {r['amount']} — {r['points']:,} credits" for r in deliveries)[:1024]
            or "None yet.")
        embed.add_field(name="Airtime", inline=False, value='\n'.join(
            f"{r['what'] or 'logistics'}: {r['amount'] / 3600:.1f} h — {r['points']:,} credits"
            for r in flights)[:1024] or "None yet.")
        total = sum(int(r['points'] or 0) for r in rows)
        embed.set_footer(text=f"{total:,} credits in total. Deliveries pay when Foothold banks "
                              f"them on landing; airtime pays on landing too.")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Logistics(bot, LogisticsEventListener))
