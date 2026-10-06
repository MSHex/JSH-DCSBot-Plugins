import asyncio
import io
import json

import discord

from core import Group, Plugin, PluginRequiredError, get_translation, utils
from discord import app_commands, SelectOption
from plugins.logbook.utils.ribbon import create_ribbon_rack, HAS_IMAGING
from psycopg.rows import dict_row
from services.bot import DCSServerBot

from .ranks import (
    calculate_kd_ratio,
    calculate_matching_credit_loss,
    format_decimal,
    format_playtime,
    get_rank_progress,
)

_ = get_translation(__name__.split('.')[1])


class RankStatus(Plugin):
    pilot = Group(name="pilot", description=_("Commands to view pilot campaign progress"))

    async def get_campaign_status(self, ucid: str) -> list[dict]:
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT c.id, c.name, COALESCE(cr.points, 0) AS credits,
                           COALESCE(stats.playtime, 0) AS playtime,
                           ARRAY(
                               SELECT cs.server_name
                               FROM campaigns_servers cs
                               WHERE cs.campaign_id = c.id
                               ORDER BY cs.server_name
                           ) AS servers
                    FROM campaigns c
                    LEFT JOIN credits cr ON cr.campaign_id = c.id AND cr.player_ucid = %s
                    LEFT JOIN LATERAL (
                        SELECT ROUND(SUM(EXTRACT(EPOCH FROM (s.hop_off - s.hop_on)))) AS playtime
                        FROM statistics s
                        JOIN missions m ON m.id = s.mission_id
                        JOIN campaigns_servers cs
                          ON cs.campaign_id = c.id AND cs.server_name = m.server_name
                        WHERE s.player_ucid = %s
                          AND tsrange(s.hop_on, s.hop_off) && tsrange(c.start, c.stop)
                    ) stats ON TRUE
                    WHERE (now() AT TIME ZONE 'utc')
                          BETWEEN c.start AND COALESCE(c.stop, now() AT TIME ZONE 'utc')
                    ORDER BY c.start DESC
                """, (ucid, ucid))
                return await cursor.fetchall()

    async def get_awards(self, ucid: str) -> list[dict]:
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT a.name, a.ribbon_colors, COUNT(*)::INTEGER AS count,
                           MAX(pa.granted_at) AS last_granted
                    FROM logbook_pilot_awards pa
                    JOIN logbook_awards a ON a.id = pa.award_id
                    WHERE pa.player_ucid = %s
                    GROUP BY a.id, a.name, a.ribbon_colors
                    ORDER BY last_granted DESC, a.name
                """, (ucid,))
                return await cursor.fetchall()

    async def get_campaign_combat_stats(self, campaign_id: int, ucid: str) -> dict:
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    WITH campaign_stats AS (
                        SELECT s.*
                        FROM statistics s
                        JOIN missions m ON m.id = s.mission_id
                        JOIN campaigns c ON c.id = %s
                        JOIN campaigns_servers cs
                          ON cs.campaign_id = c.id AND cs.server_name = m.server_name
                        WHERE s.player_ucid = %s
                          AND tsrange(s.hop_on, s.hop_off) && tsrange(c.start, c.stop)
                    )
                    SELECT COALESCE(SUM(kills), 0)::INTEGER AS kills,
                           COALESCE(SUM(
                               COALESCE(deaths_planes, 0) + COALESCE(deaths_helicopters, 0) +
                               COALESCE(deaths_ships, 0) + COALESCE(deaths_sams, 0) +
                               COALESCE(deaths_ground, 0)
                           ), 0)::INTEGER AS deaths,
                           COALESCE(SUM(teamkills), 0)::INTEGER AS friendly_kills
                    FROM campaign_stats
                """, (campaign_id, ucid))
                summary = dict(await cursor.fetchone())

                await cursor.execute("""
                    WITH campaign_stats AS (
                        SELECT s.*
                        FROM statistics s
                        JOIN missions m ON m.id = s.mission_id
                        JOIN campaigns c ON c.id = %s
                        JOIN campaigns_servers cs
                          ON cs.campaign_id = c.id AND cs.server_name = m.server_name
                        WHERE s.player_ucid = %s
                          AND s.hop_off IS NOT NULL
                          AND tsrange(s.hop_on, s.hop_off) && tsrange(c.start, c.stop)
                    )
                    SELECT slot,
                           ROUND(SUM(EXTRACT(EPOCH FROM (hop_off - hop_on))))::BIGINT AS playtime
                    FROM campaign_stats
                    GROUP BY slot
                    ORDER BY playtime DESC, slot
                    LIMIT 3
                """, (campaign_id, ucid))
                summary['top_aircraft'] = await cursor.fetchall()
                return summary

    async def get_squadrons(self, ucid: str) -> list[dict]:
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT s.name, sm.position
                    FROM squadron_members sm
                    JOIN squadrons s ON s.id = sm.squadron_id
                    WHERE sm.player_ucid = %s
                    ORDER BY s.name
                """, (ucid,))
                return await cursor.fetchall()

    async def get_penalty_summary(self, ucid: str) -> dict:
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT COALESCE(SUM(points), 0) AS points,
                           COUNT(*)::INTEGER AS event_count
                    FROM pu_events
                    WHERE init_id = %s
                """, (ucid,))
                return dict(await cursor.fetchone())

    def get_friendly_kill_reasons(self, servers: list[str]) -> list[str]:
        if 'punishment' not in self.bot.plugins:
            return []

        configs = []
        try:
            configs.append(self.get_config(plugin_name='punishment'))
            for server_name in servers:
                server = self.bot.servers.get(server_name)
                if server:
                    configs.append(self.get_config(server, plugin_name='punishment'))
        except Exception:
            self.log.exception("RankStatus could not read Punishment configuration.")
            return []

        reasons = set()
        for config in configs:
            for penalty in config.get('penalties', []):
                if penalty.get('event') in {'kill', 'collision_kill'}:
                    reasons.add(penalty.get('reason') or penalty['event'])
        return sorted(reasons)

    async def get_fk_credit_loss(self, campaign_id: int, ucid: str, reasons: list[str]) -> int | None:
        if not reasons:
            return None

        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("""
                    SELECT old_points, new_points, remark
                    FROM credits_log
                    WHERE campaign_id = %s
                      AND player_ucid = %s
                      AND event = 'punishment'
                      AND old_points > new_points
                """, (campaign_id, ucid))
                return calculate_matching_credit_loss(await cursor.fetchall(), reasons)

    async def get_hardcore_status(self, ucid: str) -> dict | None:
        hardcore = self.bot.cogs.get('Hardcore')
        if not hardcore or not hasattr(hardcore, 'economy'):
            return None
        server = await hardcore.resolve_economy_server()
        if not server:
            return None
        return await hardcore.economy.status_for(server, ucid)

    async def get_optional_data(self, label: str, operation):
        try:
            return await operation
        except Exception:
            self.log.exception("RankStatus could not load optional %s data.", label)
            return None

    def get_achievements(self, servers: list[str]) -> list[dict]:
        achievements = self.get_config(plugin_name='creditsystem').get('achievements')
        if achievements:
            return achievements

        for server_name in servers:
            server = self.bot.servers.get(server_name)
            if not server:
                continue
            achievements = self.get_config(server, plugin_name='creditsystem').get('achievements')
            if achievements:
                return achievements
        return []

    def get_achievement_name(self, achievement: dict | None) -> str:
        if not achievement:
            return _('Unranked')
        if 'role' in achievement:
            role = self.bot.get_role(achievement['role'])
            return role.name if role else str(achievement['role'])
        if achievement.get('badge'):
            return achievement['badge']['name']
        return _('Unnamed rank')

    @pilot.command(description=_("Shows a player's rank and progress in the current campaign"))
    @app_commands.describe(user=_('Player to check; leave empty to check yourself'))
    @app_commands.guild_only()
    @utils.app_has_role('DCS')
    async def status(self, interaction: discord.Interaction, user: discord.Member | None = None):
        ephemeral = utils.get_ephemeral(interaction)

        async def send_message(**kwargs):
            if interaction.response.is_done():
                await interaction.followup.send(**kwargs, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(**kwargs, ephemeral=ephemeral)

        await interaction.response.defer(ephemeral=ephemeral)

        member = user or interaction.user
        ucid = await self.bot.get_ucid_by_member(member)
        if not ucid:
            if member == interaction.user:
                mission = self.bot.cogs['Mission']
                await send_message(content=_("Use {} to link your account.").format(
                    (await utils.get_command(self.bot, name=mission.linkme.name)).mention))
            else:
                await send_message(content=_('{} has not linked a DCS account.').format(member.mention))
            return

        data = await self.get_campaign_status(ucid)
        if not data:
            await send_message(content=_('There is no active campaign at the moment.'))
            return

        if len(data) > 1:
            selected = await utils.selection(
                interaction,
                title=_("Campaign Status"),
                options=[
                    SelectOption(label=row['name'], value=str(index), default=(index == 0))
                    for index, row in enumerate(data)
                ],
                ephemeral=ephemeral
            )
            if selected is None:
                return
            selected = int(selected)
        else:
            selected = 0

        campaign = data[selected]
        achievements = self.get_achievements(campaign['servers'])
        if not achievements:
            await send_message(content=_('No ranks are configured for this campaign.'))
            return

        credits = int(campaign['credits'])
        playtime = int(campaign['playtime']) / 3600.0
        current_rank, next_rank = get_rank_progress(achievements, credits, playtime)
        is_self = member.id == interaction.user.id
        can_view_private = is_self or utils.check_roles(
            self.bot.roles.get('DCS Admin', []), interaction.user)

        awards, combat, squadrons = await asyncio.gather(
            self.get_awards(ucid),
            self.get_campaign_combat_stats(campaign['id'], ucid),
            self.get_squadrons(ucid)
        )

        penalty = fk_credit_loss = hardcore = None
        if can_view_private and 'punishment' in self.bot.plugins:
            fk_reasons = self.get_friendly_kill_reasons(campaign['servers'])
            penalty, fk_credit_loss = await asyncio.gather(
                self.get_optional_data('Punishment', self.get_penalty_summary(ucid)),
                self.get_optional_data(
                    'friendly-kill credit loss',
                    self.get_fk_credit_loss(campaign['id'], ucid, fk_reasons)
                )
            )
        if can_view_private and 'hardcore' in self.bot.plugins:
            hardcore = await self.get_optional_data('Hardcore', self.get_hardcore_status(ucid))

        embed = discord.Embed(
            title=_('Pilot Status'),
            description=_('**Campaign:** {campaign}').format(campaign=campaign['name']),
            color=discord.Color.blue()
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(
            name=_('🎖️ Current Rank'), value=f"**{self.get_achievement_name(current_rank)}**", inline=True)
        embed.add_field(name=_('💳 Available Credits'), value=f"**{credits:,}**", inline=True)
        embed.add_field(name=_('⏱️ Campaign Flight Time'), value=f"**{format_playtime(playtime)}**", inline=True)

        if next_rank:
            next_credits = next_rank.get('credits')
            next_playtime = next_rank.get('playtime')
            progress = [_('**Target Rank:** {rank}').format(rank=self.get_achievement_name(next_rank))]
            if next_credits is not None:
                remaining_credits = max(0, next_credits - credits)
                progress.append(
                    _('💳 **Credits:** {current:,} / {required:,} · **{remaining:,} remaining**').format(
                        remaining=remaining_credits, current=credits, required=next_credits
                    )
                )
            if next_playtime is not None:
                remaining_playtime = max(0, next_playtime - playtime)
                progress.append(
                    _('⏱️ **Flight Time:** {current} / {required} · **{remaining} remaining**').format(
                        remaining=format_playtime(remaining_playtime, round_up=True),
                        current=format_playtime(playtime),
                        required=format_playtime(next_playtime)
                    )
                )
            if next_rank.get('combined') and next_credits is not None and next_playtime is not None:
                progress.append(_('🔒 Both requirements must be met.'))
            elif next_credits is not None and next_playtime is not None:
                progress.append(_('🔓 Either requirement may be met.'))
            embed.add_field(name=_('📈 Promotion Progress'), value='\n'.join(progress), inline=False)
        else:
            embed.add_field(
                name=_('📈 Promotion Progress'), value=_('🏆 Maximum campaign rank achieved.'), inline=False)

        kills = int(combat['kills'])
        deaths = int(combat['deaths'])
        kd_ratio = calculate_kd_ratio(kills, deaths)
        embed.add_field(
            name=_('⚔️ Combat Record'),
            value=_('**K/D:** {ratio} ({kills} kills / {deaths} deaths)\n'
                    '**Friendly kills:** {friendly_kills}').format(
                        ratio=format_decimal(kd_ratio),
                        kills=kills,
                        deaths=deaths,
                        friendly_kills=int(combat['friendly_kills'])
                    ),
            inline=True
        )

        aircraft = combat['top_aircraft']
        if aircraft:
            aircraft_value = '\n'.join(
                f"{index}. **{row['slot']}** — {format_playtime(int(row['playtime']) / 3600.0)}"
                for index, row in enumerate(aircraft, start=1)
            )
        else:
            aircraft_value = _('No completed campaign flights yet.')
        embed.add_field(name=_('🛩️ Preferred Airframes'), value=aircraft_value, inline=True)

        if squadrons:
            squadron_value = '\n'.join(
                f"**{row['name']}**" + (f" — {row['position']}" if row.get('position') else '')
                for row in squadrons[:5]
            )
            if len(squadrons) > 5:
                squadron_value += _('\n…and {count} more').format(count=len(squadrons) - 5)
        else:
            squadron_value = _('Independent')

        profile_lines = [_('**Squadron:** {squadron}').format(squadron=squadron_value)]
        if hardcore:
            hardcore_mode = _('🔥 Active (+50%)') if hardcore['hardcore'] else _('🛡️ Normal')
            profile_lines.append(_('**Hardcore:** {mode}').format(mode=hardcore_mode))
            if (hardcore.get('session') or {}).get('hardcore_revoked'):
                profile_lines.append(_('⚠️ Revoked for the current session'))
        embed.add_field(name=_('🛡️ Pilot Profile'), value='\n'.join(profile_lines), inline=True)

        if penalty is not None:
            discipline_lines = [
                _('**Current penalty points:** {points}').format(
                    points=format_decimal(float(penalty['points']))),
                _('**Active penalty events:** {count}').format(count=penalty['event_count'])
            ]
            if fk_credit_loss is not None:
                discipline_lines.append(
                    _('**Campaign credits lost to friendly kills:** {credits:,}').format(
                        credits=fk_credit_loss)
                )
                discipline_lines.append(_('*Matched from recorded punishment reasons.*'))
            embed.add_field(name=_('⚠️ Discipline'), value='\n'.join(discipline_lines), inline=False)

        total_awards = sum(int(award['count']) for award in awards)
        if awards:
            award_lines = [
                f"• **{award['name']}**" + (f" ×{award['count']}" if award['count'] > 1 else '')
                for award in awards
            ]
            award_value = ''
            shown = 0
            for line in award_lines:
                candidate = award_value + ('\n' if award_value else '') + line
                if len(candidate) > 1000:
                    award_value += _('\n*…and {remaining} more award types*').format(
                        remaining=len(award_lines) - shown)
                    break
                award_value = candidate
                shown += 1
            embed.add_field(
                name=_('🏅 Awards ({count})').format(count=total_awards), value=award_value, inline=False)
        else:
            embed.add_field(name=_('🏅 Awards'), value=_('No awards earned yet.'), inline=False)

        if current_rank and current_rank.get('badge', {}).get('img'):
            embed.set_thumbnail(url=current_rank['badge']['img'])

        file = None
        if awards and HAS_IMAGING:
            ribbon_awards = []
            for award in awards:
                colors = award.get('ribbon_colors')
                if isinstance(colors, str):
                    try:
                        colors = json.loads(colors)
                    except (json.JSONDecodeError, TypeError):
                        colors = None
                ribbon_awards.append((award['name'], colors, int(award['count'])))

            ribbon_bytes = await asyncio.to_thread(create_ribbon_rack, ribbon_awards)
            if ribbon_bytes:
                file = discord.File(io.BytesIO(ribbon_bytes), filename='pilot-ribbons.png')
                embed.set_image(url='attachment://pilot-ribbons.png')

        embed.set_footer(text=_(
            'Rank, combat data and airframes use the selected campaign. '
            'Squadron, penalties and Hardcore are current; awards are Logbook totals.'
        ))
        if file:
            await send_message(embed=embed, file=file)
        else:
            await send_message(embed=embed)


async def setup(bot: DCSServerBot):
    for plugin in ['mission', 'userstats', 'gamemaster', 'creditsystem', 'logbook']:
        if plugin not in bot.plugins:
            raise PluginRequiredError(plugin)
    await bot.add_cog(RankStatus(bot))
