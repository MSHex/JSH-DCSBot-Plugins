import asyncio

from core import EventListener, event, Server
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import CarrierQual

#: greenieboard writes its traps row from its OWN handler for this event, and
#: nothing orders two plugins' handlers. So wait before reading the row.
SETTLE_SECONDS = 15


class CQEventListener(EventListener["CarrierQual"]):
    """Re-checks a pilot shortly after each Super Carrier grade."""

    def __init__(self, plugin: "CarrierQual"):
        super().__init__(plugin)
        # A task with no retained reference can be collected before it runs.
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @event(name="onMissionEvent")
    async def onMissionEvent(self, server: Server, data: dict) -> None:
        if data.get('eventName') != 'S_EVENT_LANDING_QUALITY_MARK':
            return
        name = (data.get('initiator') or {}).get('name')
        player = server.get_player(name=name) if name else None
        if player and player.ucid:
            # Spawned, never awaited: a listener holds up every other listener
            # on this server until it returns.
            self._spawn(self._later(player.ucid))

    async def _later(self, ucid: str) -> None:
        await asyncio.sleep(SETTLE_SECONDS)
        try:
            await self.plugin.evaluate(ucid)
        except Exception as ex:
            self.log.exception(f"JSH CQ: could not evaluate {ucid}: {ex}")
