import asyncio

from core import EventListener, event, Server, Player
from typing import TYPE_CHECKING

from . import sheet

if TYPE_CHECKING:
    from .commands import TrapSheet

Key = tuple[str, str]   # (server name, player name)


class TrapSheetEventListener(EventListener["TrapSheet"]):
    """
    Pairs a sampled approach with its LSO grade, then hands both on.

    Two things arrive separately and have to be matched up:

      * `jshTrapsheet` -- our samples, sent by the mission when the pass ends.
      * `S_EVENT_LANDING_QUALITY_MARK` -- the Super Carrier LSO grade, through
        the bot's normal mission-event path.

    EITHER may arrive first. They travel different paths and nothing orders
    them: on a trap the samples flush 0.1 s after touchdown, but on a
    wave-off or a bolter they only flush once the aircraft has passed the
    ship, by which time the grade has often already landed. So each side
    waits for the other, for `grade_wait` seconds, and whichever arrives
    second completes the pair. An earlier version only waited on the grade
    and silently dropped any pass whose grade came first.
    """

    def __init__(self, plugin: "TrapSheet"):
        super().__init__(plugin)
        self.samples: dict[Key, dict] = {}   # waiting for a grade
        self.grades: dict[Key, dict] = {}    # waiting for samples
        # asyncio.create_task with no retained reference can be collected
        # before it runs. Keep them until they are done.
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _forget(self, server: Server) -> None:
        self.samples = {k: v for k, v in self.samples.items() if k[0] != server.name}
        self.grades = {k: v for k, v in self.grades.items() if k[0] != server.name}

    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        config = self.plugin.get_config(server)
        if config.get('channel'):
            # Same check greenieboard does: fail loudly at startup if the
            # channel is wrong, rather than silently on the first trap.
            self.plugin.bot.check_channel(int(config['channel']))
        # Do not test server.status here. The mission plugin sets it inside
        # ITS OWN registerDCSServer handler and listener order across plugins
        # is not guaranteed, so RUNNING/PAUSED is always false at this point.
        # DCS registering means DCS is there.
        self._spawn(self.plugin.load_lua(server))

    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, data: dict) -> None:
        # A new mission wipes the injected hook, so put it back -- and drop
        # any half-pair from the old mission, which can never complete now.
        self._forget(server)
        self._spawn(self.plugin.load_lua(server))

    # ------------------------------------------------------------- the halves
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
        self.log.debug(f"JSH TrapSheet: {len(points)} samples for {name} on {server.name}")
        await self._arrive(server, (server.name, name), config, samples={
            'points': points,
            'unit_type': data.get('unit_type') or '',
            'carrier': data.get('carrier') or '',
        })

    @event(name="onMissionEvent")
    async def onMissionEvent(self, server: Server, data: dict) -> None:
        if data.get('eventName') != 'S_EVENT_LANDING_QUALITY_MARK':
            return
        initiator = data.get('initiator') or {}
        if 'name' not in initiator:
            return
        config = self.plugin.get_config(server)
        if not config.get('enabled', True):
            return
        await self._arrive(server, (server.name, initiator['name']), config, grade={
            'comment': data.get('comment', '') or '',
        })

    # --------------------------------------------------------------- pairing
    async def _arrive(self, server: Server, key: Key, config: dict,
                      samples: dict | None = None, grade: dict | None = None) -> None:
        """One half has arrived. Complete the pair, or wait for the other."""
        if samples is not None:
            other = self.grades.pop(key, None)
            if other is None:
                self.samples[key] = samples
                self._spawn(self._expire(self.samples, key, config, 'grade'))
                return
            grade = other
        else:
            other = self.samples.pop(key, None)
            if other is None:
                self.grades[key] = grade
                self._spawn(self._expire(self.grades, key, config, 'samples'))
                return
            samples = other

        player: Player = server.get_player(name=key[1])
        if not player:
            # Left between the pass and now. The samples cannot be filed
            # against a UCID we no longer have, so let it go.
            return

        comment = grade['comment']
        parsed = sheet.grade_of(comment)
        if parsed is None:
            # Graded by DCS but unparseable. Rare, and worth saying out loud
            # rather than silently filing a blank: it means the LSO comment
            # format has changed under us.
            self.log.warning(f"JSH TrapSheet: could not read a grade out of "
                             f"{comment!r} for {player.name}.")
        # Spawned, not awaited. The bot reads a server's next message only
        # after every listener has finished with this one
        # (services/servicebus/service.py, asyncio.wait on all listener
        # tasks), and store() renders a PNG, may retry the attach for three
        # seconds, and posts to Discord. Awaiting it here would stall every
        # other event from this server -- kills, landings, other plugins'
        # credits -- behind one picture.
        self._spawn(self.plugin.store(server, player, samples,
                                      grade=parsed, comment=comment))

    async def _expire(self, waiting: dict, key: Key, config: dict, missing: str) -> None:
        """
        Drop a half that never found its partner.

        Nothing is plotted or stored. An ungraded pass is clutter, and a grade
        with no samples is already on greenieboard without our help. The timer
        exists so neither dict can grow without bound.
        """
        grace = float(config.get('grade_wait', 20))
        await asyncio.sleep(grace)
        if waiting.pop(key, None) is not None:
            self.log.debug(f"JSH TrapSheet: no {missing} for {key[1]} within "
                           f"{grace}s; dropping the other half.")
