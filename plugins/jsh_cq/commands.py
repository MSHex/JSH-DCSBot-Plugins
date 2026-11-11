import discord

from core import Plugin, Group, utils, DEFAULT_TAG
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import tasks
from psycopg.rows import dict_row
from services.bot import DCSServerBot
from typing import Optional

from . import rules, sql
from .listener import CQEventListener

ADMIN_ROLES = ['DCS Admin', 'Admin']


def _utcnow() -> datetime:
    """Naive UTC, as `traps.time` and the logbook columns are stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CarrierQual(Plugin[CQEventListener]):
    """
    Carrier qualification from greenieboard's traps, kept in logbook.

    logbook can auto-grant a qualification from `carrier_landings`, but that
    is a lifetime COUNT of every traps row -- bolters, wave-offs and cuts
    included, day and night alike, never expiring in practice. This applies
    real rules instead and writes the result into logbook's own tables, so a
    CQ shows in /logbook pilot and /qualification list like any other, and
    expires through logbook's own machinery.
    """

    def __init__(self, bot: DCSServerBot, listener=None):
        super().__init__(bot, listener)
        self._warned: set[str] = set()

    # ---------------------------------------------------------------- config
    def settings(self) -> dict:
        return self.locals.get(DEFAULT_TAG, {}) or {}

    def rules(self) -> rules.Rules:
        cfg = self.settings()
        return rules.Rules(day_traps=max(1, int(cfg.get('day_traps', 4))),
                           night_traps=max(0, int(cfg.get('night_traps', 2))),
                           valid_days=max(1, int(cfg.get('valid_days', 90))))

    def _warn_once(self, text: str) -> None:
        if text not in self._warned:
            self._warned.add(text)
            self.log.warning(f"JSH CQ: {text}")

    # ------------------------------------------------------------- lifecycle
    async def cog_load(self) -> None:
        await super().cog_load()
        # Every node sees its own servers' traps through the listener; the
        # sweep re-reads the shared database, so one node is enough for it.
        if self.node.master:
            self.sweep.change_interval(minutes=max(10, int(self.settings().get('sweep_minutes', 60))))
            utils.safe_start(self.sweep)

    async def cog_unload(self) -> None:
        if self.node.master:
            await utils.safe_cancel(self.sweep)
        await super().cog_unload()

    # ------------------------------------------------------------- the rules
    async def _qualification_ids(self, cursor) -> Optional[dict]:
        """{'day': id, 'night': id}, creating the qualifications if missing."""
        cfg, r = self.settings(), self.rules()
        names = {'day': cfg.get('day_qualification', 'CQ_CV_DAY'),
                 'night': cfg.get('night_qualification', 'CQ_CV_NIGHT')}
        ids = {}
        for kind, name in names.items():
            await cursor.execute(sql.ENSURE_QUALIFICATION, {
                'name': name, 'valid_days': r.valid_days,
                'description': f"Carrier qualified, {kind}: jsh_cq."})
            await cursor.execute(sql.QUALIFICATION, {'name': name})
            row = await cursor.fetchone()
            if not row:
                return None
            if row['requirements']:
                # logbook would then grant it too, from a lifetime count, and
                # renew it forever -- the very thing this plugin replaces.
                self._warn_once(f"qualification '{name}' has logbook requirements "
                                f"{row['requirements']}; clear them, or logbook will grant "
                                f"and renew it on its own lifetime count as well.")
            ids[kind] = row['id']
        return ids

    async def evaluate(self, ucid: Optional[str] = None) -> list[tuple[str, str]]:
        """
        Apply the rules to one pilot, or to everyone with a qualifying trap.
        Returns the NEW grants as (ucid, 'day'|'night'). Renewals are silent.
        """
        cfg, r, now = self.settings(), self.rules(), _utcnow()
        granted: list[tuple[str, str]] = []
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(sql.TABLES)
                have = await cursor.fetchone()
                if not have['traps'] or not have['logbook']:
                    self._warn_once("needs both greenieboard (traps) and logbook installed.")
                    return granted
                await cursor.execute(sql.TRAPS, {
                    'min_points': float(cfg.get('min_points', 3)),
                    'valid_days': r.valid_days,
                    'places': list(cfg.get('places') or []),
                    'ucid': ucid})
                by_pilot: dict[str, list] = {}
                for row in await cursor.fetchall():
                    by_pilot.setdefault(row['player_ucid'], []).append((row['night'], row['time']))

            for pilot, traps in by_pilot.items():
                st = rules.standing(traps, r, now)
                for kind, expires in (('day', st.day_expires), ('night', st.night_expires)):
                    if expires is None:
                        continue
                    async with conn.transaction():
                        async with conn.cursor(row_factory=dict_row) as cursor:
                            ids = await self._qualification_ids(cursor)
                            if not ids:
                                return granted
                            await cursor.execute(sql.HELD, {'ucid': pilot, 'qid': ids[kind]})
                            held = await cursor.fetchone()
                            old = held['expires_at'] if held else None
                            if not rules.should_write(old, held is not None, expires):
                                continue
                            await cursor.execute(sql.GRANT, {'ucid': pilot, 'qid': ids[kind],
                                                             'expires': expires})
                            if rules.is_new_grant(old, held is not None, now):
                                granted.append((pilot, kind))
                                await self._ribbon(cursor, pilot, kind)
        for pilot, kind in granted:
            await self._announce(pilot, kind)
        if granted:
            self.log.info(f"JSH CQ: new qualification(s): {granted}")
        return granted

    async def _ribbon(self, cursor, ucid: str, kind: str) -> None:
        """The configured logbook award, once per pilot -- not again on re-qualifying."""
        name = self.settings().get(f'{kind}_award')
        if not name:
            return
        await cursor.execute(sql.AWARD_ID, {'name': name})
        row = await cursor.fetchone()
        if not row:
            self._warn_once(f"{kind}_award '{name}' is not a logbook award; create it with /award create.")
            return
        await cursor.execute(sql.HAS_AWARD, {'ucid': ucid, 'aid': row['id']})
        if await cursor.fetchone():
            return
        await cursor.execute(sql.GIVE_AWARD, {
            'ucid': ucid, 'aid': row['id'],
            'citation': f"Carrier qualified ({kind}), {self.rules().day_traps if kind == 'day' else self.rules().night_traps} "
                        f"qualifying {kind} traps."})

    async def _announce(self, ucid: str, kind: str) -> None:
        channel_id = self.settings().get('channel')
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            self._warn_once(f"channel {channel_id} not found.")
            return
        member = await self.bot.get_member_by_ucid(ucid)
        who = member.mention if member else (await self._name(ucid))
        text = self.settings().get(f'{kind}_message') or (
            "{pilot} is now carrier qualified ({kind})!")
        try:
            body = text.format(pilot=who, kind='night' if kind == 'night' else 'day')
        except (KeyError, IndexError, ValueError):
            body = f"{who} is now carrier qualified ({kind})!"
        try:
            await channel.send(body, allowed_mentions=discord.AllowedMentions(
                users=[member] if member else False, roles=False, everyone=False))
        except discord.HTTPException as ex:
            self.log.warning(f"JSH CQ: could not announce in {channel_id}: {ex}")

    async def _name(self, ucid: str) -> str:
        async with self.apool.connection() as conn:
            cursor = await conn.execute("SELECT name FROM players WHERE ucid = %s", (ucid,))
            row = await cursor.fetchone()
        return row[0] if row else ucid

    # ------------------------------------------------------------- scheduler
    @tasks.loop(minutes=60)
    async def sweep(self):
        # A trap the listener missed -- bot restarting, agent node down -- is
        # still picked up here, and so are lapses in the other direction.
        try:
            await self.evaluate()
        except Exception as ex:
            self.log.exception(f"JSH CQ: sweep failed: {ex}")

    @sweep.before_loop
    async def before_sweep(self):
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------- commands
    cq = Group(name="cq", description="Carrier qualification")

    @cq.command(name='status', description='Show carrier qualification progress')
    @app_commands.guild_only()
    @utils.app_has_role('DCS')
    async def status(self, interaction: discord.Interaction,
                     member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        member = member or interaction.user
        ucid = await self.bot.get_ucid_by_member(member)
        if not ucid:
            await interaction.followup.send(f"{member.display_name} has no linked DCS account.",
                                            ephemeral=True)
            return
        cfg, r = self.settings(), self.rules()
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(sql.TRAPS, {
                    'min_points': float(cfg.get('min_points', 3)), 'valid_days': r.valid_days,
                    'places': list(cfg.get('places') or []), 'ucid': ucid})
                traps = [(row['night'], row['time']) for row in await cursor.fetchall()]
        st = rules.standing(traps, r, _utcnow())

        def line(expires):
            return f"✅ until {expires:%Y-%m-%d}" if expires else "❌ not yet"

        embed = discord.Embed(title=f"Carrier qualification: {member.display_name}",
                              colour=discord.Colour.blue())
        embed.add_field(name="Day", value=line(st.day_expires), inline=True)
        embed.add_field(name="Night", value=line(st.night_expires), inline=True)
        embed.add_field(name=f"Qualifying traps, last {r.valid_days} days",
                        value=rules.progress_line(st, r), inline=False)
        embed.set_footer(text=f"Counts: wire caught, {float(cfg.get('min_points', 3)):g}+ points. "
                              f"Night needs a current day CQ.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @cq.command(name='check', description='Re-apply the carrier qualification rules to everyone now')
    @app_commands.guild_only()
    @utils.app_has_roles(ADMIN_ROLES)
    async def check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        granted = await self.evaluate()
        await interaction.followup.send(
            f"Done. {len(granted)} new qualification(s)." if granted
            else "Done. No new qualifications; renewals are applied silently.", ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(CarrierQual(bot, CQEventListener))
