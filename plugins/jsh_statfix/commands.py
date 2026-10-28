import discord

from core import Plugin, Group, utils, DEFAULT_TAG
from discord import app_commands
from discord.ext import tasks
from psycopg.rows import dict_row
from services.bot import DCSServerBot

from . import sql

ADMIN_ROLES = ['DCS Admin', 'Admin']

#: The loop ticks far more often than the repair runs. Due-ness is decided
#: against the last successful run in jsh_statfix_runs, not against process
#: uptime, so a bot that restarts weekly still reaches a monthly interval --
#: a tasks.loop(hours=720) never would.
TICK_HOURS = 6.0


class _DryRun(Exception):
    """Unwinds the repair transaction after counting what it would have done."""


class StatFix(Plugin):
    """
    Repairs the playtime DCSServerBot credits for sessions nobody flew.

    When a pilot leaves a slot cleanly the bot records the moment they left.
    When they do not -- a DCS crash, a dropped connection, a server kill --
    the row is left open, and the next registerDCSServer repairs it by
    stamping hop_off with the MISSION'S end time (userstats/listener.py,
    close_all_statistics). A pilot who dropped out at 19:30 of a mission that
    ran to 06:00 is then credited ten and a half hours, and the row looks
    entirely normal afterwards.

    That is in the bot's own source, so it keeps happening; this plugin
    repairs the damage on a schedule rather than patching the bot, which an
    update would undo.
    """

    def __init__(self, bot: DCSServerBot):
        super().__init__(bot)

    # ------------------------------------------------------------- lifecycle
    async def cog_load(self) -> None:
        await super().cog_load()
        # The repair rewrites one shared database. On a multi-node setup every
        # node loads every plugin, so without this both would run it at once.
        if not self.node.master:
            self.log.debug("JSH StatFix: not the master node, scheduler not started.")
            return
        if self.get_config().get('enabled', True):
            utils.safe_start(self.schedule)

    async def cog_unload(self) -> None:
        if self.node.master and self.get_config().get('enabled', True):
            await utils.safe_cancel(self.schedule)
        await super().cog_unload()

    # ---------------------------------------------------------------- config
    def _settings(self) -> tuple[str, str, int]:
        config = self.locals.get(DEFAULT_TAG, {})
        min_age = f"{max(1, int(config.get('min_age_hours', 6)))} hours"
        grace = f"{max(0, int(config.get('grace_minutes', 15)))} minutes"
        interval = max(1, int(config.get('interval_days', 30)))
        return min_age, grace, interval

    # ---------------------------------------------------------------- repair
    async def preview(self) -> dict:
        """What the next run would touch. Reads only."""
        min_age, _, _ = self._settings()
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(sql.PREVIEW, {'min_age': min_age})
                counts = await cursor.fetchone()
                await cursor.execute(sql.TOTAL_HOURS)
                counts['hours'] = (await cursor.fetchone())['hours']
        return counts

    async def repair(self, dry_run: bool = False, triggered_by: str = 'schedule') -> dict:
        """
        The three passes, in order, in one transaction.

        Order matters. Pass 1 closes sessions the bot has not yet got to, which
        is what stops its own repair inventing the hours in the first place.
        Pass 2 uses each pilot's recorded events. Pass 3 catches what neither
        reached -- above all instructor and artillery-commander sessions, which
        generate no events because those slots shoot at nothing.

        A dry run does the work and then unwinds it, so the counts it reports
        are what a real run would do, not an estimate.
        """
        min_age, grace, _ = self._settings()
        result = {'orphans_closed': 0, 'corrected_by_events': 0, 'overlaps_clipped': 0,
                  'hours_before': None, 'hours_after': None, 'dry_run': dry_run,
                  'skipped_events_pass': False, 'error': None}

        async with self.apool.connection() as conn:
            cursor = await conn.execute(sql.TOTAL_HOURS)
            result['hours_before'] = (await cursor.fetchone())[0]
            cursor = await conn.execute(sql.START_RUN,
                                        (dry_run, triggered_by, result['hours_before']))
            run_id = (await cursor.fetchone())[0]

        try:
            async with self.apool.connection() as conn:
                cursor = await conn.execute(sql.HAS_MISSIONSTATS)
                has_missionstats = (await cursor.fetchone())[0]
                try:
                    async with conn.transaction():
                        cursor = await conn.execute(sql.CLOSE_ORPHANS, {'min_age': min_age})
                        result['orphans_closed'] = cursor.rowcount

                        if has_missionstats:
                            cursor = await conn.execute(sql.CORRECT_BY_EVENTS, {'grace': grace})
                            result['corrected_by_events'] = cursor.rowcount
                        else:
                            # Not an error: pass 3 still does most of the work.
                            result['skipped_events_pass'] = True

                        cursor = await conn.execute(sql.CLIP_OVERLAPS)
                        result['overlaps_clipped'] = cursor.rowcount

                        cursor = await conn.execute(sql.TOTAL_HOURS)
                        result['hours_after'] = (await cursor.fetchone())[0]

                        if dry_run:
                            raise _DryRun
                except _DryRun:
                    pass
        except Exception as ex:
            result['error'] = str(ex)
            self.log.error(f"JSH StatFix: repair failed: {ex}")

        async with self.apool.connection() as conn:
            await conn.execute(sql.FINISH_RUN, (
                result['orphans_closed'], result['corrected_by_events'],
                result['overlaps_clipped'], result['hours_after'], result['error'], run_id))

        if not result['error']:
            removed = (result['hours_before'] or 0) - (result['hours_after'] or 0)
            self.log.info(
                f"JSH StatFix: {'would close' if dry_run else 'closed'} "
                f"{result['orphans_closed']} orphan(s), corrected "
                f"{result['corrected_by_events']} by events, clipped "
                f"{result['overlaps_clipped']} overlap(s); {removed:.1f} phantom hour(s)"
                f"{' (dry run, nothing written)' if dry_run else ''}")
        return result

    # ------------------------------------------------------------- scheduler
    @tasks.loop(hours=TICK_HOURS)
    async def schedule(self):
        _, _, interval_days = self._settings()
        async with self.apool.connection() as conn:
            cursor = await conn.execute(sql.LAST_RUN)
            last = (await cursor.fetchone())[0]
        if last is not None:
            cursor_days = (discord.utils.utcnow().replace(tzinfo=None) - last).days
            if cursor_days < interval_days:
                return
        await self.repair(dry_run=bool(self.get_config().get('dry_run', False)),
                          triggered_by='schedule')

    @schedule.before_loop
    async def before_schedule(self):
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------- commands
    statfix = Group(name="statfix", description="Repair inflated flight hours")

    @staticmethod
    def _report(result: dict) -> str:
        if result['error']:
            return f"The repair failed and nothing was written:\n```{result['error']}```"
        removed = (result['hours_before'] or 0) - (result['hours_after'] or 0)
        head = ("**Dry run** -- nothing was written.\n" if result['dry_run'] else "")
        note = ("\n\n*missionstats is not installed, so the event-based pass was "
                "skipped. The overlap pass still ran.*"
                if result['skipped_events_pass'] else "")
        return (f"{head}"
                f"- Orphaned sessions closed: **{result['orphans_closed']}**\n"
                f"- Corrected from recorded events: **{result['corrected_by_events']}**\n"
                f"- Overlapping sessions clipped: **{result['overlaps_clipped']}**\n"
                f"- Phantom hours removed: **{removed:.1f}** "
                f"({result['hours_before']} -> {result['hours_after']}){note}")

    @statfix.command(name='preview',
                     description='Show what a repair would change, without changing it')
    @app_commands.guild_only()
    @utils.app_has_roles(ADMIN_ROLES)
    async def preview_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        counts = await self.preview()
        await interaction.followup.send(
            f"- Orphaned sessions old enough to close: **{counts['orphans']}**\n"
            f"- Sessions the bot closed at mission end: **{counts['machine_closed']}**\n"
            f"- Sessions overlapping the pilot's next one: **{counts['overlapping']}**\n"
            f"- Recorded hours right now: **{counts['hours']}**\n\n"
            f"`/statfix run dry_run:True` reports exactly what would change.",
            ephemeral=True)

    @statfix.command(description='Run the repair now')
    @app_commands.guild_only()
    @app_commands.describe(dry_run='Do the work, report it, then undo it. Nothing is written.')
    @utils.app_has_roles(ADMIN_ROLES)
    async def run(self, interaction: discord.Interaction, dry_run: bool = True):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.repair(dry_run=dry_run, triggered_by=str(interaction.user))
        await interaction.followup.send(self._report(result), ephemeral=True)

    @statfix.command(description='Show the last repair runs')
    @app_commands.guild_only()
    @utils.app_has_roles(ADMIN_ROLES)
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        _, _, interval_days = self._settings()
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(sql.RECENT_RUNS, (8,))
                rows = await cursor.fetchall()
        if not rows:
            await interaction.followup.send(
                f"No repair has run yet. The scheduler checks every "
                f"{TICK_HOURS:.0f} hours and runs every {interval_days} days.",
                ephemeral=True)
            return
        lines = []
        for row in rows:
            when = row['started_at'].strftime('%Y-%m-%d %H:%M')
            if row['error']:
                lines.append(f"`{when}` **failed** - {row['error'][:80]}")
                continue
            removed = (row['hours_before'] or 0) - (row['hours_after'] or 0)
            tag = ' *(dry run)*' if row['dry_run'] else ''
            lines.append(
                f"`{when}` {row['orphans_closed']}/{row['corrected_by_events']}/"
                f"{row['overlaps_clipped']} - {removed:.1f}h removed "
                f"({row['triggered_by']}){tag}")
        await interaction.followup.send(
            f"**Repair runs** (closed/corrected/clipped)\n" + "\n".join(lines) +
            f"\n\nRuns every {interval_days} days; the scheduler checks every "
            f"{TICK_HOURS:.0f} hours.", ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(StatFix(bot))
