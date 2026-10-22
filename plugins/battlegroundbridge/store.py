"""
The bridge's database layer.

Every mutation runs inside one transaction that also claims its event receipt,
so the two outcomes are the only ones possible: the work and the receipt both
commit, or neither does and a retry runs again. That is the whole of the
idempotency story -- there is no separate bookkeeping to get out of step.

Writes to bg_geometry2 are always qualified by server_name as well as id, and
only ever for an id already confirmed in bgb_items for that server. A bad or
hostile ID therefore matches nothing rather than reaching another server's map
or somebody's website-created annotation.
"""
import asyncio
import json

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import sql as S
from .domain import (BRIDGE_ID_START, DEFAULT_RING_COLOUR, DEFAULT_RING_LABEL,
                     DEFAULT_UNLABELLED,
                     DEFAULT_RING_SKIP, ImportSummary, build_recon_payload,
                     build_ring_payload, convert_drawing, match_threat_system,
                     merge_marker_update, Converted, Unsupported)


# ------------------------------------------------------------------ outcomes

@dataclass(frozen=True)
class Created:
    intel_id: int


@dataclass(frozen=True)
class Updated:
    intel_id: int


@dataclass(frozen=True)
class Deleted:
    intel_id: int
    title: Optional[str] = None


@dataclass(frozen=True)
class AlreadyDone:
    """The event had already been processed. Not an error -- a replay."""


@dataclass(frozen=True)
class NotFound:
    intel_id: int


@dataclass(frozen=True)
class Denied:
    owner_name: Optional[str]


Outcome = Any


class BridgeStore:

    def __init__(self, pool, log):
        self.pool = pool
        self.log = log
        # Serialises mutations per server. The database transactions are already
        # correct on their own; this keeps a burst of marker events for one
        # server from queueing up lock waits behind each other.
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, server_name: str) -> asyncio.Lock:
        return self._locks.setdefault(server_name, asyncio.Lock())

    # -------------------------------------------------------------- sessions

    async def touch_session(self, server_name: str, token: str,
                            mission_name: Optional[str]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(S.TOUCH_SESSION, (server_name, token, mission_name))

    async def latest_session(self, server_name: str) -> Optional[tuple]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(S.LATEST_SESSION, (server_name,))
            return await cursor.fetchone()

    # ------------------------------------------------------------- receipts

    @staticmethod
    async def _claim(conn, server_name: str, token: str, marker_id: int,
                     sequence: int) -> bool:
        """
        True when this event is ours to process.

        Claimed inside the caller's transaction on purpose: a rollback releases
        the claim so the retry still runs, and a commit makes every later
        delivery of the same sequence a no-op.
        """
        cursor = await conn.execute(S.CLAIM_RECEIPT,
                                    (server_name, token, marker_id, sequence))
        return await cursor.fetchone() is not None

    @staticmethod
    async def _audit(conn, server_name: str, action: str, geometry_id: Optional[int],
                     marker_id: Optional[int], actor_ucid: Optional[str],
                     actor_name: Optional[str], detail: str = '') -> None:
        await conn.execute(S.AUDIT, (server_name, action, geometry_id, marker_id,
                                     actor_ucid, actor_name, detail))

    # ----------------------------------------------------------------- intel

    async def upsert_intel(self, server_name: str, token: str, marker_id: int, sequence: int,
                           title: str, description: list[str], mgrs: str, lat: float, lon: float,
                           ucid: str, player_name: str, side: str = 'blue',
                           rings: Optional[dict] = None) -> Outcome:
        """Create the report for this marker, or move and retitle the existing one."""
        async with self.lock(server_name):
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    if not await self._claim(conn, server_name, token, marker_id, sequence):
                        return AlreadyDone()

                    cursor = await conn.execute(S.FIND_ITEM_BY_MARKER,
                                                (server_name, token, marker_id))
                    row = await cursor.fetchone()

                    if row:
                        item_id, geometry_id, owner_ucid, owner_name, _, deleted_at = row
                        if deleted_at is not None:
                            # Deliberately removed. A queued or replayed edit must
                            # not bring it back; new intel needs a new marker.
                            return AlreadyDone()
                        if owner_ucid and owner_ucid != ucid:
                            return Denied(owner_name)

                        cursor = await conn.execute(S.SELECT_GEOMETRY_FOR_UPDATE,
                                                    (geometry_id, server_name))
                        current = await cursor.fetchone()
                        if not current:
                            # Deleted in the browser while we held the mapping.
                            # Treat the website as authoritative and tombstone.
                            await conn.execute(S.TOMBSTONE_ITEM, (item_id,))
                            return NotFound(geometry_id)

                        payload = merge_marker_update(_as_dict(current[0]), title, mgrs, lat, lon)
                        await conn.execute(S.UPDATE_GEOMETRY,
                                           (json.dumps(payload), geometry_id, server_name))
                        await conn.execute(S.TOUCH_ITEM_CONTENT, (title, item_id))
                        await self._sync_intel_ring(conn, server_name, token, item_id,
                                                    title, lat, lon, rings, player_name, side)
                        await self._audit(conn, server_name, 'update', geometry_id, marker_id,
                                          ucid, player_name, title)
                        return Updated(geometry_id)

                    cursor = await conn.execute(S.NEXT_GEOMETRY_ID)
                    geometry_id = int((await cursor.fetchone())[0])
                    payload = build_recon_payload(
                        title=title, author_name=player_name, mgrs=mgrs, lat=lat, lon=lon,
                        description=description, intel_id=geometry_id,
                        reported_at=_now(), side=side)
                    await conn.execute(S.INSERT_GEOMETRY,
                                       (geometry_id, server_name, json.dumps(payload)))
                    item_cursor = await conn.execute(
                        S.INSERT_ITEM,
                        (server_name, token, 'intel', marker_id, geometry_id,
                         ucid, player_name, title, None))
                    item_id = (await item_cursor.fetchone())[0]
                    await self._sync_intel_ring(conn, server_name, token, item_id,
                                                title, lat, lon, rings, player_name, side)
                    await self._audit(conn, server_name, 'create', geometry_id, marker_id,
                                      ucid, player_name, title)
                    return Created(geometry_id)

    async def delete_by_marker(self, server_name: str, token: str, marker_id: int, sequence: int,
                               ucid: str, player_name: str, is_admin: bool) -> Outcome:
        async with self.lock(server_name):
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    if not await self._claim(conn, server_name, token, marker_id, sequence):
                        return AlreadyDone()
                    cursor = await conn.execute(S.FIND_ITEM_BY_MARKER,
                                                (server_name, token, marker_id))
                    row = await cursor.fetchone()
                    if not row:
                        return NotFound(0)
                    item_id, geometry_id, owner_ucid, owner_name, title, deleted_at = row
                    if deleted_at is not None:
                        return AlreadyDone()
                    if not is_admin and owner_ucid and owner_ucid != ucid:
                        return Denied(owner_name)
                    return await self._remove(conn, server_name, item_id, geometry_id,
                                              marker_id, ucid, player_name, title)

    async def delete_by_id(self, server_name: str, geometry_id: int, ucid: Optional[str],
                           player_name: str, is_admin: bool) -> Outcome:
        """
        Delete a specific bridge-created report.

        Scoped by server, and only for an id the bridge owns: an id belonging to
        another server, to the website, or to an imported drawing comes back as
        NotFound rather than being deleted.
        """
        async with self.lock(server_name):
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    cursor = await conn.execute(S.FIND_ITEM_BY_GEOMETRY,
                                                (server_name, geometry_id))
                    row = await cursor.fetchone()
                    if not row:
                        return NotFound(geometry_id)
                    item_id, geo_id, marker_id, owner_ucid, owner_name, title, deleted_at = row
                    if deleted_at is not None:
                        return AlreadyDone()
                    if not is_admin and owner_ucid and owner_ucid != ucid:
                        return Denied(owner_name)
                    return await self._remove(conn, server_name, item_id, geo_id,
                                              marker_id, ucid, player_name, title)

    async def _sync_intel_ring(self, conn, server_name: str, token: str, item_id: int,
                               title: str, lat: float, lon: float, rings: Optional[dict],
                               author_name: str, side: str) -> None:
        """
        Make the report's threat ring agree with its title and position.

        Called on create and on every update, which is what lets a ring follow a
        marker that moves and disappear when a title stops naming a system --
        retitling `SA-10` to `empty revetment` should not leave a 119 km circle
        on the map.
        """
        rings = rings or {}
        if not rings.get('enabled', False):
            return
        match = match_threat_system(
            title, rings.get('systems') or {},
            rings.get('skip_pattern', DEFAULT_RING_SKIP),
            rings.get('colour', DEFAULT_RING_COLOUR),
            rings.get('label', DEFAULT_RING_LABEL))

        cursor = await conn.execute(S.FIND_RING_BY_PARENT, (item_id,))
        existing = await cursor.fetchone()

        if not match:
            if existing:
                ring_item_id, geometry_id, _ = existing
                await conn.execute(S.DELETE_GEOMETRY, (geometry_id, server_name))
                await conn.execute(S.TOMBSTONE_ITEM, (ring_item_id,))
            return

        payload = build_ring_payload(match, [lat, lon], author_name, side=side)

        if existing:
            ring_item_id, geometry_id, _ = existing
            await conn.execute(S.UPDATE_GEOMETRY,
                               (json.dumps(payload), geometry_id, server_name))
            await conn.execute(S.TOUCH_ITEM_CONTENT, (payload['title'], ring_item_id))
            return

        cursor = await conn.execute(S.NEXT_GEOMETRY_ID)
        geometry_id = int((await cursor.fetchone())[0])
        await conn.execute(S.INSERT_GEOMETRY,
                           (geometry_id, server_name, json.dumps(payload)))
        await conn.execute(S.INSERT_ITEM,
                           (server_name, token, 'ring', None, geometry_id, None,
                            author_name, payload['title'], item_id))

    async def _remove(self, conn, server_name: str, item_id: int, geometry_id: int,
                      marker_id: Optional[int], ucid: Optional[str], player_name: str,
                      title: Optional[str]) -> Outcome:
        # The ring goes first. ON DELETE CASCADE covers the mapping row, but the
        # geometry row it points at is the website's and has to be removed here.
        cursor = await conn.execute(S.FIND_RING_BY_PARENT, (item_id,))
        ring = await cursor.fetchone()
        if ring:
            ring_item_id, ring_geometry_id, _ = ring
            await conn.execute(S.DELETE_GEOMETRY, (ring_geometry_id, server_name))
            await conn.execute(S.TOMBSTONE_ITEM, (ring_item_id,))

        await conn.execute(S.DELETE_GEOMETRY, (geometry_id, server_name))
        await conn.execute(S.TOMBSTONE_ITEM, (item_id,))
        await self._audit(conn, server_name, 'delete', geometry_id, marker_id,
                          ucid, player_name, title or '')
        return Deleted(geometry_id, title)

    # ----------------------------------------------------------- maintenance

    async def expire_intel(self, server_name: str, hours: int) -> list[int]:
        """
        Expiry is measured from the last DCS-driven change, so edits made in the
        browser never extend a report's life. Returns what it removed.
        """
        if hours <= 0:
            return []
        removed: list[int] = []
        async with self.lock(server_name):
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    cursor = await conn.execute(S.EXPIRED_INTEL, (server_name, hours))
                    for item_id, geometry_id, title in await cursor.fetchall():
                        await conn.execute(S.DELETE_GEOMETRY, (geometry_id, server_name))
                        await conn.execute(S.TOMBSTONE_ITEM, (item_id,))
                        await self._audit(conn, server_name, 'expire', geometry_id, None,
                                          None, 'bridge', title or '')
                        removed.append(geometry_id)
        return removed

    async def clear_intel(self, server_name: str) -> int:
        """Used when retain_intel_on_mission_change is off and a new mission starts."""
        async with self.lock(server_name):
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    cursor = await conn.execute(S.LIVE_INTEL_IDS, (server_name,))
                    rows = await cursor.fetchall()
                    for item_id, geometry_id in rows:
                        await conn.execute(S.DELETE_GEOMETRY, (geometry_id, server_name))
                        await conn.execute(S.TOMBSTONE_ITEM, (item_id,))
                    if rows:
                        await self._audit(conn, server_name, 'clear', None, None, None,
                                          'bridge', f'{len(rows)} reports')
                    return len(rows)

    # -------------------------------------------------------------- drawings

    async def replace_drawings(self, server_name: str, token: str, layers: dict,
                               wanted_layers: list[str], author_name: str,
                               side: str = 'blue',
                               rings: Optional[dict] = None,
                               unlabelled_pattern: Optional[str] = None) -> ImportSummary:
        """
        Reimport the configured layers, replacing only what the bridge imported
        before. Website-created geometry and marker intel are untouched: the
        removal step works from bgb_items rows of kind 'drawing' and nothing
        else.

        DCS is authoritative for these, which is why they are replaced outright
        rather than merged the way marker intel is.
        """
        summary = ImportSummary()
        converted: list[Converted] = []
        ring_payloads: list[dict] = []
        for layer_name, drawings in (layers or {}).items():
            if wanted_layers and layer_name not in wanted_layers:
                continue
            summary.layers.append(layer_name)
            for drawing in drawings or []:
                result = convert_drawing(
                    drawing, author_name, side,
                    unlabelled_pattern if unlabelled_pattern is not None
                    else DEFAULT_UNLABELLED)
                if isinstance(result, Unsupported):
                    summary.skipped.append(
                        f"{result.name} ({result.primitive}): {result.reason}")
                    continue
                converted.append(result)
                if result.approximated:
                    summary.approximated.append(
                        f"{result.name or result.payload['title']}: {result.approximated}")

                # Rings attach to point features only. A zone, a line or an
                # existing circle never gets one -- otherwise a hand-drawn
                # 'SA-11 Range' circle would acquire a ring of its own.
                if result.kind != 'markpoint':
                    continue
                ring_config = rings or {}
                if not ring_config.get('enabled', False):
                    continue
                match = match_threat_system(
                    result.payload['title'], ring_config.get('systems') or {},
                    ring_config.get('skip_pattern', DEFAULT_RING_SKIP),
                    ring_config.get('colour', DEFAULT_RING_COLOUR),
                    ring_config.get('label', DEFAULT_RING_LABEL))
                centre = result.payload['fields'].get('posPoint')
                if match and centre and len(centre) == 2:
                    ring_payloads.append(
                        build_ring_payload(match, centre, author_name, side=side))

        async with self.lock(server_name):
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    cursor = await conn.execute(S.LIVE_DRAWING_IDS, (server_name,))
                    for item_id, geometry_id in await cursor.fetchall():
                        await conn.execute(S.DELETE_GEOMETRY, (geometry_id, server_name))
                        await conn.execute(S.TOMBSTONE_ITEM, (item_id,))
                        summary.removed += 1

                    for result in converted:
                        cursor = await conn.execute(S.NEXT_GEOMETRY_ID)
                        geometry_id = int((await cursor.fetchone())[0])
                        await conn.execute(
                            S.INSERT_GEOMETRY,
                            (geometry_id, server_name, json.dumps(result.payload)))
                        await conn.execute(
                            S.INSERT_ITEM,
                            (server_name, token, 'drawing', None, geometry_id, None,
                             author_name, result.name or result.payload['title'], None))
                        summary.imported += 1

                    # Rings for the point features, stored as ordinary drawings:
                    # the next import replaces every row this one wrote, so they
                    # need no parent link and no lifecycle of their own.
                    for ring in ring_payloads:
                        cursor = await conn.execute(S.NEXT_GEOMETRY_ID)
                        geometry_id = int((await cursor.fetchone())[0])
                        await conn.execute(
                            S.INSERT_GEOMETRY,
                            (geometry_id, server_name, json.dumps(ring)))
                        await conn.execute(
                            S.INSERT_ITEM,
                            (server_name, token, 'drawing', None, geometry_id, None,
                             author_name, ring['title'], None))
                        summary.rings += 1

                    await self._audit(conn, server_name, 'import', None, None, None,
                                      author_name, summary.describe())
        return summary


def _as_dict(value: Any) -> dict:
    """bg_geometry2.data is json; psycopg may hand back str or an already-parsed dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8')
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


__all__ = ['BridgeStore', 'Created', 'Updated', 'Deleted', 'AlreadyDone', 'NotFound',
           'Denied', 'BRIDGE_ID_START']
