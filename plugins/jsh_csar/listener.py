from core import EventListener, Server, Status, event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import Csar


class CsarEventListener(EventListener["Csar"]):

    # Load mission.lua on every mission start, but only where the plugin is enabled.
    @event(name="onMissionLoadEnd")
    async def onMissionLoadEnd(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server):
            await server.send_to_dcs({"command": "jshCsarLoad"})

    # Covers a bot restart while a mission is already running.
    # mission.lua ignores a second load, so this is safe.
    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, data: dict) -> None:
        if self.plugin.enabled(server) and server.status in (Status.RUNNING, Status.PAUSED):
            await server.send_to_dcs({"command": "jshCsarLoad"})

    # Sent by mission.lua when pilots are delivered.
    # data = {"player": "<name>", "counts": {"Critical": 2, ...}, "source": "joker"}
    @event(name="jshCsarRescue")
    async def jshCsarRescue(self, server: Server, data: dict) -> None:
        if not self.plugin.enabled(server):
            return
        name = data.get('player')
        counts = data.get('counts') or {}
        if not name or not isinstance(counts, dict):
            return

        labels: dict = self.plugin.get_config(server).get('labels', {})

        def phrase(status: str, n: int) -> str:
            label = labels.get(status, status.lower())
            if n != 1 and not label.endswith('s'):
                label += 's'
            return f"{n} {label}"

        player = server.get_player(name=name)
        ucid = player.ucid if player else None
        rewards: dict = self.plugin.get_config(server).get('rewards', {})

        total, parts = 0, []
        for status, n in sorted(counts.items()):
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if n <= 0:
                continue
            points = int(rewards.get(status, 0)) * n
            total += points
            parts.append(phrase(status, n))
            reason = f"for rescuing {phrase(status, n)}"
            # Recorded whatever the reward is, so the counts stay complete.
            await self.plugin.record(server, ucid, name, status, n, points,
                                     reason, data.get('source'))

        if total > 0:
            if len(parts) > 1:
                what = ", ".join(parts[:-1]) + " and " + parts[-1]
            else:
                what = parts[0]
            await self.plugin.pay(server, name, total, f"for rescuing {what}", ucid)

        if ucid:
            for award in await self.plugin.check_awards_for(server, ucid):
                await self.plugin.message(server, name, f"Award earned: {award}")
