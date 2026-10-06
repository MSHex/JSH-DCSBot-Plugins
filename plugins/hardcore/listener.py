from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from core import EventListener, Server, Side, Status, event

from .economy import EconomyService

if TYPE_CHECKING:
    from .commands import Hardcore


class HardcoreEventListener(EventListener["Hardcore"]):
    """
    DCS event adapter.

    Persistent economy/death state lives in PostgreSQL. The only RAM state here
    is a short-lived (server, player_id) -> UCID identity cache so disconnect
    and spectator slot-change events can still be attributed after DCS omits
    the UCID from the event payload.
    """

    def __init__(self, plugin: "Hardcore"):
        super().__init__(plugin)
        self.economy = EconomyService(plugin)
        self._player_ucids: dict[tuple[str, int], str] = {}

    def enabled(self, server: Server) -> bool:
        return self.economy.enabled(server)

    @staticmethod
    def _player_id(data: dict, *, game_event: bool = False) -> Optional[int]:
        raw = data.get("arg1") if game_event else data.get("id")
        if raw in (None, -1):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _cache_ucid(self, server: Server, player_id: Optional[int], ucid: Optional[str]) -> Optional[str]:
        if player_id is None or not ucid:
            return ucid
        self._player_ucids[(server.name, player_id)] = ucid
        return ucid

    def _cached_ucid(self, server: Server, player_id: Optional[int]) -> Optional[str]:
        if player_id is None:
            return None
        return self._player_ucids.get((server.name, player_id))

    def _forget_player(self, server: Server, player_id: Optional[int]) -> None:
        if player_id is not None:
            self._player_ucids.pop((server.name, player_id), None)

    def _resolve_ucid(
        self,
        server: Server,
        data: dict,
        *,
        game_event: bool = False,
        allow_player_lookup: bool = True
    ) -> Optional[str]:
        """
        Resolve a UCID without depending on the player still existing in
        DCSServerBot's live player registry.

        Resolution order:
          1. UCID carried directly by the event
          2. listener identity cache
          3. server.get_player(id=...) as a final fallback
        """
        player_id = self._player_id(data, game_event=game_event)

        ucid = data.get("ucid")
        if ucid:
            return self._cache_ucid(server, player_id, str(ucid))

        cached = self._cached_ucid(server, player_id)
        if cached:
            return cached

        if allow_player_lookup and player_id is not None:
            player = server.get_player(id=player_id)
            if player and getattr(player, "ucid", None):
                return self._cache_ucid(server, player_id, player.ucid)

        return None

    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, _data: dict) -> None:
        if not self.enabled(server):
            return

        # Rebuild the identity cache from players DCSServerBot already knows.
        try:
            for player in server.get_active_players():
                pid = getattr(player, "id", None)
                ucid = getattr(player, "ucid", None)
                if pid not in (None, 1) and ucid:
                    self._cache_ucid(server, int(pid), ucid)
        except Exception:
            self.log.debug("Hardcore: failed to prefill player identity cache for %s", server.name, exc_info=True)

        asyncio.create_task(self.economy.reconcile_server(server))

    @event(name="onPlayerStart")
    async def onPlayerStart(self, server: Server, data: dict) -> None:
        if not self.enabled(server) or data.get("id") == 1:
            return

        player_id = self._player_id(data)
        ucid = self._resolve_ucid(server, data)
        if not ucid:
            self.log.warning(
                "Hardcore: onPlayerStart could not resolve UCID (server=%s id=%s)",
                server.name, player_id
            )
            return

        player = server.get_player(ucid=ucid)
        name = getattr(player, "name", None) or data.get("name") or ucid

        # Do not open an economy session merely for connecting/spectating.
        # The first eligible aircraft slot opens the session, which means a
        # connected spectator can still use /hardcore join or /hardcore leave.
        if player:
            try:
                mode = "🔥 HARDCORE" if await self.economy.is_hardcore(ucid) else "🛡️ NORMAL"
                await player.sendChatMessage(f"Flight Economy active: {mode}.")
            except Exception:
                self.log.debug("Hardcore: welcome chat failed for %s", ucid, exc_info=True)

    @event(name="onPlayerChangeSlot")
    async def onPlayerChangeSlot(self, server: Server, data: dict) -> None:
        if not self.enabled(server) or data.get("id") == 1:
            return

        player_id = self._player_id(data)
        ucid = self._resolve_ucid(server, data)

        # DCS commonly omits UCID when a player leaves an aircraft for
        # spectators. The identity cache is specifically intended to handle
        # that payload. If we still cannot resolve it, log instead of silently
        # discarding the event.
        if not ucid:
            self.log.warning(
                "Hardcore: slot change could not resolve UCID "
                "(server=%s id=%s slot=%s side=%s unit=%s)",
                server.name, player_id, data.get("slot"), data.get("side"),
                data.get("unit_type")
            )
            return

        # If the old aircraft was airborne, any slot change is an illegal
        # airborne reslot before we process the new slot.
        if self.plugin.get_config(server).get("illegal_airborne_reslot", True):
            if await self.economy.is_airborne(server, ucid):
                counted = await self.economy.record_loss(server, ucid, "illegal airborne reslot")
                if counted:
                    self.log.warning(
                        "Hardcore: airborne reslot counted as loss for %s on %s",
                        ucid, server.name
                    )

        side = data.get("side")
        unit_type = data.get("unit_type")

        # Side.NEUTRAL means spectators / no aircraft. Some disconnect paths
        # provide slot=-1 and unit_type='?' without a useful side value.
        in_aircraft = False
        if side is not None:
            try:
                in_aircraft = Side(int(side)) != Side.NEUTRAL
            except (ValueError, TypeError):
                in_aircraft = False

        try:
            if int(data.get("slot", 0)) == -1:
                in_aircraft = False
        except (ValueError, TypeError):
            pass

        excluded = {
            None, "", "?", "forward_observer", "observer", "artillery_commander",
            "instructor", "spectator", "game-master", "gamemaster"
        }
        if str(unit_type).lower() in excluded:
            in_aircraft = False

        player = server.get_player(ucid=ucid)
        name = getattr(player, "name", None) or data.get("name") or ucid

        if in_aircraft:
            await self.economy.start_segment(server, ucid, name)
            self.log.debug(
                "Hardcore: earning segment active (server=%s id=%s ucid=%s unit=%s)",
                server.name, player_id, ucid, unit_type
            )
        else:
            await self.economy.pause_segment(server, ucid)
            self.log.debug(
                "Hardcore: earning segment paused (server=%s id=%s ucid=%s)",
                server.name, player_id, ucid
            )

    @event(name="onPlayerStop")
    async def onPlayerStop(self, server: Server, data: dict) -> None:
        if not self.enabled(server) or data.get("id") == 1:
            return

        player_id = self._player_id(data)
        ucid = self._resolve_ucid(server, data)
        if not ucid:
            self.log.warning(
                "Hardcore: onPlayerStop could not resolve UCID (server=%s id=%s)",
                server.name, player_id
            )
            return

        try:
            settlement = await self.economy.settle(server, ucid)
            if settlement is None:
                self.log.debug(
                    "Hardcore: onPlayerStop found no open session (server=%s id=%s ucid=%s)",
                    server.name, player_id, ucid
                )
        except Exception:
            self.log.exception(
                "Hardcore: onPlayerStop settlement FAILED (server=%s id=%s ucid=%s)",
                server.name, player_id, ucid
            )
            raise
        finally:
            self._forget_player(server, player_id)

    @event(name="onGameEvent")
    async def onGameEvent(self, server: Server, data: dict) -> None:
        if not self.enabled(server) or server.status != Status.RUNNING:
            return

        event_name = data.get("eventName")
        if not event_name:
            return

        # DCSServerBot's common game events identify the player through arg1.
        if event_name in {"takeoff", "landing", "crash", "pilot_death", "eject"}:
            player_id = self._player_id(data, game_event=True)
            if player_id is None:
                return

            player = server.get_player(id=player_id)
            if player and getattr(player, "ucid", None):
                self._cache_ucid(server, player_id, player.ucid)

            if not player:
                ucid = self._cached_ucid(server, player_id)
                self.log.warning(
                    "Hardcore: %s event could not resolve live player "
                    "(server=%s id=%s cached_ucid=%s)",
                    event_name, server.name, player_id, ucid
                )
                return

            # Apply to the pilot/crew session represented by each active crew
            # member when DCSServerBot can resolve the crew.
            try:
                crew = server.get_crew_members(player)
            except Exception:
                crew = [player]
            if not crew:
                crew = [player]

            for member in crew:
                member_ucid = getattr(member, "ucid", None)
                if not member_ucid:
                    continue
                member_id = getattr(member, "id", None)
                if member_id is not None:
                    self._cache_ucid(server, int(member_id), member_ucid)

                if event_name == "takeoff":
                    await self.economy.set_airborne(server, member_ucid, True)
                elif event_name == "landing":
                    await self.economy.set_airborne(server, member_ucid, False)
                else:
                    counted = await self.economy.record_loss(server, member_ucid, event_name)
                    if counted:
                        self.log.info(
                            "Hardcore: loss recorded (%s) for %s on %s",
                            event_name, member_ucid, server.name
                        )

        elif event_name == "disconnect":
            player_id = self._player_id(data, game_event=True)
            if player_id is None:
                return

            # IMPORTANT: use the cache first. At disconnect time the Player
            # object may already have been removed from DCSServerBot.
            ucid = self._cached_ucid(server, player_id)
            if not ucid:
                ucid = self._resolve_ucid(server, data, game_event=True)

            if not ucid:
                self.log.error(
                    "Hardcore: disconnect settlement FAILED - could not resolve UCID "
                    "(server=%s id=%s)",
                    server.name, player_id
                )
                return

            try:
                settlement = await self.economy.settle(server, ucid)
                if settlement is None:
                    self.log.debug(
                        "Hardcore: disconnect found no open session "
                        "(server=%s id=%s ucid=%s)",
                        server.name, player_id, ucid
                    )
            except Exception:
                self.log.exception(
                    "Hardcore: disconnect settlement FAILED "
                    "(server=%s id=%s ucid=%s)",
                    server.name, player_id, ucid
                )
                raise
            finally:
                self._forget_player(server, player_id)

    @event(name="onSimulationStop")
    async def onSimulationStop(self, server: Server, _data: dict) -> None:
        if not self.enabled(server):
            return

        # Mission/server stop should not destroy accrued earnings. Settle all
        # currently open sessions against their accumulated time.
        async with self.apool.connection() as conn:
            cursor = await conn.execute("""
                SELECT player_ucid
                FROM hardcore_sessions
                WHERE server_name = %s AND settled = FALSE
            """, (server.name,))
            rows = await cursor.fetchall()

        for (ucid,) in rows:
            try:
                await self.economy.settle(server, ucid)
            except Exception:
                self.log.exception("Hardcore: simulation-stop settlement failed for %s", ucid)

        # Player ids can be reused after a mission/server restart.
        for key in [k for k in self._player_ucids if k[0] == server.name]:
            self._player_ucids.pop(key, None)
