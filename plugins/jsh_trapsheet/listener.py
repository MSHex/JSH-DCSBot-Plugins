import asyncio
import time

from core import EventListener, event, Server, Player, chat_command
from typing import TYPE_CHECKING

from . import sheet

if TYPE_CHECKING:
    from .commands import TrapSheet


class TrapSheetEventListener(EventListener["TrapSheet"]):
    """
    Correlates a sampled approach with the LSO grade, and stores the result.

    Two things arrive separately and have to be matched up:

      * `jshTrapsheet` -- our own samples, sent by the mission as soon as the
        pass ends (touchdown, bolter or departure from the capture window).
      * `S_EVENT_LANDING_QUALITY_MARK` -- the stock Super Carrier LSO grade,
        which arrives through the bot's normal mission-event path.

    The grade is the one that may never come: DCS only raises it on Super
    Carrier decks. So the samples are held briefly and flushed on a timer if
    no grade turns up, which is how a legacy deck still gets a picture.
    """

    def __init__(self, plugin: "TrapSheet"):
        super().__init__(plugin)
        # (server_name, player_name) -> {'points': [...], 'at': monotonic, ...}
        self.pending: dict[tuple[str, str], dict] = {}
        # asyncio.create_task with no retained reference can be collected
        # before it runs. Keep them until they are done.
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        # Do not test server.status here. The mission plugin sets it inside
        # ITS OWN registerDCSServer handler and listener order across plugins
        # is not guaranteed, so RUNNING/PAUSED is always false at this point.
        # DCS registering means DCS is there.
        self._spawn(self.plugin.load_lua(server))

    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, data: dict) -> None:
        # A new mission wipes the injected hook, so put it back.
        self.pending = {k: v for k, v in self.pending.items() if k[0] != server.name}
        self._spawn(self.plugin.load_lua(server))

    # --------------------------------------------------------------- samples
    @event(name="jshTrapsheet")
    async def jshTrapsheet(self, server: Server, data: dict) -> None:
        """A finished pass, sampled mission-side."""
        name = data.get('name')
        points = data.get('points') or []
        if not name or len(points) < 10:
            return
        config = self.plugin.get_config(server)
        if not config.get('enabled', True):
            return

        key = (server.name, name)
        self.pending[key] = {
            'points': points,
            'unit_type': data.get('unit_type') or '',
            'carrier': data.get('carrier') or '',
            'at': time.monotonic(),
        }
        self.log.debug(f"JSH TrapSheet: {len(points)} samples for {name} on {server.name}")

        grace = float(config.get('grade_wait', 20))
        self._spawn(self._expire(server, key, grace))

    async def _expire(self, server: Server, key: tuple[str, str], grace: float) -> None:
        """Flush a pass that never got an LSO grade -- a legacy deck."""
        await asyncio.sleep(grace)
        pass_ = self.pending.pop(key, None)
        if pass_ is None:
            return   # the grade arrived and took it
        player = server.get_player(name=key[1])
        if player:
            self.log.debug(f"JSH TrapSheet: no LSO grade for {key[1]} within {grace}s; "
                           f"plotting without one (legacy deck?)")
            await self.plugin.store(server, player, pass_, grade=None, comment='')

    # ----------------------------------------------------------------- grade
    @event(name="onMissionEvent")
    async def onMissionEvent(self, server: Server, data: dict) -> None:
        if data.get('eventName') != 'S_EVENT_LANDING_QUALITY_MARK':
            return
        if 'initiator' not in data or 'name' not in data['initiator']:
            return
        config = self.plugin.get_config(server)
        if not config.get('enabled', True):
            return
        player: Player = server.get_player(name=data['initiator']['name'])
        if not player:
            return

        key = (server.name, data['initiator']['name'])
        pass_ = self.pending.pop(key, None)
        if pass_ is None:
            # Graded, but we have no samples. Nothing to draw; greenieboard's
            # own SC path has already recorded the grade, so leave it alone.
            return

        comment = data.get('comment', '') or ''
        await self.plugin.store(server, player, pass_,
                                grade=sheet.grade_of(comment), comment=comment)
