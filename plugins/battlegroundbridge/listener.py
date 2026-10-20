import asyncio

from core import EventListener, Server, Status, chat_command, event, Player
from typing import TYPE_CHECKING

from .domain import ID_MAX, ID_MIN

if TYPE_CHECKING:
    from .commands import BattlegroundBridge


class BridgeEventListener(EventListener["BattlegroundBridge"]):

    def __init__(self, plugin):
        super().__init__(plugin)
        # asyncio holds only a weak reference to a running task, so a task
        # nothing else keeps can be collected before it finishes.
        self._tasks: set = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, data: dict) -> None:
        if not self.plugin.enabled(server):
            return
        await server.send_to_dcs({"command": "jshBgbLoad"})
        # A new mission environment means a new token, which the first snapshot
        # will establish. The drawing import waits for that, because importing
        # against the previous mission's token would attach this mission's
        # drawings to the session that is ending.
        self._spawn(self.plugin.on_new_mission(server))

    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        """Covers a bot restart while a mission is already running."""
        if self.plugin.enabled(server) and server.status in (Status.RUNNING, Status.PAUSED):
            await server.send_to_dcs({"command": "jshBgbLoad"})
            self._spawn(self.plugin.ensure_polling(server))

    @event(name="jshBgbSnapshot")
    async def jshBgbSnapshot(self, server: Server, data: dict) -> None:
        future = self.plugin.pending.pop(data.get('request_id'), None)
        if future and not future.done():
            future.set_result(data)

    @chat_command(name="intel", help="Delete your Battleground intel: -intel delete <id>")
    async def intel(self, server: Server, player: Player, params: list[str]) -> None:
        """
        The authenticated fallback.

        In-game chat carries a real UCID, so this works where a marker event
        does not -- from a GM or spectator slot, whose marker events often have
        no identifiable initiator at all.
        """
        if not self.plugin.enabled(server):
            return
        if len(params) != 2 or params[0].lower() != 'delete' or not params[1].isdigit():
            await player.sendChatMessage(
                f"Usage: {self.prefix}intel delete <id>. "
                f"To report intel, place an F10 marker and type {self.prefix}intel: <title>")
            return
        intel_id = int(params[1])
        if not ID_MIN <= intel_id <= ID_MAX:
            await player.sendChatMessage(f"{intel_id} is not a valid intel ID.")
            return
        await player.sendChatMessage(
            await self.plugin.delete_for_player(server, player, intel_id))
