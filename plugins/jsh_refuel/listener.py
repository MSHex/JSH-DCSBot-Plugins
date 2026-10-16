import asyncio

from core import EventListener, Server, Status, event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import Refuel

RUNNING = [Status.RUNNING, Status.PAUSED]


class RefuelEventListener(EventListener["Refuel"]):
    """
    Turns a reported fuel transfer into credits.

    The per-sortie cap is kept here rather than in the mission, because the bot
    already sees the events that should reset it -- slot change, death,
    disconnect -- and because a pilot who reconnects should not inherit a cap
    from a sortie that no longer exists.
    """

    def __init__(self, plugin):
        super().__init__(plugin)
        # asyncio holds only a weak reference to a running task.
        self._tasks: set = set()
        # "instance:ucid" -> pounds already paid for this sortie
        self.paid_lbs: dict[str, int] = {}

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _instance(server: Server) -> str:
        """
        Instance name (e.g. DCS.dcs_serverrelease), which is what plugin configs
        are keyed on. Falls back to the server name if no instance is attached,
        the same way jsh_credits does.
        """
        instance = getattr(server, 'instance', None)
        return getattr(instance, 'name', None) or server.name

    def _key(self, server: Server, ucid: str) -> str:
        return f"{self._instance(server)}:{ucid}"

    # ------------------------------------------------------------ mission load
    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, _: dict) -> None:
        if self.plugin.enabled(server):
            await server.send_to_dcs({"command": "jshRefuelLoad"})
            self.paid_lbs.clear()

    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, _: dict) -> None:
        # Covers a bot restart while a mission is already running. mission.lua
        # ignores a second load, so this is safe.
        if self.plugin.enabled(server) and server.status in RUNNING:
            await server.send_to_dcs({"command": "jshRefuelLoad"})

    # ------------------------------------------------------------ sortie reset
    def _reset(self, server: Server, data: dict) -> None:
        ucid = data.get('ucid')
        if not ucid:
            player = server.get_player(id=data.get('id'))
            ucid = player.ucid if player else None
        if ucid:
            self.paid_lbs.pop(self._key(server, ucid), None)

    @event(name="onPlayerChangeSlot")
    async def onPlayerChangeSlot(self, server: Server, data: dict) -> None:
        self._reset(server, data)

    @event(name="onPlayerStop")
    async def onPlayerStop(self, server: Server, data: dict) -> None:
        self._reset(server, data)

    @event(name="onGameEvent")
    async def onGameEvent(self, server: Server, data: dict) -> None:
        # A new airframe is a new sortie, whether it ended well or badly.
        if data.get('eventName') in ('crash', 'eject', 'pilot_death', 'self_kill'):
            player = server.get_player(id=data.get('arg1'))
            if player and getattr(player, 'ucid', None):
                self.paid_lbs.pop(self._key(server, player.ucid), None)

    # ------------------------------------------------------------ the payout
    @event(name="jshRefuelDone")
    async def jshRefuelDone(self, server: Server, data: dict) -> None:
        if not self.plugin.enabled(server):
            return
        name = data.get('player')
        try:
            lbs = int(data.get('lbs') or 0)
        except (TypeError, ValueError):
            return
        if not name or lbs <= 0:
            return

        aircraft = data.get('aircraft')
        player = server.get_player(name=name)
        ucid = getattr(player, 'ucid', None) if player else None
        block, per_block, minimum, cap = self.plugin.rate(server)

        # Below the minimum the transfer is still recorded, just not paid: the
        # totals should reflect fuel taken, not fuel that happened to pay.
        if lbs < minimum:
            await self.plugin.record(server, ucid, name, aircraft, lbs, 0, 0)
            return

        payable = lbs
        if cap and ucid:
            key = self._key(server, ucid)
            already = self.paid_lbs.get(key, 0)
            payable = max(0, min(lbs, cap - already))
            if payable <= 0:
                self.log.debug(f"Refuel: {name} is at the {cap} lb sortie cap, {lbs} lbs unpaid")
                await self.plugin.record(server, ucid, name, aircraft, lbs, 0, 0)
                return
            self.paid_lbs[key] = already + payable

        # Whole blocks only, so the rate reads the way it is written in the
        # config: 5 credits per 100 lbs pays nothing for the first 99.
        points = (payable // block) * per_block
        await self.plugin.record(server, ucid, name, aircraft, lbs, payable, points)
        if points <= 0:
            return

        capped = " (sortie cap reached)" if payable < lbs else ""
        reason = f"Air-to-air refuelling: {payable:,} lbs, +{points} credits{capped}"
        self._spawn(self.plugin.pay(server, name, points, reason, ucid))
