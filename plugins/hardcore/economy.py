from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING, cast

import discord

from core import Server, utils

if TYPE_CHECKING:
    from .commands import Hardcore


UTC_SQL_NOW = "(NOW() AT TIME ZONE 'utc')"


@dataclass(slots=True)
class Settlement:
    session_id: int
    player_ucid: str
    player_name: str
    campaign_id: int
    eligible_seconds: int
    completed_blocks: int
    deaths: int
    hardcore_at_start: bool
    hardcore_revoked: bool
    gross_half_units: int
    penalty_percent: int
    net_half_units: int
    paid_credits: int
    carry_half_units: int
    old_balance: int
    new_balance: int


class EconomyService:
    """
    Persistent economy engine.

    Money math is done in half-credit units:
      2 units = 1 credit
      50 units = 25 credits
      75 units = 37.5 credits

    Native DCSServerBot credits remain INTEGER. Any 0.5 remainder is carried
    in hardcore_wallet_carry and applied to the player's next settlement.
    """

    LOSS_DEBOUNCE_SECONDS = 10

    def __init__(self, plugin: "Hardcore"):
        self.plugin = plugin
        self.bot = plugin.bot
        self.apool = plugin.apool
        self.log = plugin.log
        self.node = plugin.node

    def enabled(self, server: Server) -> bool:
        return bool(self.plugin.get_config(server).get("enabled", False))

    def cfg(self, server: Server) -> dict:
        return self.plugin.get_config(server)

    async def running_campaign(self, server: Server) -> tuple[Optional[int], Optional[str]]:
        return await utils.get_running_campaign_async(self.node, server)

    async def is_hardcore(self, ucid: str) -> bool:
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT active FROM hardcore_status WHERE player_ucid = %s",
                (ucid,)
            )
            row = await cursor.fetchone()
            return bool(row and row[0])

    async def set_hardcore(self, ucid: str, active: bool, changed_by: str) -> None:
        async with self.apool.connection() as conn:
            await conn.execute("""
                INSERT INTO hardcore_status (player_ucid, active, changed_at, changed_by)
                VALUES (%s, %s, (NOW() AT TIME ZONE 'utc'), %s)
                ON CONFLICT (player_ucid) DO UPDATE
                SET active = EXCLUDED.active,
                    changed_at = EXCLUDED.changed_at,
                    changed_by = EXCLUDED.changed_by
            """, (ucid, active, changed_by))

    async def get_balance(self, campaign_id: int, ucid: str) -> int:
        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT points
                FROM credits
                WHERE campaign_id = %s AND player_ucid = %s
            """, (campaign_id, ucid))
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def has_open_session(self, ucid: str) -> bool:
        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT 1 FROM hardcore_sessions
                WHERE player_ucid = %s AND settled = FALSE
                LIMIT 1
            """, (ucid,))
            return await cursor.fetchone() is not None

    async def enroll(self, server: Server, ucid: str, changed_by: str = "USER") -> tuple[bool, str, int]:
        config = self.cfg(server)
        fee = int(config.get("buyin_fee", 500))
        campaign_id, campaign_name = await self.running_campaign(server)
        if not campaign_id:
            return False, "No active campaign is running on the configured economy server.", 0

        if await self.has_open_session(ucid):
            return False, "You cannot change Hardcore status while an economy session is active.", 0

        async with self.apool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute("""
                    SELECT active
                    FROM hardcore_status
                    WHERE player_ucid = %s
                    FOR UPDATE
                """, (ucid,))
                row = await cursor.fetchone()
                if row and row[0]:
                    balance = await self._balance_on_conn(conn, campaign_id, ucid)
                    return False, "You are already enrolled in Hardcore Mode.", balance

                cursor = await conn.execute("""
                    SELECT points
                    FROM credits
                    WHERE campaign_id = %s AND player_ucid = %s
                    FOR UPDATE
                """, (campaign_id, ucid))
                credit_row = await cursor.fetchone()
                balance = int(credit_row[0]) if credit_row else 0

                if balance < fee:
                    return False, f"You need {fee} credits to enroll. Current balance: {balance}.", balance

                new_balance = balance - fee
                await conn.execute("""
                    INSERT INTO credits (campaign_id, player_ucid, points)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (campaign_id, player_ucid)
                    DO UPDATE SET points = EXCLUDED.points
                """, (campaign_id, ucid, new_balance))

                await conn.execute("""
                    INSERT INTO credits_log
                        (campaign_id, event, player_ucid, old_points, new_points, remark)
                    VALUES (%s, 'hardcore buy-in', %s, %s, %s, %s)
                """, (
                    campaign_id, ucid, balance, new_balance,
                    f"Hardcore enrollment fee for campaign {campaign_name or campaign_id}"
                ))

                await conn.execute("""
                    INSERT INTO hardcore_status (player_ucid, active, changed_at, changed_by)
                    VALUES (%s, TRUE, (NOW() AT TIME ZONE 'utc'), %s)
                    ON CONFLICT (player_ucid) DO UPDATE
                    SET active = TRUE,
                        changed_at = EXCLUDED.changed_at,
                        changed_by = EXCLUDED.changed_by
                """, (ucid, changed_by))

        self._sync_live_player_balance(server, ucid, new_balance)
        return True, f"Hardcore Mode activated. {fee} credits deducted.", new_balance

    async def leave(self, ucid: str, changed_by: str = "USER") -> tuple[bool, str]:
        if await self.has_open_session(ucid):
            return False, "You cannot leave Hardcore while an economy session is active."
        if not await self.is_hardcore(ucid):
            return False, "You are already in Normal Mode."
        await self.set_hardcore(ucid, False, changed_by)
        return True, "Hardcore Mode disabled. The buy-in is non-refundable."

    async def _balance_on_conn(self, conn, campaign_id: int, ucid: str) -> int:
        cursor = await conn.execute("""
            SELECT points FROM credits
            WHERE campaign_id = %s AND player_ucid = %s
        """, (campaign_id, ucid))
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    def _sync_live_player_balance(self, server: Server, ucid: str, balance: int) -> None:
        player = server.get_player(ucid=ucid)
        if player and hasattr(player, "_points"):
            # CreditPlayer cache. DB is already authoritative; avoid invoking the
            # synchronous points setter a second time.
            player._points = balance
            self.bot.loop.create_task(server.send_to_dcs({
                "command": "updateUserPoints",
                "ucid": ucid,
                "points": balance
            }))

    async def open_session(self, server: Server, ucid: str, player_name: str) -> Optional[int]:
        if not self.enabled(server):
            return None

        campaign_id, _ = await self.running_campaign(server)
        if not campaign_id:
            self.log.warning("Hardcore: no running campaign on %s; session not opened for %s", server.name, ucid)
            return None

        hardcore = await self.is_hardcore(ucid)

        async with self.apool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute("""
                    SELECT id FROM hardcore_sessions
                    WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
                    FOR UPDATE
                """, (server.name, ucid))
                row = await cursor.fetchone()
                if row:
                    await conn.execute("""
                        UPDATE hardcore_sessions
                        SET player_name = %s,
                            last_seen_at = (NOW() AT TIME ZONE 'utc')
                        WHERE id = %s
                    """, (player_name, row[0]))
                    return int(row[0])

                cursor = await conn.execute("""
                    INSERT INTO hardcore_sessions (
                        server_name, player_ucid, campaign_id, player_name,
                        hardcore_at_start, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, (NOW() AT TIME ZONE 'utc'))
                    RETURNING id
                """, (server.name, ucid, campaign_id, player_name, hardcore))
                return int((await cursor.fetchone())[0])

    async def start_segment(self, server: Server, ucid: str, player_name: str) -> None:
        session_id = await self.open_session(server, ucid, player_name)
        if not session_id:
            return
        async with self.apool.connection() as conn:
            await conn.execute("""
                UPDATE hardcore_sessions
                SET segment_started_at = COALESCE(segment_started_at, (NOW() AT TIME ZONE 'utc')),
                    last_seen_at = (NOW() AT TIME ZONE 'utc')
                WHERE id = %s AND settled = FALSE
            """, (session_id,))

    async def pause_segment(self, server: Server, ucid: str) -> None:
        async with self.apool.connection() as conn:
            await conn.execute("""
                UPDATE hardcore_sessions
                SET eligible_seconds = eligible_seconds +
                    CASE
                        WHEN segment_started_at IS NULL THEN 0
                        ELSE GREATEST(
                            0,
                            FLOOR(EXTRACT(EPOCH FROM (
                                (NOW() AT TIME ZONE 'utc') - segment_started_at
                            )))::BIGINT
                        )
                    END,
                    segment_started_at = NULL,
                    airborne = FALSE,
                    last_seen_at = (NOW() AT TIME ZONE 'utc')
                WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
            """, (server.name, ucid))

    async def set_airborne(self, server: Server, ucid: str, airborne: bool) -> None:
        async with self.apool.connection() as conn:
            await conn.execute("""
                UPDATE hardcore_sessions
                SET airborne = %s,
                    last_seen_at = (NOW() AT TIME ZONE 'utc')
                WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
            """, (airborne, server.name, ucid))

    async def is_airborne(self, server: Server, ucid: str) -> bool:
        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT airborne
                FROM hardcore_sessions
                WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
            """, (server.name, ucid))
            row = await cursor.fetchone()
            return bool(row and row[0])

    async def record_loss(self, server: Server, ucid: str, reason: str) -> bool:
        """
        Record one loss incident. Returns True if a new loss was counted.
        crash/pilot_death/eject commonly arrive close together, so events within
        LOSS_DEBOUNCE_SECONDS are coalesced into one incident.
        """
        async with self.apool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute("""
                    SELECT id, deaths, hardcore_at_start, hardcore_revoked,
                           EXTRACT(EPOCH FROM (
                               (NOW() AT TIME ZONE 'utc') - last_loss_at
                           ))
                    FROM hardcore_sessions
                    WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
                    FOR UPDATE
                """, (server.name, ucid))
                row = await cursor.fetchone()
                if not row:
                    return False

                session_id, deaths, hc_start, hc_revoked, age = row
                if age is not None and float(age) < self.LOSS_DEBOUNCE_SECONDS:
                    return False

                new_deaths = int(deaths) + 1
                revoke = bool(hc_start and not hc_revoked)

                await conn.execute("""
                    UPDATE hardcore_sessions
                    SET deaths = %s,
                        hardcore_revoked = hardcore_revoked OR %s,
                        airborne = FALSE,
                        last_loss_at = (NOW() AT TIME ZONE 'utc'),
                        last_loss_reason = %s,
                        last_seen_at = (NOW() AT TIME ZONE 'utc')
                    WHERE id = %s
                """, (new_deaths, revoke, reason, session_id))

                await conn.execute("""
                    INSERT INTO hardcore_events (session_id, event_type, details)
                    VALUES (%s, 'loss', %s)
                """, (session_id, reason))

                if revoke:
                    await conn.execute("""
                        INSERT INTO hardcore_status (player_ucid, active, changed_at, changed_by)
                        VALUES (%s, FALSE, (NOW() AT TIME ZONE 'utc'), 'LOSS')
                        ON CONFLICT (player_ucid) DO UPDATE
                        SET active = FALSE,
                            changed_at = EXCLUDED.changed_at,
                            changed_by = 'LOSS'
                    """, (ucid,))

        return True

    @staticmethod
    def penalty_percent(deaths: int, config: dict) -> int:
        penalties = config.get("death_penalties", {})
        if deaths <= 0:
            return int(penalties.get(0, penalties.get("0", 0)))
        if deaths == 1:
            return int(penalties.get(1, penalties.get("1", 20)))
        if deaths == 2:
            return int(penalties.get(2, penalties.get("2", 50)))
        return int(penalties.get(3, penalties.get("3", 90)))

    @staticmethod
    def _reward_to_half_units(value) -> int:
        # Values must resolve exactly to half-credit increments.
        return int(round(float(value) * 2))

    async def settle(self, server: Server, ucid: str) -> Optional[Settlement]:
        config = self.cfg(server)
        block_minutes = int(config.get("block_minutes", 15))
        if block_minutes <= 0:
            raise ValueError("block_minutes must be > 0")
        block_seconds = block_minutes * 60

        async with self.apool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute("""
                    SELECT id, campaign_id, COALESCE(player_name, player_ucid),
                           eligible_seconds,
                           segment_started_at,
                           deaths,
                           hardcore_at_start,
                           hardcore_revoked
                    FROM hardcore_sessions
                    WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
                    FOR UPDATE
                """, (server.name, ucid))
                row = await cursor.fetchone()
                if not row:
                    return None

                (
                    session_id, campaign_id, player_name, eligible_seconds,
                    segment_started_at, deaths, hc_start, hc_revoked
                ) = row

                total_seconds = int(eligible_seconds)
                if segment_started_at is not None:
                    cursor = await conn.execute("""
                        SELECT GREATEST(
                            0,
                            FLOOR(EXTRACT(EPOCH FROM (
                                (NOW() AT TIME ZONE 'utc') - %s
                            )))::BIGINT
                        )
                    """, (segment_started_at,))
                    total_seconds += int((await cursor.fetchone())[0])

                blocks = total_seconds // block_seconds

                # A revoked Hardcore session is repriced entirely at Normal rate.
                hardcore_rate_applies = bool(hc_start and not hc_revoked)
                rate = (
                    config.get("hardcore_reward", 37.5)
                    if hardcore_rate_applies
                    else config.get("normal_reward", 25)
                )
                rate_half_units = self._reward_to_half_units(rate)
                gross_half_units = int(blocks * rate_half_units)

                penalty = self.penalty_percent(int(deaths), config)
                # Integer math; floor to half-credit units after percentage.
                net_half_units = (gross_half_units * (100 - penalty)) // 100

                cursor = await conn.execute("""
                    SELECT half_credit_units
                    FROM hardcore_wallet_carry
                    WHERE campaign_id = %s AND player_ucid = %s
                    FOR UPDATE
                """, (campaign_id, ucid))
                carry_row = await cursor.fetchone()
                carry = int(carry_row[0]) if carry_row else 0

                combined_half_units = net_half_units + carry
                payout = combined_half_units // 2
                new_carry = combined_half_units % 2

                cursor = await conn.execute("""
                    SELECT points
                    FROM credits
                    WHERE campaign_id = %s AND player_ucid = %s
                    FOR UPDATE
                """, (campaign_id, ucid))
                credit_row = await cursor.fetchone()
                old_balance = int(credit_row[0]) if credit_row else 0
                new_balance = old_balance + payout

                # Respect CreditSystem's configured maximum if present.
                credit_plugin = self.bot.cogs.get("CreditSystem")
                if credit_plugin:
                    credit_cfg = credit_plugin.get_config(server)
                    if "max_points" in credit_cfg:
                        new_balance = min(new_balance, int(credit_cfg["max_points"]))
                        payout = max(0, new_balance - old_balance)

                await conn.execute("""
                    INSERT INTO credits (campaign_id, player_ucid, points)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (campaign_id, player_ucid)
                    DO UPDATE SET points = EXCLUDED.points
                """, (campaign_id, ucid, new_balance))

                if payout:
                    await conn.execute("""
                        INSERT INTO credits_log
                            (campaign_id, event, player_ucid, old_points, new_points, remark)
                        VALUES (%s, 'flight economy', %s, %s, %s, %s)
                    """, (
                        campaign_id, ucid, old_balance, new_balance,
                        f"{blocks} x {block_minutes}m block(s), {deaths} loss(es), {penalty}% penalty"
                    ))

                await conn.execute("""
                    INSERT INTO hardcore_wallet_carry
                        (campaign_id, player_ucid, half_credit_units)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (campaign_id, player_ucid) DO UPDATE
                    SET half_credit_units = EXCLUDED.half_credit_units
                """, (campaign_id, ucid, new_carry))

                await conn.execute("""
                    UPDATE hardcore_sessions
                    SET eligible_seconds = %s,
                        segment_started_at = NULL,
                        airborne = FALSE,
                        settled = TRUE,
                        payout = %s,
                        gross_half_units = %s,
                        penalty_percent = %s,
                        settled_at = (NOW() AT TIME ZONE 'utc'),
                        last_seen_at = (NOW() AT TIME ZONE 'utc')
                    WHERE id = %s
                """, (
                    total_seconds, payout, gross_half_units,
                    penalty, session_id
                ))

        self._sync_live_player_balance(server, ucid, new_balance)

        settlement = Settlement(
            session_id=int(session_id),
            player_ucid=ucid,
            player_name=str(player_name),
            campaign_id=int(campaign_id),
            eligible_seconds=total_seconds,
            completed_blocks=int(blocks),
            deaths=int(deaths),
            hardcore_at_start=bool(hc_start),
            hardcore_revoked=bool(hc_revoked),
            gross_half_units=gross_half_units,
            penalty_percent=penalty,
            net_half_units=net_half_units,
            paid_credits=payout,
            carry_half_units=new_carry,
            old_balance=old_balance,
            new_balance=new_balance
        )

        self.log.info(
            "Hardcore SETTLEMENT | server=%s | ucid=%s | session=%s | seconds=%s | "
            "blocks=%s | hc_start=%s | revoked=%s | deaths=%s | gross_half=%s | "
            "penalty=%s%% | net_half=%s | carry=%s | payout=%s | old=%s | new=%s",
            server.name, ucid, session_id, total_seconds, blocks, bool(hc_start),
            bool(hc_revoked), int(deaths), gross_half_units, penalty,
            net_half_units, new_carry, payout, old_balance, new_balance
        )

        if blocks > 0 and payout <= 0 and new_balance <= old_balance:
            self.log.warning(
                "Hardcore ZERO PAYOUT | server=%s | ucid=%s | session=%s | "
                "seconds=%s | blocks=%s | deaths=%s | penalty=%s%% | old=%s | new=%s",
                server.name, ucid, session_id, total_seconds, blocks,
                int(deaths), penalty, old_balance, new_balance
            )

        if config.get("dm_receipt", True):
            await self.send_receipt(settlement, block_minutes)

        return settlement

    async def send_receipt(self, s: Settlement, block_minutes: int) -> None:
        member = await self.bot.get_member_by_ucid(s.player_ucid)
        if not member:
            return

        minutes, seconds = divmod(s.eligible_seconds, 60)
        gross = s.gross_half_units / 2
        net = s.net_half_units / 2

        if s.hardcore_at_start and not s.hardcore_revoked:
            mode = "🔥 Hardcore"
        elif s.hardcore_at_start and s.hardcore_revoked:
            mode = "🔥 Hardcore → 🛡️ Normal (revoked)"
        else:
            mode = "🛡️ Normal"

        embed = discord.Embed(
            title="✈️ Dynamic Campaign Flight Receipt",
            color=discord.Color.blue()
        )
        embed.add_field(name="Pilot", value=s.player_name, inline=False)
        embed.add_field(name="Mode", value=mode, inline=False)
        embed.add_field(name="Eligible Time", value=f"{minutes}m {seconds}s", inline=True)
        embed.add_field(name="Billable Blocks", value=f"{s.completed_blocks} × {block_minutes}m", inline=True)
        embed.add_field(name="Losses", value=str(s.deaths), inline=True)
        embed.add_field(name="Gross", value=f"{gross:g} cr", inline=True)
        embed.add_field(name="Penalty", value=f"-{s.penalty_percent}%", inline=True)
        embed.add_field(name="Net Earned", value=f"{net:g} cr", inline=True)
        embed.add_field(name="Deposited", value=f"{s.paid_credits} cr", inline=True)
        embed.add_field(name="Previous Balance", value=f"{s.old_balance:,} cr", inline=True)
        if s.carry_half_units:
            embed.add_field(name="Fractional Carry", value="0.5 cr", inline=True)
        embed.add_field(name="New Balance", value=f"{s.new_balance:,} cr", inline=False)

        try:
            await member.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            self.log.warning("Hardcore: could not DM settlement receipt to UCID %s", s.player_ucid)

    async def status_for(self, server: Server, ucid: str) -> dict:
        campaign_id, campaign_name = await self.running_campaign(server)
        balance = await self.get_balance(campaign_id, ucid) if campaign_id else 0
        active = await self.is_hardcore(ucid)

        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT eligible_seconds,
                       segment_started_at,
                       deaths,
                       hardcore_at_start,
                       hardcore_revoked,
                       airborne
                FROM hardcore_sessions
                WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
            """, (server.name, ucid))
            row = await cursor.fetchone()

        session = None
        if row:
            eligible_seconds, segment_started_at, deaths, hc_start, hc_revoked, airborne = row
            seconds = int(eligible_seconds)
            if segment_started_at is not None:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                seconds += max(0, int((now - segment_started_at).total_seconds()))
            session = {
                "eligible_seconds": seconds,
                "deaths": int(deaths),
                "hardcore_at_start": bool(hc_start),
                "hardcore_revoked": bool(hc_revoked),
                "airborne": bool(airborne),
            }

        return {
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "balance": balance,
            "hardcore": active,
            "session": session,
        }

    async def reconcile_server(self, server: Server) -> None:
        """
        Called after DCSServerBot registers/re-registers a DCS server.

        For open sessions belonging to players that are no longer connected,
        stop the open segment at last_seen_at and settle conservatively.
        For players still connected, resume time from 'now' rather than awarding
        bot/DCS downtime.
        """
        if not self.enabled(server):
            return

        active_ucids = {p.ucid for p in server.get_active_players() if getattr(p, "ucid", None)}
        stale_to_settle: list[str] = []

        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT player_ucid
                FROM hardcore_sessions
                WHERE server_name = %s AND settled = FALSE
            """, (server.name,))
            rows = await cursor.fetchall()

            for (ucid,) in rows:
                if ucid in active_ucids:
                    await conn.execute("""
                        UPDATE hardcore_sessions
                        SET segment_started_at =
                                CASE WHEN segment_started_at IS NULL THEN NULL
                                     ELSE (NOW() AT TIME ZONE 'utc')
                                END,
                            last_seen_at = (NOW() AT TIME ZONE 'utc')
                        WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
                    """, (server.name, ucid))
                else:
                    await conn.execute("""
                        UPDATE hardcore_sessions
                        SET eligible_seconds = eligible_seconds +
                            CASE
                                WHEN segment_started_at IS NULL THEN 0
                                ELSE GREATEST(
                                    0,
                                    FLOOR(EXTRACT(EPOCH FROM (
                                        last_seen_at - segment_started_at
                                    )))::BIGINT
                                )
                            END,
                            segment_started_at = NULL,
                            airborne = FALSE
                        WHERE server_name = %s AND player_ucid = %s AND settled = FALSE
                    """, (server.name, ucid))
                    stale_to_settle.append(ucid)

        for ucid in stale_to_settle:
            try:
                await self.settle(server, ucid)
            except Exception:
                self.log.exception("Hardcore: failed to settle stale session for %s", ucid)
