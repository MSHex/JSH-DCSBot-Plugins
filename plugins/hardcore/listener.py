from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from core import EventListener, Server, Status, event
from .economy import EconomyService


class HardcoreEventListener(EventListener):
    """Only dedicated mission telemetry may authorize an automatic penalty.

    Native player events retain their identity/settlement role. They must never
    independently count death notifications already correlated by mission.lua.
    """
    def __init__(self, plugin):
        super().__init__(plugin)
        self.economy = EconomyService(plugin)
        self._locks = defaultdict(asyncio.Lock)
        self._players = {}
        self._closing = {}
        self._heartbeat = {}
        self._tasks = set()
        self._warned = set()
        self._watchdog = None
        self._epochs = {}
        self._retired_epochs = defaultdict(set)

    def enabled(self, server):
        return self.economy.enabled(server)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        def done(t):
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                self.log.error('Hardcore background task failed', exc_info=(type(t.exception()), t.exception(), t.exception().__traceback__))
        task.add_done_callback(done)
        return task

    async def _configure(self, server):
        cfg = self.plugin.get_config(server)
        await server.send_to_dcs({'command': 'configureHardcore', 'config': {
            key: cfg[key] for key in ('enabled','support_roles','protection_zones','farp_radius_m',
                                     'airfield_margin_m','evidence_max_age_seconds','incident_merge_seconds') if key in cfg
        }})

    @event(name='registerDCSServer')
    async def registerDCSServer(self, server, data):
        if not self.enabled(server):
            return
        closing=[t for (name,_),t in self._closing.items() if name==server.name]
        if closing:
            await asyncio.gather(*closing)
        async with self._locks[server.name]:
            for p in server.get_active_players():
                self._players[(server.name,p.id)] = p.ucid
            await self.economy.reconcile_server(server)
        await self._configure(server)
        self._heartbeat[server.name] = time.monotonic()
        if not self._watchdog or self._watchdog.done():
            self._watchdog = self._spawn(self._watch())

    async def _watch(self):
        while True:
            await asyncio.sleep(10)
            for server in self.bot.servers.values():
                if not self.enabled(server):
                    continue
                if not server.get_active_players():
                    self._heartbeat[server.name] = time.monotonic()
                    continue
                last = self._heartbeat.get(server.name, time.monotonic())
                if time.monotonic()-last > 15 and server.name not in self._warned:
                    self._warned.add(server.name)
                    if server.status == Status.RUNNING:
                        self.log.warning('Hardcore telemetry missing on %s. Restart DCS after installing the Lua files. Earnings pause at last confirmed activity; no unsupported penalties are applied.', server.name)
                    async with self._locks[server.name]:
                        await self.economy.reconcile_server(server)
                    await self._configure(server)

    @event(name='onHardcoreTelemetry')
    async def onHardcoreTelemetry(self, server, data):
        if not self.enabled(server) or not data.get('ucid') or not data.get('epoch') or 'seq' not in data:
            return
        epoch=data['epoch']
        if epoch in self._retired_epochs[server.name]:
            return
        old=self._epochs.get(server.name)
        if old and old!=epoch:
            self._retired_epochs[server.name].add(old)
        self._epochs[server.name]=epoch
        ucid=data['ucid']
        self._players[(server.name,data.get('id'))] = ucid
        self._heartbeat[server.name] = time.monotonic()
        self._warned.discard(server.name)
        async with self._locks[server.name]:
            if data.get('action') == 'incident':
                await self.economy.record_incident(server,ucid,data)
            elif data.get('action') == 'state' and (server.name,ucid) not in self._closing:
                await self.economy.observe(server,ucid,data.get('name') or ucid,data)

    @event(name='onPlayerStart')
    async def onPlayerStart(self, server, data):
        if not self.enabled(server) or data.get('id')==1:
            return
        ucid=data.get('ucid')
        if ucid:
            self._players[(server.name,data['id'])]=ucid
            # If reconnecting during the grace window, finish the old session first.
            task=self._closing.get((server.name,ucid))
            if task:
                await asyncio.shield(task)

    async def _disconnect(self, server, player_id, ucid=None):
        ucid=ucid or self._players.get((server.name,player_id))
        if not ucid:
            return
        key=(server.name,ucid)
        if key in self._closing:
            return
        async def finish():
            try:
                async with self._locks[server.name]:
                    await self.economy.pause_segment(server,ucid)
                # Allow the mission's ejection/crash grouping to finish first.
                await asyncio.sleep(float(self.plugin.get_config(server).get('incident_merge_seconds',3))+2)
                async with self._locks[server.name]:
                    await self.economy.settle(server,ucid)
            finally:
                self._closing.pop(key,None)
        self._closing[key]=self._spawn(finish())

    @event(name='onPlayerStop')
    async def onPlayerStop(self, server, data):
        if self.enabled(server):
            await self._disconnect(server,data.get('id'),data.get('ucid'))

    @event(name='onGameEvent')
    async def onGameEvent(self, server, data):
        if self.enabled(server) and data.get('eventName')=='disconnect':
            await self._disconnect(server,data.get('arg1'))

    @event(name='onSimulationStop')
    async def onSimulationStop(self, server, data):
        if not self.enabled(server):
            return
        async with self.apool.connection() as conn:
            cur=await conn.execute('SELECT player_ucid FROM hardcore_sessions WHERE server_name=%s AND NOT settled',(server.name,))
            for (ucid,) in await cur.fetchall():
                await self._disconnect(server,None,ucid)

    async def shutdown(self):
        # Finish outstanding settlements. Stop the infinite watchdog first.
        if self._watchdog:
            self._watchdog.cancel()
        await asyncio.gather(*list(self._tasks),return_exceptions=True)
