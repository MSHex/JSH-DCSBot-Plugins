from __future__ import annotations

import json
from psycopg.rows import dict_row
from .policy import earnings, loss_decision
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

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
    aircraft_seconds: int
    support_seconds: int
    aircraft_half: int
    support_half: int
    events: dict
    ejections_used: int


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
                    changed_by = EXCLUDED.changed_by,
                    ejections_used = CASE WHEN EXCLUDED.active AND NOT hardcore_status.active THEN 0 ELSE hardcore_status.ejections_used END
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
                        changed_by = EXCLUDED.changed_by,
                        ejections_used = 0
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

    @staticmethod
    async def _close_segment(conn, session_id, cutoff=None):
        await conn.execute("""
            UPDATE hardcore_sessions SET
                aircraft_seconds = aircraft_seconds + CASE WHEN segment_role = 'aircraft'
                    AND segment_started_at IS NOT NULL THEN GREATEST(0, FLOOR(EXTRACT(EPOCH FROM
                    (COALESCE(%s::timestamp, timezone('utc', clock_timestamp())) - segment_started_at))))::bigint ELSE 0 END,
                support_seconds = support_seconds + CASE WHEN segment_role = 'support'
                    AND segment_started_at IS NOT NULL THEN GREATEST(0, FLOOR(EXTRACT(EPOCH FROM
                    (COALESCE(%s::timestamp, timezone('utc', clock_timestamp())) - segment_started_at))))::bigint ELSE 0 END,
                segment_started_at = NULL
            WHERE id = %s
        """, (cutoff, cutoff, session_id))
        await conn.execute('UPDATE hardcore_sessions SET eligible_seconds = aircraft_seconds + support_seconds WHERE id = %s', (session_id,))

    async def observe(self, server, ucid, name, data):
        role = data.get('role', 'spectator')
        if role not in {'aircraft', 'support', 'spectator'}:
            return
        if role != 'spectator':
            await self.open_session(server, ucid, name)
        async with self.apool.connection() as conn:
            async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute('SELECT * FROM hardcore_sessions WHERE server_name=%s AND player_ucid=%s AND NOT settled FOR UPDATE', (server.name, ucid))
                row = await cur.fetchone()
                if not row:
                    return
                if row['telemetry_epoch'] == data['epoch'] and row['telemetry_seq'] >= int(data['seq']):
                    return
                if row['segment_role'] != role:
                    await self._close_segment(conn, row['id'])
                await conn.execute("""
                    UPDATE hardcore_sessions SET segment_role=%s,
                        segment_started_at = CASE WHEN %s='spectator' THEN NULL
                            ELSE COALESCE(segment_started_at, timezone('utc', clock_timestamp())) END,
                        airborne=%s, protected=%s, protection_place=%s,
                        telemetry_at=timezone('utc', clock_timestamp()), last_seen_at=timezone('utc', clock_timestamp()),
                        telemetry_seq=%s, telemetry_epoch=%s
                    WHERE id=%s
                """, (role, role, data.get('airborne') is True, data.get('protected'), data.get('place'),
                      data['seq'], data['epoch'], row['id']))

    async def pause_segment(self, server, ucid):
        async with self.apool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute('SELECT id FROM hardcore_sessions WHERE server_name=%s AND player_ucid=%s AND NOT settled FOR UPDATE', (server.name, ucid))
                row = await cur.fetchone()
                if row:
                    await self._close_segment(conn, row[0])
                    await conn.execute("UPDATE hardcore_sessions SET segment_role='spectator', airborne=FALSE WHERE id=%s", (row[0],))

    async def record_incident(self, server, ucid, data):
        kind = data.get('kind')
        if kind not in {'ejection', 'death', 'reslot'}:
            return False
        event_key = f"{server.name}:{data['epoch']}:{data['seq']}:{ucid}"
        spawn_key = f"{server.name}:{data['epoch']}:{data['spawn']}"
        protected = data.get('protected')
        if protected not in (True, False):
            protected = None
        async with self.apool.connection() as conn:
            async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute('SELECT * FROM hardcore_sessions WHERE server_name=%s AND player_ucid=%s AND NOT settled FOR UPDATE', (server.name, ucid))
                session = await cur.fetchone()
                if not session:
                    return False
                # One terminal incident per aircraft incarnation and crew member,
                # including across a reconnect. Safe reslots are not terminal.
                await cur.execute("""
                    SELECT 1 FROM hardcore_events e JOIN hardcore_sessions s ON s.id=e.session_id
                    WHERE s.player_ucid=%s AND e.spawn_key=%s
                    AND (e.event_type IN ('ejection','death') OR (e.event_type='reslot' AND e.counted))
                """, (ucid, spawn_key))
                if await cur.fetchone():
                    return False
                await cur.execute('SELECT active,ejections_used FROM hardcore_status WHERE player_ucid=%s FOR UPDATE', (ucid,))
                status = await cur.fetchone() or {'active': False, 'ejections_used': 0}
                count, revoke, used = loss_decision(kind, protected, status['active'], status['ejections_used'])
                await cur.execute("""
                    INSERT INTO hardcore_events(session_id,event_type,details,event_key,spawn_key,protected,counted)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id
                """, (session['id'], kind, json.dumps(data, ensure_ascii=False), event_key, spawn_key, protected, count))
                if not await cur.fetchone():
                    return False
                if count:
                    await conn.execute("""
                        UPDATE hardcore_sessions SET deaths=deaths+1,
                            hardcore_revoked=hardcore_revoked OR %s,
                            last_loss_at=timezone('utc',clock_timestamp()),last_loss_reason=%s
                        WHERE id=%s
                    """, (revoke, kind, session['id']))
                if status['active'] and count:
                    await conn.execute("""
                        UPDATE hardcore_status SET active=%s,ejections_used=%s,
                            changed_at=CASE WHEN %s THEN timezone('utc',clock_timestamp()) ELSE changed_at END,
                            changed_by=CASE WHEN %s THEN %s ELSE changed_by END WHERE player_ucid=%s
                    """, (not revoke, used, revoke, revoke, kind.upper(), ucid))
                if kind != 'reslot':
                    # Do not close the new aircraft's reward clock for an old delayed death.
                    if data.get('current', True):
                        await self._close_segment(conn, session['id'])
                        await conn.execute("UPDATE hardcore_sessions SET segment_role='spectator' WHERE id=%s", (session['id'],))
        self.log.info('Hardcore incident | pilot=%s | ucid=%s | kind=%s | protected=%s | counted=%s | revoked=%s | place=%s',
                      session['player_name'], ucid, kind, protected, count, revoke, data.get('place'))
        return count

    @staticmethod
    def penalty_percent(deaths, config):
        levels = {int(k): int(v) for k, v in config.get('death_penalties', {0: 0, 1: 20, 2: 50, 3: 90}).items()}
        return levels[max((k for k in levels if k <= deaths), default=min(levels))]

    async def settle(self, server, ucid):
        cfg = self.cfg(server)
        async with self.apool.connection() as conn:
            async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute('SELECT * FROM hardcore_sessions WHERE server_name=%s AND player_ucid=%s AND NOT settled FOR UPDATE', (server.name, ucid))
                row = await cur.fetchone()
                if not row:
                    return None
                await self._close_segment(conn, row['id'])
                await cur.execute('SELECT * FROM hardcore_sessions WHERE id=%s', (row['id'],))
                row = await cur.fetchone()
                penalty = self.penalty_percent(row['deaths'], cfg)
                earned = earnings(row['aircraft_seconds'], row['support_seconds'], cfg.get('block_minutes', 15),
                                  cfg.get('normal_reward', 25), cfg.get('hardcore_reward', 37.5),
                                  row['hardcore_at_start'] and not row['hardcore_revoked'], penalty)
                # Ensure wallets exist before locking, also safe on first-ever credit.
                await conn.execute('INSERT INTO credits(campaign_id,player_ucid,points) VALUES (%s,%s,0) ON CONFLICT DO NOTHING', (row['campaign_id'], ucid))
                await cur.execute('SELECT points FROM credits WHERE campaign_id=%s AND player_ucid=%s FOR UPDATE', (row['campaign_id'], ucid))
                old = (await cur.fetchone())['points']
                await conn.execute('INSERT INTO hardcore_wallet_carry(campaign_id,player_ucid,half_credit_units) VALUES (%s,%s,0) ON CONFLICT DO NOTHING', (row['campaign_id'], ucid))
                await cur.execute('SELECT half_credit_units FROM hardcore_wallet_carry WHERE campaign_id=%s AND player_ucid=%s FOR UPDATE', (row['campaign_id'], ucid))
                carry = (await cur.fetchone())['half_credit_units']
                payout, carry = divmod(earned.net_half + carry, 2)
                credit_plugin = self.bot.cogs.get('CreditSystem')
                maximum = credit_plugin.get_config(server).get('max_points') if credit_plugin else None
                if maximum is not None:
                    payout = min(payout, max(0, int(maximum) - old))
                new = old + payout
                await conn.execute('UPDATE credits SET points=%s WHERE campaign_id=%s AND player_ucid=%s', (new, row['campaign_id'], ucid))
                if payout:
                    await conn.execute("""INSERT INTO credits_log(campaign_id,event,player_ucid,old_points,new_points,remark)
                        VALUES (%s,'flight economy',%s,%s,%s,%s)""",
                        (row['campaign_id'], ucid, old, new,
                         f"Aircraft blocks={earned.aircraft_blocks}; support blocks={earned.support_blocks}; losses={row['deaths']}; aircraft penalty={penalty}%"))
                await conn.execute('UPDATE hardcore_wallet_carry SET half_credit_units=%s WHERE campaign_id=%s AND player_ucid=%s', (carry, row['campaign_id'], ucid))
                await cur.execute('SELECT ejections_used FROM hardcore_status WHERE player_ucid=%s', (ucid,))
                status = await cur.fetchone()
                used = status['ejections_used'] if status else 0
                await conn.execute("""
                    UPDATE hardcore_sessions SET settled=TRUE,airborne=FALSE,payout=%s,
                        gross_half_units=%s,aircraft_gross_half_units=%s,support_gross_half_units=%s,
                        penalty_percent=%s,ejections_used_at_end=%s,settled_at=timezone('utc',clock_timestamp()) WHERE id=%s
                """, (payout, earned.aircraft_half + earned.support_half, earned.aircraft_half,
                      earned.support_half, penalty, used, row['id']))
                await cur.execute('SELECT event_type,protected,counted,COUNT(*) AS n FROM hardcore_events WHERE session_id=%s GROUP BY event_type,protected,counted', (row['id'],))
                counts = {(e['event_type'], e['protected'], e['counted']): e['n'] for e in await cur.fetchall()}
        s = Settlement(row['id'], ucid, row['player_name'] or ucid, row['campaign_id'],
                       row['eligible_seconds'], earned.aircraft_blocks + earned.support_blocks,
                       row['deaths'], row['hardcore_at_start'], row['hardcore_revoked'],
                       earned.aircraft_half + earned.support_half, penalty, earned.net_half,
                       payout, carry, old, new, row['aircraft_seconds'], row['support_seconds'],
                       earned.aircraft_half, earned.support_half, counts, used)
        self._sync_live_player_balance(server, ucid, new)
        self.log.info('Hardcore settlement | pilot=%s | session=%s | aircraft=%ss | support=%ss | losses=%s | payout=%s | revoked=%s',
                      s.player_name, s.session_id, s.aircraft_seconds, s.support_seconds, s.deaths, payout, s.hardcore_revoked)
        if cfg.get('dm_receipt', True):
            await self.send_receipt(s, cfg.get('block_minutes', 15))
        return s

    async def send_receipt(self, s, block_minutes):
        member = await self.bot.get_member_by_ucid(s.player_ucid)
        if not member:
            return
        mode = 'Hardcore → Normal (revoked)' if s.hardcore_revoked else ('Hardcore retained' if s.hardcore_at_start else 'Normal')
        if s.hardcore_at_start and not s.hardcore_revoked:
            mode += ' — first ejection allowance ' + ('used' if s.ejections_used else 'available')
        def count(kind, protected=None, counted=None):
            return sum(n for (k,p,c),n in s.events.items() if k == kind and (protected is None or p == protected) and (counted is None or c == counted))
        embed = discord.Embed(title='Campaign Session Receipt', color=discord.Color.blue())
        embed.add_field(name='Pilot', value=s.player_name, inline=False)
        embed.add_field(name='Mode', value=mode, inline=False)
        embed.add_field(name='Aircraft participation', value=f'{s.aircraft_seconds // 60}m {s.aircraft_seconds % 60}s\nGross: {s.aircraft_half / 2:g} cr', inline=True)
        embed.add_field(name='Support participation · Normal rate', value=f'{s.support_seconds // 60}m {s.support_seconds % 60}s\nGross: {s.support_half / 2:g} cr', inline=True)
        embed.add_field(name='Deaths / crashes and ejections', value=(
            f"Deaths/crashes: {count('death', counted=True)} counted · {count('death', protected=True)} protected\n"
            f"Ejections: {count('ejection', counted=True)} counted · {count('ejection', protected=True)} protected"), inline=False)
        embed.add_field(name='Reslots / respawns', value=f"{count('reslot', counted=True)} unprotected · {count('reslot', protected=True)} friendly-ground safe", inline=False)
        unknown = sum(n for (k,p,c),n in s.events.items() if p is None)
        if unknown:
            embed.add_field(name='Unverified incidents', value=f'{unknown} — logged without automatic penalty', inline=False)
        embed.add_field(name='Payout', value=f'Counted losses: {s.deaths}\nAircraft deduction: {s.penalty_percent}%\nSupport deduction: 0%\nNet earned: {s.net_half_units / 2:g} cr\nDeposited: {s.paid_credits} cr', inline=False)
        embed.add_field(name='Campaign balance', value=f'{s.old_balance:,} → {s.new_balance:,} cr', inline=False)
        embed.set_footer(text=f'Complete {block_minutes}-minute blocks per category; fractional carry: {s.carry_half_units / 2:g} cr')
        try:
            await member.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            self.log.warning('Hardcore: could not DM receipt for %s', s.player_name)

    async def status_for(self, server, ucid):
        campaign_id, campaign_name = await self.running_campaign(server)
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute('SELECT active,ejections_used FROM hardcore_status WHERE player_ucid=%s', (ucid,))
                status = await cur.fetchone() or {'active': False, 'ejections_used': 0}
                await cur.execute('SELECT * FROM hardcore_sessions WHERE server_name=%s AND player_ucid=%s AND NOT settled', (server.name, ucid))
                session = await cur.fetchone()
        if session:
            if session['segment_started_at'] and session['segment_role'] in ('aircraft','support'):
                elapsed = max(0,int((datetime.now(timezone.utc).replace(tzinfo=None) - session['segment_started_at']).total_seconds()))
                session[session['segment_role'] + '_seconds'] += elapsed
            session['eligible_seconds'] = session['aircraft_seconds'] + session['support_seconds']
        return {'campaign_id': campaign_id, 'campaign_name': campaign_name,
                'balance': await self.get_balance(campaign_id, ucid) if campaign_id else 0,
                'hardcore': status['active'], 'ejections_used': status['ejections_used'], 'session': session}

    async def reconcile_server(self, server):
        # Close at last confirmed heartbeat. Resume only upon fresh DCS telemetry;
        # never grant time or protection from a pre-restart slot assumption.
        active = {p.ucid for p in server.get_active_players()}
        stale = []
        async with self.apool.connection() as conn:
            async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                await cur.execute('SELECT id,player_ucid,last_seen_at FROM hardcore_sessions WHERE server_name=%s AND NOT settled FOR UPDATE', (server.name,))
                for row in await cur.fetchall():
                    await self._close_segment(conn,row['id'],row['last_seen_at'])
                    await conn.execute("UPDATE hardcore_sessions SET segment_role='spectator',protected=NULL,airborne=FALSE WHERE id=%s", (row['id'],))
                    if row['player_ucid'] not in active:
                        stale.append(row['player_ucid'])
        for ucid in stale:
            await self.settle(server,ucid)
