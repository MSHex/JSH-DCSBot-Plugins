import asyncio

from core import EventListener, Server, Status, event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import Airport


class AirportEventListener(EventListener["Airport"]):

    def __init__(self, plugin):
        super().__init__(plugin)
        # asyncio keeps only a weak reference to a running task, so a task that
        # nothing holds can be garbage collected before it finishes. Without
        # this, the refresh after a mission load may simply never run.
        self._tasks: set = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # Load mission.lua on every mission start, but only where the plugin is enabled.
    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server):
            await server.send_to_dcs({"command": "jshAirportLoad"})
            # The airbase list changes with the mission; drop the cache and rebuild
            # it once mission.lua has had time to load.
            self.plugin.airbases.pop(server.name, None)
            self._spawn(self.refresh_later(server))

    # Covers a bot restart while a mission is already running.
    # mission.lua ignores a second load, so this is safe.
    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server) and server.status in (Status.RUNNING, Status.PAUSED):
            await server.send_to_dcs({"command": "jshAirportLoad"})
            self.plugin.airbases.pop(server.name, None)
            self._spawn(self.refresh_later(server))

    async def refresh_later(self, server: Server, delay: int = 15) -> None:
        await asyncio.sleep(delay)
        try:
            await self.plugin.refresh_airbases(server)
        except Exception as ex:
            self.log.warning(f"Airport: could not read the airbase list from {server.name}: {ex}")

    # Sent by mission.lua after a warehouse change the bot requested.
    @event(name="jshAirportResult")
    async def jshAirportResult(self, server: Server, data: dict) -> None:
        # Cache an airbase list whenever one arrives, even if whoever asked for
        # it has already given up. Discord autocomplete can only wait ~2s, and
        # the round trip through DCS is often slower than that, so a late reply
        # is what fills the cache for the next keystroke.
        if data.get('airbases'):
            self.plugin.airbases[server.name] = data['airbases']

        future = self.plugin.pending.pop(data.get('request_id'), None)
        if future and not future.done():
            future.set_result(data)
