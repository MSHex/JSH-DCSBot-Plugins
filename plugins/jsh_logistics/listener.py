import asyncio

from core import EventListener, Server, event
from typing import TYPE_CHECKING

from . import rewards

if TYPE_CHECKING:
    from .commands import Logistics


class LogisticsEventListener(EventListener["Logistics"]):

    def __init__(self, plugin: "Logistics"):
        super().__init__(plugin)
        # Airtime seconds not yet worth a payout, per server and player name,
        # carried to the next landing. Lost on a bot restart: at most a few
        # credits short of a minute's worth per pilot.
        self.carry: dict[tuple[str, str], float] = {}
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        # A listener holds up every other listener on its server until it
        # returns, so the database writes run on their own.
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _load(self, server: Server) -> None:
        if not self.plugin.enabled(server):
            return
        await server.send_to_dcs({"command": "jshLogisticsLoad"})
        await self.plugin.configure(server)

    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, data: dict) -> None:
        await self._load(server)

    # Covers a bot restart with a mission running. Loading twice is safe.
    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        # No status check -- see jsh_credits 1.9 and CLAUDE.md.
        await self._load(server)

    # Foothold banked a landing: data = {"player": name, "stats": {stat: count}}
    @event(name="jshLogisticsBanked")
    async def jshLogisticsBanked(self, server: Server, data: dict) -> None:
        if not self.plugin.enabled(server):
            return
        name, stats = data.get('player'), data.get('stats')
        if not name or not isinstance(stats, dict):
            return
        total, lines = rewards.deliveries(stats, self.plugin.delivery_table(server))
        if not lines:
            return
        player = server.get_player(name=name)
        ucid = player.ucid if player else None
        await self.plugin.pay(server, name, total, f"for logistics: {rewards.phrase(lines)}")
        for stat, count, points in lines:
            self._spawn(self.plugin.record(server, ucid, name, 'delivery', stat, count, points))

    # Landed after flying a logistics aircraft: {"player", "seconds", "type"}
    @event(name="jshLogisticsAirtime")
    async def jshLogisticsAirtime(self, server: Server, data: dict) -> None:
        if not self.plugin.enabled(server):
            return
        name = data.get('player')
        try:
            seconds = float(data.get('seconds') or 0)
        except (TypeError, ValueError):
            return
        if not name or seconds <= 0:
            return
        cfg = self.plugin.airtime_config(server)
        key = (server.name, name)
        points, left = rewards.airtime(seconds, self.carry.get(key, 0.0),
                                       float(cfg.get('minutes', 1)), int(cfg.get('credits', 1)))
        self.carry[key] = left
        player = server.get_player(name=name)
        ucid = player.ucid if player else None
        if points > 0:
            minutes = seconds / 60.0
            await self.plugin.pay(server, name, points,
                                  f"for {minutes:.0f} min flying {data.get('type') or 'logistics'}")
        # Recorded whatever was paid, so the hours stay complete.
        self._spawn(self.plugin.record(server, ucid, name, 'airtime',
                                       str(data.get('type') or ''), int(seconds), points))
