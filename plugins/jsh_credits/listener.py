import asyncio
import json
import os

from collections import Counter
from typing import Any, Optional, cast

from core import EventListener, Server, Status, event, chat_command
from plugins.creditsystem.player import CreditPlayer

#: One UDP datagram. DCS reads with LuaSocket's 8192-byte default and
#: truncates anything larger without a word in either log.
DATAGRAM_LIMIT = 8192
DATAGRAM_MARGIN = 400


class CreditsEventListener(EventListener["Credits"]):
    """
    Kill credits go into a per-player pending bucket instead of straight onto
    their balance. The bucket is paid out when the mission-side hook reports a
    landing at a friendly airbase or FARP, and dropped on crash, eject, pilot
    death, slot change, disconnect or mission end.

    Kills are all this plugin credits. Mission awards (CSAR, logistics) are
    credited by CreditSystem and labelled by the plugin that earned them, so
    they keep their own credits_log events. Requires points_on_rtb: false.

    Two records are kept:
      - credits_log (CreditSystem's): one 'rtb' row per landing.
      - jsh_credits_events (ours): one row per kill, with a 'paid' flag so
        credits lost to a death are still visible.

    Nothing here touches CreditPlayer.deposit except to zero it, so SlotBlocking
    can never pay out or clear anything behind our back.
    """

    def __init__(self, plugin):
        super().__init__(plugin)
        # key -> {'points': int,
        #         'reasons': {label: points},
        #         'detail':  {label: Counter(unit_type)},
        #         'rows':    [jsh_credits_events.id, ...]}
        self.pending: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        # asyncio keeps only a WEAK reference to a running task, so a task that
        # nothing else holds can be garbage-collected before it finishes. The
        # injection was spawned bare, and registration is the busiest moment in
        # the loop -- which is how 1.7 could be correctly installed, pass its
        # size check, and still never reach the mission.
        self._tasks: set = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _config(self, server: Server) -> Optional[dict]:
        """Merged DEFAULT + per-server config, or None if disabled here."""
        config = self.plugin.get_config(server)
        if not config or not config.get('enabled', True):
            return None
        return config

    @staticmethod
    def _instance(server: Server) -> str:
        """
        Instance name (e.g. DCS.dcs_serverrelease), which is what plugin configs
        are keyed on. Falls back to the server name if no instance is attached.
        """
        instance = getattr(server, 'instance', None)
        return getattr(instance, 'name', None) or server.name

    def _key(self, server: Server, ucid: str) -> str:
        return f"{self._instance(server)}:{ucid}"

    @staticmethod
    def _message(config: dict, name: str, default: str = '') -> str:
        return config.get('messages', {}).get(name, default)

    @staticmethod
    def _partial_event(config: dict, why: str) -> str:
        """
        credits_log event for a sortie that ended badly, named after how it
        ended -- 'ejected', 'crashed', 'died' -- so the log reads as what
        happened rather than as bookkeeping.
        """
        mapping = config.get('partial_events') or {}
        return mapping.get(why) or config.get('default_partial_event', 'salvaged')

    @staticmethod
    def _new_bucket() -> dict[str, Any]:
        # refuel_lbs is the pounds already paid for this sortie, for the
        # refuelling cap. It rides here because the bucket is created, dropped
        # and paid out on exactly the sortie boundaries the cap needs.
        return {'points': 0, 'reasons': {}, 'detail': {}, 'rows': [], 'refuel_lbs': 0}

    # ------------------------------------------------------------------
    # ledger (jsh_credits_events)
    # ------------------------------------------------------------------

    async def _log_event(self, server: Server, player: CreditPlayer, *, source: str,
                         points: int, unit_type: str = None, category: str = None,
                         reason: str = None) -> Optional[int]:
        """Record one earning event as unpaid. Returns its row id."""
        try:
            async with self.apool.connection() as conn:
                async with conn.transaction():
                    cursor = await conn.execute("""
                        INSERT INTO jsh_credits_events
                            (instance, player_ucid, source, unit_type, category,
                             reason, points, paid)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                        RETURNING id
                    """, (self._instance(server), player.ucid, source, unit_type,
                          category, reason, points))
                    row = await cursor.fetchone()
                    return row[0] if row else None
        except Exception as ex:
            # the ledger is for analysis: never let it break crediting
            self.log.error(f"JSH Credits: could not log event: {ex}")
            return None

    async def _mark_paid(self, row_ids: list[int], place: str) -> None:
        """Flip a set of ledger rows to paid."""
        if not row_ids:
            return
        try:
            async with self.apool.connection() as conn:
                async with conn.transaction():
                    await conn.execute("""
                        UPDATE jsh_credits_events
                           SET paid = TRUE,
                               paid_time = (now() AT TIME ZONE 'utc'),
                               paid_place = %s
                         WHERE id = ANY(%s)
                    """, (place or None, row_ids))
        except Exception as ex:
            self.log.error(f"JSH Credits: could not mark events paid: {ex}")

    # ------------------------------------------------------------------
    # pending bucket
    # ------------------------------------------------------------------

    async def _hold(self, server: Server, player: CreditPlayer, points: int, label: str,
                    *, source: str = 'mission', unit_type: str = None,
                    category: str = None) -> None:
        """Add points to a player's pending bucket. Never touches their balance."""
        config = self._config(server)
        if not config or not player or points <= 0:
            return

        row_id = await self._log_event(
            server, player, source=source, points=points, unit_type=unit_type,
            category=category, reason=None if source == 'kill' else label)

        key = self._key(server, player.ucid)
        async with self.lock:
            bucket = self.pending.setdefault(key, self._new_bucket())
            bucket['points'] += points
            bucket['reasons'][label] = bucket['reasons'].get(label, 0) + points
            if unit_type:
                bucket['detail'].setdefault(label, Counter())[unit_type] += 1
            if row_id:
                bucket['rows'].append(row_id)
            total = bucket['points']

        self.log.debug(f"JSH Credits: held {points} for {player.name} ({label}), "
                       f"pending {total} on {server.name}")

        message = self._message(config, 'held')
        if message:
            await player.sendUserMessage(message.format(
                points=points, total=total, reason=label))

    async def _drop(self, server: Server, ucid: str, why: str) -> None:
        """
        End a sortie that never made it home.

        `death_payout` decides how much of the pending bucket the pilot keeps
        anyway -- 0.25 leaves them a quarter of what they earned, 0 is the
        all-or-nothing behaviour. The rest is gone.

        Ledger rows stay unpaid either way: they were earned but never landed
        with, so "earned vs collected" still measures what RTB collected. The
        partial is its own credits_log row instead.
        """
        key = self._key(server, ucid)
        async with self.lock:
            bucket = self.pending.pop(key, None)
        if not bucket or bucket['points'] <= 0:
            return

        config = self._config(server)
        if not config:
            return

        pending = bucket['points']
        keep = float(config.get('death_payout', 0.25))
        kept = int(round(pending * max(0.0, min(1.0, keep))
                         * config.get('multiplier', 1.0))) if keep > 0 else 0

        player = cast(CreditPlayer, server.get_player(ucid=ucid))

        if kept > 0 and player:
            old_points = player.points
            player.points += kept
            detail = self._remark(bucket, config.get('remark_detail', 3))
            remark = f"kept {kept} of {pending} pending after {why} ({detail})"
            max_len = int(config.get('remark_max_length', 200))
            if len(remark) > max_len:
                remark = remark[:max_len - 3] + '...'
            await player.audit(self._partial_event(config, why), old_points, remark)
            self.log.info(f"JSH Credits: {player.name} kept {kept} of {pending} "
                          f"pending on {server.name} ({why})")
        else:
            self.log.debug(f"JSH Credits: dropped {pending} pending for "
                           f"{ucid} on {server.name} ({why})")

        if not player:
            return
        name = 'salvaged' if kept > 0 else 'lost'
        message = self._message(config, name)
        if message:
            await player.sendUserMessage(message.format(
                points=pending, kept=kept, lost=pending - kept, reason=why))

    async def _drop_server(self, server: Server, why: str) -> None:
        """Discard every pending bucket on one server."""
        prefix = f"{self._instance(server)}:"
        async with self.lock:
            keys = [k for k in self.pending if k.startswith(prefix)]
            for key in keys:
                self.pending.pop(key, None)
        if keys:
            self.log.debug(f"JSH Credits: cleared {len(keys)} pending bucket(s) "
                           f"on {server.name} ({why})")

    # ------------------------------------------------------------------
    # remark rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _detail_str(counter: Counter, limit: int) -> str:
        """'5x SA-18 Igla manpad, 3x tt_ZU-23, 17x Infantry AK, +4 more'"""
        if not counter or limit <= 0:
            return ''
        top = counter.most_common(limit)
        parts = [f"{count}x {unit}" for unit, count in top]
        remaining = sum(counter.values()) - sum(count for _, count in top)
        if remaining > 0:
            parts.append(f"+{remaining} more")
        return ', '.join(parts)

    def _remark(self, bucket: dict, limit: int = 3) -> str:
        """'kills (86: 5x SA-18 Igla manpad, +28 more), CSAR: 5x Unknown (20)'"""
        parts = []
        for label, points in bucket['reasons'].items():
            detail = self._detail_str(bucket['detail'].get(label), limit)
            if detail:
                parts.append(f"{label} ({points}: {detail})")
            else:
                parts.append(f"{label} ({points})")
        return ', '.join(parts) if parts else 'RTB'

    # ------------------------------------------------------------------
    # payout
    # ------------------------------------------------------------------

    async def _payout(self, server: Server, player: CreditPlayer, place: str) -> None:
        """Move a pending bucket onto the player's balance, with an audit row."""
        config = self._config(server)
        if not config or not player:
            return

        key = self._key(server, player.ucid)
        async with self.lock:
            bucket = self.pending.pop(key, None)
        if not bucket or bucket['points'] <= 0:
            return

        points = int(round(bucket['points'] * config.get('multiplier', 1.0)))
        if points <= 0:
            return

        detail = self._remark(bucket, config.get('remark_detail', 3))
        remark = f"RTB {place}: {detail}" if place else f"RTB: {detail}"
        # credits_log.remark is TEXT, but the Discord embed truncates: keep it sane
        max_len = int(config.get('remark_max_length', 200))
        if len(remark) > max_len:
            remark = remark[:max_len - 3] + '...'

        old_points = player.points
        player.points += points
        await player.audit('rtb', old_points, remark)
        await self._mark_paid(bucket['rows'], place)
        # CreditSystem may have written a deposit we don't use; never pay it
        player.deposit = 0

        self.log.info(f"JSH Credits: paid {player.name} {points} on {server.name} ({remark})")

        message = self._message(config, 'paid')
        if message:
            await player.sendUserMessage(message.format(
                points=points, old_points=old_points, new_points=player.points,
                place=place or 'base', reason=detail))

    # ------------------------------------------------------------------
    # kill scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _victim_category(data: dict) -> str:
        return data.get('victimCategory') or data.get('category') or ''

    def _points_per_kill(self, config: dict, data: dict, victim_is_player: bool) -> int:
        """
        Walk points_per_kill top to bottom and return the first match, exactly
        like CreditSystem does: order entries from most specific to least.

        Supported keys per entry: category, type (Player/AI), unit_type, points,
        default.
        """
        unit_type = data.get('arg5', '')
        category = self._victim_category(data)

        for entry in config.get('points_per_kill', []):
            if 'default' in entry:
                return int(entry['default'])
            if 'category' in entry and entry['category'] != category:
                continue
            if 'unit_type' in entry and entry['unit_type'] != unit_type:
                continue
            if 'type' in entry:
                wants_player = str(entry['type']).lower() == 'player'
                if wants_player != victim_is_player:
                    continue
            return int(entry.get('points', 0))
        return 0

    # ------------------------------------------------------------------
    # mission-side hook
    # ------------------------------------------------------------------

    async def _inject_lua(self, server: Server) -> None:
        """
        Push the mission hooks into the running mission.

        The script goes out as a single UDP datagram: send_to_dcs ends in
        transport.sendto, and the DCS side reads it with UDPRecvSocket:receive()
        called with no size argument -- LuaSocket's 8192-byte default. A larger
        payload is TRUNCATED, not rejected. The JSON then fails to parse, no
        handler runs, and absolutely nothing is logged on either side.

        That is not hypothetical. Version 1.6 grew the file to an 8424-byte
        datagram, and every injection silently did nothing: the sampler was
        never registered, no error appeared in dcs.log, and the only symptom was
        that refuelling paid nobody. Mission-load injection of 1.5 had always
        worked because that file was only 5644 bytes.

        So comments are stripped on the wire -- the source keeps them -- and the
        size is checked before sending, loudly, because a silent truncation is
        far worse than a refusal.
        """
        if not self._config(server):
            return
        path = os.path.join(os.path.dirname(__file__), 'lua', 'mission.lua')
        try:
            with open(path, mode='r', encoding='utf-8') as f:
                script = f.read()
        except OSError as ex:
            self.log.error(f"JSH Credits: could not read {path}: {ex}")
            return

        script = self._minify_lua(script)
        message = {'command': 'do_script', 'script': script}
        # Headroom for the fields the bot adds on the way out (server_name and,
        # for a remote server, channel) and for JSON escaping.
        size = len(json.dumps(message).encode()) + DATAGRAM_MARGIN
        if size > DATAGRAM_LIMIT:
            self.log.error(
                f"JSH Credits: mission.lua is too large to inject into {server.name} "
                f"({size} bytes with headroom, limit {DATAGRAM_LIMIT}). DCS would "
                f"truncate it silently and no handler would register. Not sending.")
            return

        await server.send_to_dcs(message)
        # INFO, not DEBUG. This happens twice a mission at most, and when it
        # stops happening that is exactly what you need the log to tell you.
        self.log.info(f"JSH Credits: mission hooks injected into {server.name} "
                      f"({size} bytes)")

    @staticmethod
    def _minify_lua(script: str) -> str:
        """
        Drop whole-line comments and blank lines.

        Only lines whose first non-space characters are `--` are removed, and
        trailing comments are left alone: a `--` inside a string literal would
        otherwise take the rest of the line with it. Long-form `--[[ ]]` blocks
        are not used in this file and are deliberately not handled.
        """
        kept = [line for line in script.splitlines()
                if line.strip() and not line.lstrip().startswith('--')]
        return '\n'.join(kept)

    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        """
        Re-inject into a mission that is already running, so a bot restart is
        enough to pick up a new mission.lua.

        This used to fire only when data['channel'] started with 'sync-', on the
        assumption that the registration reply carries the sync channel through
        to plugin listeners. In practice it does not reliably, and the symptom is
        invisible: the bot loads the new version, reports it migrated, and the
        mission quietly keeps running the old script until someone restarts the
        mission. That cost a deploy of 1.4 -- the refuel handler was on disk and
        loaded by the bot for two days without ever reaching the mission.

        The guard bought nothing anyway. _inject_lua is cheap, and the script
        itself is idempotent: every handler sits behind its own `if not ... then`
        so a second injection registers only what is missing.
        """
        # No status check. DCS is registering, which means DCS is there with a
        # mission loaded -- that is what the hook registers from.
        #
        # Checking server.status here never worked, and the reason is in
        # DCSServerBot itself. plugins/mission/listener.py sets it inside ITS
        # registerDCSServer handler, under a comment reading "Set the status at
        # the latest possible place". Listener order across plugins is not
        # guaranteed, so when this handler runs the status is usually still
        # whatever it was before registration, and a RUNNING/PAUSED test is
        # simply false. Every bot restart silently skipped the injection.
        self._spawn(self._inject_lua(server))

    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, _: dict) -> None:
        await self._drop_server(server, 'mission load')
        self._spawn(self._inject_lua(server))

    @event(name="jshCreditsLanding")
    async def jshCreditsLanding(self, server: Server, data: dict) -> None:
        """
        Sent by lua/mission.lua on S_EVENT_LAND. The mission has already checked
        that the landing place exists and belongs to the pilot's own coalition,
        so anything arriving here is a valid RTB.
        """
        if not self._config(server):
            return
        player = cast(CreditPlayer, server.get_player(name=data.get('name'), active=True))
        if not player:
            self.log.debug(f"JSH Credits: landing for unknown player {data.get('name')}")
            return
        await self._payout(server, player, data.get('place', ''))

    @event(name="jshCreditsRefuel")
    async def jshCreditsRefuel(self, server: Server, data: dict) -> None:
        """
        Sent by lua/mission.lua when a pilot comes off a tanker, with the pounds
        taken. Held like a kill, so refuelling is collected on landing and goes
        the same way as everything else if the sortie ends badly.

        Fuel is counted in thousands of pounds, so the rate is easy to set far
        too high -- see the comparison table in jsh_credits.yaml.
        """
        config = self._config(server)
        if not config or server.status != Status.RUNNING:
            return
        refuel = config.get('refuelling') or {}
        if not refuel.get('enabled', True):
            return

        try:
            lbs = int(data.get('lbs') or 0)
        except (TypeError, ValueError):
            return
        if lbs <= 0:
            return

        player = cast(CreditPlayer, server.get_player(name=data.get('name'), active=True))
        if not player:
            self.log.debug(f"JSH Credits: refuelling for unknown player {data.get('name')}")
            return

        block = max(1, int(refuel.get('block_lbs', 1000)))
        per_block = int(refuel.get('credits_per_block', 2))
        minimum = max(0, int(refuel.get('minimum_lbs', 500)))
        cap = max(0, int(refuel.get('max_lbs_per_sortie', 0)))

        if lbs < minimum:
            return

        payable = lbs
        if cap:
            # Fuel costs the pilot nothing, so one sortie could otherwise orbit a
            # tanker, burn it off and tank again indefinitely. The counter rides
            # in the pending bucket, which is already created, dropped and paid
            # out on exactly the sortie boundaries this needs.
            key = self._key(server, player.ucid)
            async with self.lock:
                bucket = self.pending.setdefault(key, self._new_bucket())
                already = bucket.get('refuel_lbs', 0)
                payable = max(0, min(lbs, cap - already))
                if payable > 0:
                    bucket['refuel_lbs'] = already + payable
            if payable <= 0:
                self.log.debug(f"JSH Credits: {player.name} is at the {cap} lb refuelling "
                               f"cap for this sortie, {lbs} lbs unpaid")
                return

        # Whole blocks only, so the rate reads the way it is written: 2 credits
        # per 1000 lbs pays nothing for the first 999.
        points = (payable // block) * per_block
        if points <= 0:
            return

        await self._hold(server, player, points, refuel.get('label', 'refuelling'),
                         source='refuel', unit_type=data.get('aircraft') or None)

    # ------------------------------------------------------------------
    # earning
    # ------------------------------------------------------------------

    @event(name="onGameEvent")
    async def onGameEvent(self, server: Server, data: dict) -> None:
        config = self._config(server)
        if not config or server.status != Status.RUNNING:
            return

        name = data['eventName']

        if name == 'kill':
            # no AI killers, no self-kills, no team-kills
            if data['arg1'] == -1 or data['arg1'] == data['arg4'] or data['arg3'] == data['arg6']:
                return
            killer = server.get_player(id=data['arg1'])
            if not killer:
                return
            victim_is_player = data['arg4'] != -1 and server.get_player(id=data['arg4']) is not None
            points = self._points_per_kill(config, data, victim_is_player)
            if points <= 0:
                return
            unit_type = data.get('arg5', '')
            category = self._victim_category(data)
            # multicrew: pilot and crew each get the points, as CreditSystem does
            for player in server.get_crew_members(killer):  # type: CreditPlayer
                await self._hold(server, player, points, 'kills', source='kill',
                                 unit_type=unit_type, category=category)

        elif name in ('crash', 'eject', 'pilot_death', 'self_kill'):
            player = server.get_player(id=data.get('arg1'))
            if player:
                await self._drop(server, player.ucid, name)

        elif name == 'disconnect':
            player = server.get_player(id=data.get('arg1'))
            if player:
                await self._drop(server, player.ucid, 'disconnect')

        elif name == 'mission_end':
            await self._drop_server(server, 'mission end')

    @event(name="onPlayerChangeSlot")
    async def onPlayerChangeSlot(self, server: Server, data: dict) -> None:
        if not self._config(server) or data.get('id') == 1:
            return
        ucid = data.get('ucid')
        if ucid:
            await self._drop(server, ucid, 'slot change')

    @event(name="addUserPoints")
    async def addUserPoints(self, server: Server, data: dict) -> None:
        """
        Mission awards (jsh_csar, flight economy) are none of our business: with
        points_on_rtb: false, CreditSystem credits them and the plugin that
        earned them labels its own credits_log row.

        The one thing we do is clear the deposit CreditSystem writes alongside
        the credit. With SlotBlocking payback off nothing ever pays it out, so
        left alone it shows as a phantom "on deposit" figure in -credits.
        """
        if not self._config(server):
            return
        player = cast(CreditPlayer, server.get_player(name=data.get('name')))
        if player and player.deposit:
            player.deposit = 0

    # ------------------------------------------------------------------
    # in-game query
    # ------------------------------------------------------------------

    @chat_command(name="pending", help="Shows credits you have not landed with yet")
    async def pending_cmd(self, server: Server, player: CreditPlayer, _params: list[str]):
        config = self._config(server)
        if not config:
            await player.sendChatMessage("Pending credits are not active on this server.")
            return
        bucket = self.pending.get(self._key(server, player.ucid))
        if not bucket or bucket['points'] <= 0:
            await player.sendChatMessage(
                f"You have {player.points} credits and nothing pending.")
            return
        await player.sendChatMessage(
            f"You have {player.points} credits, plus {bucket['points']} pending "
            f"({self._remark(bucket, config.get('remark_detail', 3))}). "
            f"Land at a friendly base to collect.")