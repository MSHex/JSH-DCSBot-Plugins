from core import EventListener, Server, Status, event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import Coords


class CoordsEventListener(EventListener["Coords"]):

    # Load mission.lua on every mission start, but only where the plugin is enabled.
    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server):
            await server.send_to_dcs({"command": "jshCoordsLoad"})

    # Covers a bot restart while a mission is already running.
    # mission.lua ignores a second load, so this is safe.
    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server) and server.status in (Status.RUNNING, Status.PAUSED):
            await server.send_to_dcs({"command": "jshCoordsLoad"})

    # Sent by mission.lua in answer to a conversion request.
    @event(name="jshCoordsResult")
    async def jshCoordsResult(self, server: Server, data: dict) -> None:
        future = self.plugin.pending.pop(data.get('request_id'), None)
        if future and not future.done():
            future.set_result(data)
