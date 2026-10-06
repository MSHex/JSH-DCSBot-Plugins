import asyncio

from core import EventListener, Server, Status, event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import Airport


class AirportEventListener(EventListener["Airport"]):

    # Load mission.lua on every mission start, but only where the plugin is enabled.
    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server):
            await server.send_to_dcs({"command": "jshAirportLoad"})
            # The airbase list changes with the mission; drop the cache and rebuild
            # it once mission.lua has had time to load.
            self.plugin.airbases.pop(server.name, None)
            asyncio.create_task(self.refresh_later(server))

    # Covers a bot restart while a mission is already running.
    # mission.lua ignores a second load, so this is safe.
    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server) and server.status in (Status.RUNNING, Status.PAUSED):
            await server.send_to_dcs({"command": "jshAirportLoad"})
            self.plugin.airbases.pop(server.name, None)
            asyncio.create_task(self.refresh_later(server))

    async def refresh_later(self, server: Server, delay: int = 15) -> None:
        await asyncio.sleep(delay)
        try:
            await self.plugin.refresh_airbases(server)
        except Exception as ex:
            self.log.warning(f"Airport: could not read the airbase list from {server.name}: {ex}")

    # Sent by mission.lua after a warehouse change the bot requested.
    @event(name="jshAirportResult")
    async def jshAirportResult(self, server: Server, data: dict) -> None:
        future = self.plugin.pending.pop(data.get('request_id'), None)
        if future and not future.done():
            future.set_result(data)
