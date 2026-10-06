import asyncio
import discord

from core import Plugin, Group, utils, Server
from discord import app_commands
from services.bot import DCSServerBot
from typing import Optional

from .listener import CsarEventListener

GM_ROLES = ['DCS Admin', 'GameMaster']
STATUSES = ['Healthy', 'Wounded', 'Critical', 'RedPilot', 'Unknown']


def lua_str(value) -> str:
    s = str(value)
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
    return f'"{s}"'


class Csar(Plugin[CsarEventListener]):

    def __init__(self, bot: DCSServerBot, eventlistener=None):
        super().__init__(bot, eventlistener)
        self._logbook = None          # cached logbook award column names
        self._credits_log = None      # cached credits_log column names
        self._awards_config: list = []  # filled per server in check_awards_for()

    async def check_awards_for(self, server: Server, ucid: str) -> list[str]:
        self._awards_config = self.get_config(server).get('awards', [])
        return await self._check_awards(ucid)

    def enabled(self, server: Server) -> bool:
        """enabled: false in DEFAULT or in the server's instance section turns the plugin off there."""
        return bool(self.get_config(server).get('enabled', True))

    async def pay(self, server: Server, name: str, points: int, reason: str,
                  ucid: Optional[str] = None) -> None:
        announce = bool(self.get_config(server).get('announce_in_game', True))
        await server.send_to_dcs({
            "command": "jshCsarRun",
            "lua": (f'if jsh_csar then jsh_csar.pay({lua_str(name)}, {int(points)}, '
                    f'{lua_str(reason)}, {"true" if announce else "false"}) end')
        })
        self.log.info(f"CSAR: paid {name} {points} on {server.name} ({reason})")
        if ucid:
            asyncio.create_task(self.write_remark(server, ucid, reason))

    def is_dynamic(self, server: Server) -> bool:
        """dynamic_campaign: true marks a server whose rescues also feed jsh_csar_dynamic."""
        return bool(self.get_config(server).get('dynamic_campaign', False))

    async def credits_log_schema(self) -> Optional[dict]:
        """Finds the credits_log columns once, so a schema change is a log line, not a crash."""
        if self._credits_log is not None:
            return self._credits_log or None
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'credits_log'")
            cols = {r[0] for r in await cursor.fetchall()}
        if not cols:
            self.log.warning("CSAR: credits_log not found, remarks are disabled.")
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
        if not schema['event']:
            self.log.warning("CSAR: credits_log has no event column, only the remark is written.")
            schema['event'] = None
        if not all(schema[k] for k in ('ucid', 'remark', 'time')):
            self.log.warning(f"CSAR: unexpected credits_log schema {cols}, remarks are disabled.")
            self._credits_log = {}
            return None
        self._credits_log = schema
        return schema

    async def write_remark(self, server: Server, ucid: str, reason: str) -> None:
        """Labels the credit entry the payment just created.

        Sets the event (default "csar") and the remark on the newest entry for
        this player that isn't already labelled, so it is a no-op on bot
        versions where addUserPoints() stores the reason itself.
        """
        schema = await self.credits_log_schema()
        if not schema:
            return
        event = self.get_config(server).get('credits_log_event', 'csar')
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

    async def message(self, server: Server, name: str, text: str) -> None:
        await server.send_to_dcs({
            "command": "jshCsarRun",
            "lua": f'if jsh_csar then jsh_csar.message({lua_str(name)}, {lua_str(text)}) end'
        })

    async def record(self, server: Server, ucid: Optional[str], name: str, status: str,
                     rescues: int, points: int, reason: str, source: Optional[str]) -> None:
        """Writes the event row, the all-servers total, and the dynamic campaign breakdown."""
        dynamic = self.is_dynamic(server)
        async with self.apool.connection() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO jsh_csar_rescues
                        (server_name, player_ucid, player_name, pilot_status, rescues,
                         points, reason, source, dynamic)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (server.name, ucid, name, status, rescues, points, reason, source, dynamic))
                if not ucid:
                    return
                await conn.execute("""
                    INSERT INTO jsh_csar_totals (player_ucid, rescues, points, last_reason)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (player_ucid) DO UPDATE
                    SET rescues = jsh_csar_totals.rescues + EXCLUDED.rescues,
                        points = jsh_csar_totals.points + EXCLUDED.points,
                        last_reason = EXCLUDED.last_reason,
                        last_rescue = now() AT TIME ZONE 'utc'
                """, (ucid, rescues, points, reason))
                if dynamic:
                    await conn.execute("""
                        INSERT INTO jsh_csar_dynamic
                            (player_ucid, pilot_status, rescues, points, last_reason)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (player_ucid, pilot_status) DO UPDATE
                        SET rescues = jsh_csar_dynamic.rescues + EXCLUDED.rescues,
                            points = jsh_csar_dynamic.points + EXCLUDED.points,
                            last_reason = EXCLUDED.last_reason,
                            last_rescue = now() AT TIME ZONE 'utc'
                    """, (ucid, status, rescues, points, reason))

    # ------------------------------------------------------------ logbook awards
    async def logbook_schema(self) -> Optional[dict]:
        """Finds the logbook award columns once, so a schema change is a log line, not a crash."""
        if self._logbook is not None:
            return self._logbook or None
        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_name IN ('logbook_awards', 'logbook_pilot_awards')
            """)
            rows = await cursor.fetchall()
        cols: dict[str, set] = {'logbook_awards': set(), 'logbook_pilot_awards': set()}
        for table, column in rows:
            cols[table].add(column)
        if not cols['logbook_awards'] or not cols['logbook_pilot_awards']:
            self.log.warning("CSAR: logbook plugin tables not found, awards are disabled.")
            self._logbook = {}
            return None

        def pick(table: str, *candidates: str) -> Optional[str]:
            for c in candidates:
                if c in cols[table]:
                    return c
            return None

        schema = {
            'award_id': pick('logbook_awards', 'id', 'award_id'),
            'award_name': pick('logbook_awards', 'name', 'award_name', 'title'),
            'grant_ucid': pick('logbook_pilot_awards', 'player_ucid', 'ucid'),
            'grant_award': pick('logbook_pilot_awards', 'award_id', 'logbook_award_id', 'award'),
            'grant_citation': pick('logbook_pilot_awards', 'citation', 'reason', 'note'),
        }
        if not all(schema[k] for k in ('award_id', 'award_name', 'grant_ucid', 'grant_award')):
            self.log.warning(f"CSAR: unexpected logbook award schema {cols}, awards are disabled.")
            self._logbook = {}
            return None
        self._logbook = schema
        return schema

    async def _check_awards(self, ucid: str) -> list[str]:
        config_awards = self._awards_config
        if not config_awards:
            return []
        schema = await self.logbook_schema()
        if not schema:
            return []

        granted: list[str] = []
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(rescues, 0) FROM jsh_csar_totals WHERE player_ucid = %s", (ucid,))
            row = await cursor.fetchone()
            total = row[0] if row else 0
            cursor = await conn.execute(
                "SELECT pilot_status, rescues FROM jsh_csar_dynamic WHERE player_ucid = %s", (ucid,))
            by_status = {r[0]: r[1] for r in await cursor.fetchall()}

            for entry in config_awards:
                name = entry.get('award')
                needed = int(entry.get('rescues', 0))
                status = entry.get('pilot_status', 'any')
                if not name or needed <= 0:
                    continue
                have = total if status in (None, 'any', 'Any') else by_status.get(status, 0)
                if have < needed:
                    continue

                cursor = await conn.execute(
                    f"SELECT {schema['award_id']} FROM logbook_awards WHERE {schema['award_name']} = %s",
                    (name,))
                award = await cursor.fetchone()
                if not award:
                    self.log.warning(f"CSAR: award '{name}' does not exist, create it with /award create.")
                    continue
                cursor = await conn.execute(
                    f"SELECT 1 FROM logbook_pilot_awards "
                    f"WHERE {schema['grant_ucid']} = %s AND {schema['grant_award']} = %s",
                    (ucid, award[0]))
                if await cursor.fetchone():
                    continue

                label = "pilots" if status in (None, 'any', 'Any') else f"{status} pilots"
                citation = f"{have} {label} rescued"
                async with conn.transaction():
                    if schema['grant_citation']:
                        await conn.execute(
                            f"INSERT INTO logbook_pilot_awards "
                            f"({schema['grant_ucid']}, {schema['grant_award']}, {schema['grant_citation']}) "
                            f"VALUES (%s, %s, %s)", (ucid, award[0], citation))
                    else:
                        await conn.execute(
                            f"INSERT INTO logbook_pilot_awards "
                            f"({schema['grant_ucid']}, {schema['grant_award']}) VALUES (%s, %s)",
                            (ucid, award[0]))
                granted.append(name)
                self.log.info(f"CSAR: granted '{name}' to {ucid} ({citation})")
        return granted

    async def get_ucid(self, member: discord.Member) -> Optional[str]:
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT ucid FROM players WHERE discord_id = %s ORDER BY last_seen DESC LIMIT 1",
                (member.id,))
            row = await cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------ commands
    csar = Group(name="csar", description="CSAR rescue statistics")

    @csar.command(description='Show a player\'s CSAR rescues by pilot status')
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        member = user or interaction.user
        ucid = await self.get_ucid(member)
        if not ucid:
            await interaction.followup.send(
                f"{member.display_name} has no linked DCS account yet (/linkme).", ephemeral=True)
            return
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT rescues, points, last_rescue FROM jsh_csar_totals WHERE player_ucid = %s", (ucid,))
            total = await cursor.fetchone()
            cursor = await conn.execute(
                "SELECT pilot_status, rescues, points FROM jsh_csar_dynamic "
                "WHERE player_ucid = %s ORDER BY rescues DESC", (ucid,))
            rows = await cursor.fetchall()
        if not total:
            await interaction.followup.send(f"No rescues recorded for {member.display_name}.", ephemeral=True)
            return
        embed = discord.Embed(title=f"CSAR rescues – {member.display_name}", color=discord.Color.green())
        embed.add_field(name="All servers",
                        value=f"{total[0]} pilots, {total[1]} credits\nLast: {total[2]:%Y-%m-%d %H:%M}",
                        inline=False)
        if rows:
            embed.add_field(name="Pilot", value="\n".join(r[0] for r in rows), inline=True)
            embed.add_field(name="Rescued", value="\n".join(str(r[1]) for r in rows), inline=True)
            embed.add_field(name="Credits", value="\n".join(str(r[2]) for r in rows), inline=True)
            embed.set_footer(text="Breakdown is dynamic campaign only")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @csar.command(description='CSAR leaderboard')
    @app_commands.guild_only()
    @app_commands.choices(pilot_status=[app_commands.Choice(name=s, value=s) for s in STATUSES])
    async def top(self, interaction: discord.Interaction,
                  pilot_status: Optional[app_commands.Choice[str]] = None,
                  limit: app_commands.Range[int, 1, 25] = 10):
        await interaction.response.defer()
        status = pilot_status.value if pilot_status else None
        if status:
            sql = """
                SELECT p.name, t.rescues
                FROM jsh_csar_dynamic t JOIN players p ON p.ucid = t.player_ucid
                WHERE t.pilot_status = %s ORDER BY t.rescues DESC LIMIT %s
            """
            params = (status, limit)
        else:
            sql = """
                SELECT p.name, t.rescues
                FROM jsh_csar_totals t JOIN players p ON p.ucid = t.player_ucid
                ORDER BY t.rescues DESC LIMIT %s
            """
            params = (limit,)
        async with self.apool.connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
        if not rows:
            await interaction.followup.send("No rescues recorded yet.")
            return
        title = f"CSAR leaderboard – {status} (dynamic campaign)" if status else "CSAR leaderboard"
        embed = discord.Embed(title=title, color=discord.Color.green())
        embed.add_field(name="Pilot", value="\n".join(r[0] for r in rows), inline=True)
        embed.add_field(name="Rescued", value="\n".join(str(r[1]) for r in rows), inline=True)
        await interaction.followup.send(embed=embed)

    @csar.command(description='Show CSAR award progress')
    @app_commands.guild_only()
    async def awards(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        member = user or interaction.user
        ucid = await self.get_ucid(member)
        if not ucid:
            await interaction.followup.send(
                f"{member.display_name} has no linked DCS account yet (/linkme).", ephemeral=True)
            return
        config_awards = self.get_config().get('awards', [])
        if not config_awards:
            await interaction.followup.send("No CSAR awards are configured.", ephemeral=True)
            return
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(rescues, 0) FROM jsh_csar_totals WHERE player_ucid = %s", (ucid,))
            row = await cursor.fetchone()
            total = row[0] if row else 0
            cursor = await conn.execute(
                "SELECT pilot_status, rescues FROM jsh_csar_dynamic WHERE player_ucid = %s", (ucid,))
            by_status = {r[0]: r[1] for r in await cursor.fetchall()}
        lines = []
        for entry in config_awards:
            status = entry.get('pilot_status', 'any')
            needed = int(entry.get('rescues', 0))
            have = total if status in (None, 'any', 'Any') else by_status.get(status, 0)
            mark = "✅" if have >= needed else "▫️"
            scope = "" if status in (None, 'any', 'Any') else f" ({status})"
            lines.append(f"{mark} {entry.get('award')}{scope}: {min(have, needed)}/{needed}")
        embed = discord.Embed(title=f"CSAR awards – {member.display_name}",
                              description="\n".join(lines), color=discord.Color.gold())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @csar.command(description='Last CSAR rescues on a server')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def history(self, interaction: discord.Interaction,
                  server: app_commands.Transform[Server, utils.ServerTransformer],
                  limit: app_commands.Range[int, 1, 25] = 10):
        await interaction.response.defer(ephemeral=True)
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT player_name, pilot_status, rescues, points, source, rescued_at "
                "FROM jsh_csar_rescues WHERE server_name = %s ORDER BY rescued_at DESC LIMIT %s",
                (server.name, limit))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.followup.send(f"No rescues recorded on {server.name}.", ephemeral=True)
            return
        lines = [f"{r[5]:%Y-%m-%d %H:%M} {r[0]}: {r[2]}x {r[1]} (+{r[3]}, {r[4]})" for r in rows]
        await interaction.followup.send("```\n" + "\n".join(lines) + "\n```", ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Csar(bot, CsarEventListener))
