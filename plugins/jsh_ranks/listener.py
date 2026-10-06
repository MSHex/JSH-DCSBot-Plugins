import asyncio

from core import EventListener, Server, Status, event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import Ranks

RUNNING = [Status.RUNNING, Status.PAUSED]


class RanksEventListener(EventListener["Ranks"]):
    """
    Keeps the mission's copy of each pilot's rank level current.

    Ranks move while people fly -- credits are earned, playtime accrues -- so a
    push at connect alone would go stale during a long session. A periodic
    refresh per server covers that without the mission having to ask.
    """

    def __init__(self, plugin):
        super().__init__(plugin)
        # asyncio holds only a weak reference to a running task, so a task that
        # nothing keeps can be collected before it finishes.
        self._tasks: set = set()
        self._refreshers: dict[str, asyncio.Task] = {}

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ mission load
    async def _load_and_push(self, server: Server, delay: int = 15) -> None:
        """Waits for the mission to settle, injects the hook, then sends ranks."""
        try:
            await asyncio.sleep(delay)
            await self.plugin.load_lua(server)
            await asyncio.sleep(2)
            await self.plugin.push(server)
        except Exception as ex:
            self.log.warning(f"JSH Ranks: could not set up {server.name}: {ex}")

    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, _: dict) -> None:
        if self.plugin.enabled(server):
            self._spawn(self._load_and_push(server))
            self._start_refresher(server)

    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        # Covers a bot restart while a mission is already running.
        if self.plugin.enabled(server) and server.status in RUNNING:
            self._spawn(self._load_and_push(server))
            self._start_refresher(server)

    # ------------------------------------------------------------ players
    @event(name="onPlayerStart")
    async def onPlayerStart(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server) and data.get('id') != 1:
            self._spawn(self._push_soon(server))

    @event(name="onPlayerChangeSlot")
    async def onPlayerChangeSlot(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server) and data.get('id') != 1:
            self._spawn(self._push_soon(server))

    async def _push_soon(self, server: Server, delay: int = 2) -> None:
        # A small delay lets the player row settle before we read their credits.
        try:
            await asyncio.sleep(delay)
            await self.plugin.push(server)
        except Exception as ex:
            self.log.warning(f"JSH Ranks: could not push ranks to {server.name}: {ex}")

    # ------------------------------------------------------------ periodic
    def _start_refresher(self, server: Server) -> None:
        minutes = int(self.plugin.get_config(server).get('refresh_minutes', 10))
        if minutes <= 0:
            return
        existing = self._refreshers.get(server.name)
        if existing and not existing.done():
            return
        self._refreshers[server.name] = asyncio.create_task(
            self._refresh_loop(server, minutes * 60))

    async def _refresh_loop(self, server: Server, seconds: int) -> None:
        while True:
            try:
                await asyncio.sleep(seconds)
                if not self.plugin.enabled(server) or server.status not in RUNNING:
                    continue
                await self.plugin.push(server)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self.log.warning(f"JSH Ranks: refresh failed on {server.name}: {ex}")

    async def shutdown(self) -> None:
        for task in self._refreshers.values():
            task.cancel()
        self._refreshers.clear()
        await super().shutdown()
