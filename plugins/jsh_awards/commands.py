import discord

from core import Plugin, Group, utils, DEFAULT_TAG
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import tasks
from psycopg.rows import dict_row
from services.bot import DCSServerBot
from typing import Optional

from . import board, sql

ADMIN_ROLES = ['DCS Admin', 'Admin']


class MonthlyAwards(Plugin):
    """
    Posts last month's leaders on the 1st: credits earned, CSAR rescues,
    flight hours, and K/D air-to-air and air-to-ground. Optionally gives each
    winner a logbook award with the month in its citation.
    """

    # ---------------------------------------------------------------- config
    def settings(self) -> dict:
        return self.locals.get(DEFAULT_TAG, {}) or {}

    def categories(self) -> list[str]:
        wanted = self.settings().get('categories') or list(board.CATEGORIES)
        return [c for c in board.CATEGORIES if c in wanted]

    # ------------------------------------------------------------- lifecycle
    async def cog_load(self) -> None:
        await super().cog_load()
        # One board, one channel: only the master posts it.
        if self.node.master and self.settings().get('enabled', True):
            utils.safe_start(self.schedule)

    async def cog_unload(self) -> None:
        await utils.safe_cancel(self.schedule)
        await super().cog_unload()

    # ----------------------------------------------------------------- board
    async def leaders(self, month) -> tuple[Optional[str], dict[str, list[board.Entry]]]:
        """
        (campaign name, {category: leaders}) for one month, counting only the
        campaign: its servers, its dates, its registered pilots. No campaign
        overlapping the month: (None, {}).
        """
        cfg = self.settings()
        top = max(1, min(10, int(cfg.get('top', 3))))
        month_start, month_end = board.month_bounds(month)
        result: dict[str, list[board.Entry]] = {}
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(sql.CAMPAIGN, {'campaign_id': cfg.get('campaign_id'),
                                                    'start': month_start, 'end': month_end})
                campaign = await cursor.fetchone()
                if not campaign:
                    return None, result
                window = board.clip(month_start, month_end, campaign['start'], campaign['stop'])
                if not window:
                    return campaign['name'], result
                params = {'start': window[0], 'end': window[1], 'top': top,
                          'campaign_id': campaign['id'],
                          'exclude': [str(e).lower() for e in (cfg.get('exclude_events') or ['donation'])]}
                await cursor.execute(sql.TABLES)
                have = await cursor.fetchone()
                for category in self.categories():
                    if category == 'credits' and not have['credits']:
                        continue
                    if category == 'csar' and not have['csar']:
                        continue
                    if category in ('kd_aa', 'kd_ag'):
                        await cursor.execute(sql.KD_AA if category == 'kd_aa' else sql.KD_AG, params)
                        result[category] = board.ranked_kd(
                            await cursor.fetchall(), int(cfg.get('min_kills', 5)), top)
                        continue
                    query = {'credits': sql.CREDITS, 'csar': sql.CSAR, 'hours': sql.HOURS}[category]
                    await cursor.execute(query, params)
                    result[category] = [
                        board.Entry(r['ucid'], r['name'], int(r['discord_id'] or -1), float(r['value']))
                        for r in await cursor.fetchall()]
        return campaign['name'], result

    def embed(self, month, campaign: Optional[str], leaders: dict) -> discord.Embed:
        embed = discord.Embed(title=self.settings().get('title', 'Monthly awards') + f" — {month:%B %Y}",
                              description=(f"Campaign: **{campaign}**" if campaign
                                           else "No campaign ran this month."),
                              colour=discord.Colour.gold())
        for category in self.categories():
            if category in leaders:
                embed.add_field(name=board.TITLES[category],
                                value=board.field(category, leaders[category]), inline=False)
        cfg = self.settings()
        embed.set_footer(text=f"K/D needs {int(cfg.get('min_kills', 5))}+ kills of that kind. "
                              f"Hours are time in a slot. Campaign servers and "
                              f"registered pilots only.")
        return embed

    async def give_awards(self, month, leaders: dict) -> list[str]:
        """The configured logbook award for each category's winner. Returns problems."""
        awards = self.settings().get('awards') or {}
        problems = []
        if not awards:
            return problems
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(sql.TABLES)
                if not (await cursor.fetchone())['logbook']:
                    return ["awards are configured but logbook is not installed"]
                for category, name in awards.items():
                    winners = leaders.get(category) or []
                    if not winners or not name:
                        continue
                    await cursor.execute(sql.AWARD_ID, {'name': name})
                    row = await cursor.fetchone()
                    if not row:
                        problems.append(f"'{name}' is not a logbook award (/award create)")
                        continue
                    # The citation names the month, which is what keeps a
                    # repeated post from awarding the same month twice.
                    await cursor.execute(sql.GIVE_AWARD, {
                        'ucid': winners[0].ucid, 'aid': row['id'],
                        'citation': board.citation(category, month, winners[0])})
        return problems

    async def post(self, month, channel_id: Optional[int] = None) -> Optional[discord.Message]:
        channel_id = channel_id or self.settings().get('channel')
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if not channel:
            self.log.warning(f"JSH Awards: channel {channel_id} not found, board not posted.")
            return None
        campaign, leaders = await self.leaders(month)
        message = await channel.send(embed=self.embed(month, campaign, leaders),
                                     allowed_mentions=discord.AllowedMentions.none())
        for problem in await self.give_awards(month, leaders):
            self.log.warning(f"JSH Awards: {problem}")
        async with self.apool.connection() as conn:
            await conn.execute(sql.MARK_POSTED, {'month': month, 'message_id': message.id,
                                                 'note': None})
        self.log.info(f"JSH Awards: posted the board for {month:%Y-%m}.")
        return message

    # ------------------------------------------------------------- scheduler
    @tasks.loop(hours=1)
    async def schedule(self):
        month = board.previous_month(datetime.now(timezone.utc).date())
        async with self.apool.connection() as conn:
            if (await (await conn.execute(sql.POSTED, {'month': month})).fetchone()):
                return
            if not (await (await conn.execute(sql.ANY_POSTED)).fetchone())[0]:
                # First run ever: do not post a board for a month the plugin
                # was not installed for. /awards post can still do it by hand.
                await conn.execute(sql.MARK_POSTED, {'month': month, 'message_id': None,
                                                     'note': 'skipped: installed this month'})
                self.log.info(f"JSH Awards: installed; the first board posts on the 1st of "
                              f"next month. Use /awards post to post {month:%Y-%m} by hand.")
                return
        try:
            await self.post(month)
        except Exception as ex:
            self.log.exception(f"JSH Awards: could not post the board: {ex}")

    @schedule.before_loop
    async def before_schedule(self):
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------- commands
    awards = Group(name="awards", description="Monthly awards board")

    @awards.command(name='preview', description='Show a month\'s board, privately')
    @app_commands.guild_only()
    @utils.app_has_role('DCS')
    @app_commands.describe(month='YYYY-MM. Defaults to last month.')
    async def preview(self, interaction: discord.Interaction, month: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        which = board.parse_month(month, datetime.now(timezone.utc).date())
        if not which:
            await interaction.followup.send("Month must look like 2026-10.", ephemeral=True)
            return
        campaign, leaders = await self.leaders(which)
        await interaction.followup.send(embed=self.embed(which, campaign, leaders),
                                        ephemeral=True)

    @awards.command(name='post', description='Post a month\'s board to the awards channel now')
    @app_commands.guild_only()
    @utils.app_has_roles(ADMIN_ROLES)
    @app_commands.describe(month='YYYY-MM. Defaults to last month.')
    async def post_cmd(self, interaction: discord.Interaction, month: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        which = board.parse_month(month, datetime.now(timezone.utc).date())
        if not which:
            await interaction.followup.send("Month must look like 2026-10.", ephemeral=True)
            return
        message = await self.post(which)
        await interaction.followup.send(
            f"Posted: {message.jump_url}" if message else
            "Not posted: set `channel` in jsh_awards.yaml to a channel the bot can see.",
            ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(MonthlyAwards(bot))
