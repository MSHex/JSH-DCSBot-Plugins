import asyncio
import io
import json
import os

import discord
import matplotlib
matplotlib.use('Agg')                      # no display on a headless node
import psycopg                             # noqa: E402

from core import Plugin, Server, Player, Status          # noqa: E402
from datetime import datetime, timezone                  # noqa: E402
from psycopg.types.json import Jsonb                     # noqa: E402
from services.bot import DCSServerBot                    # noqa: E402

from . import sheet, sql                                 # noqa: E402
from .listener import TrapSheetEventListener             # noqa: E402

#: do_script goes bot -> DCS, where UDPRecvSocket:receive() reads into a fixed
#: buffer and truncates SILENTLY. The samples come back the other way, through
#: dcsbot.sendBotTable, which chunks at 65000 and is reassembled by the bot --
#: that direction is not constrained, so only the injection is checked here.
DATAGRAM_LIMIT = 8192
DATAGRAM_MARGIN = 400

RUNNING = [Status.RUNNING, Status.PAUSED]

#: greenieboard writes its row on the same event; give it this long.
ATTACH_TRIES = 6
ATTACH_DELAY = 0.5


def minify_lua(script: str) -> str:
    """
    Drop whole-line comments and blank lines before sending.

    Only lines starting with `--` go; trailing comments stay, since a `--`
    inside a string literal would otherwise swallow the rest of the line.

    Deliberately duplicated from jsh_ranks rather than imported: either
    plugin can be installed without the other, and this is three lines.
    """
    return '\n'.join(line for line in script.splitlines()
                     if line.strip() and not line.lstrip().startswith('--'))


class TrapSheet(Plugin[TrapSheetEventListener]):
    """
    FunkMan-style trapsheets with no MOOSE in the mission.

    DCSServerBot already ships the plot: plugins/greenieboard/trapsheet.py,
    three panels of lineup, glideslope and AoA. What it has never had without
    MOOSE is the trajectory behind them, because the stock Super Carrier LSO
    event carries a grade and nothing else. This samples the approach itself
    and hands the result to that same plotter, so the picture is identical to
    the AIRBOSS one and no second rendering path exists to drift.
    """

    # ------------------------------------------------------------ mission side
    async def load_lua(self, server: Server) -> None:
        config = self.get_config(server)
        if not config.get('enabled', True):
            return
        path = os.path.join(os.path.dirname(__file__), 'lua', 'mission.lua')
        try:
            with open(path, mode='r', encoding='utf-8') as f:
                script = f.read()
        except OSError as ex:
            self.log.error(f"JSH TrapSheet: could not read {path}: {ex}")
            return

        decks = config.get('carrier_types') or sheet.DEFAULT_DECKS
        tuning = {
            'rate': float(config.get('sample_rate', 0.1)),
            'range': float(config.get('capture_range_nm', 1.5)) * 1852.0,
            'ceiling': float(config.get('capture_ceiling_ft', 1500)),
            'maxpoints': int(config.get('max_points', 600)),
        }
        script = minify_lua(script) + (
            f"\njsh_trapsheet.cfg.rate={tuning['rate']!r} "
            f"jsh_trapsheet.cfg.range={tuning['range']!r} "
            f"jsh_trapsheet.cfg.ceiling={tuning['ceiling']!r} "
            f"jsh_trapsheet.cfg.maxpoints={tuning['maxpoints']!r} "
            f"jsh_trapsheet.cfg.decks={sheet.lua_list(decks)}")

        message = {'command': 'do_script', 'script': script}
        size = len(json.dumps(message).encode()) + DATAGRAM_MARGIN
        if size > DATAGRAM_LIMIT:
            self.log.error(
                f"JSH TrapSheet: mission.lua is too large to inject into "
                f"{server.name} ({size} bytes with headroom, limit "
                f"{DATAGRAM_LIMIT}). DCS would truncate it silently and no "
                f"approach would ever be sampled. Not sending.")
            return
        await server.send_to_dcs(message)
        self.log.debug(f"JSH TrapSheet: sampler injected into {server.name} ({size} bytes)")

    # ------------------------------------------------------------------- store
    @staticmethod
    def _greenieboard_tables() -> tuple[dict, dict, dict]:
        """
        greenieboard's GRADES, ERRORS and DISTANCE_MARKS, so our wording and
        legends match /traps exactly. Imported here, not at module level, for
        the same reason as the plotter: a missing table should cost words, not
        stop the plugin loading. const.py imports nothing.
        """
        try:
            from plugins.greenieboard.const import GRADES, ERRORS, DISTANCE_MARKS
            return GRADES, ERRORS, DISTANCE_MARKS
        except Exception:
            return {}, {}, {}

    async def store(self, server: Server, player: Player, pass_: dict,
                    grade: str | None, comment: str) -> None:
        """
        Draw the pass, put it on greenieboard's row, and post it.

        Order matters. greenieboard's row is FOUND first, because the picture
        and the embed show greenieboard's own verdict on the pass -- its
        points, case and day or night -- so they always agree with /traps and
        the board. Then the picture is drawn, then SET on that same row.
        """
        config = self.get_config(server)
        grades, errors, marks = self._greenieboard_tables()
        meta = pass_.get('meta') or {}

        # Cut at touchdown: the rollout in the wires is not part of the
        # approach, and stored, plotted or timed it skews all three.
        points = sheet.trim_touchdown(pass_.get('points') or [])
        unit_type = pass_.get('unit_type') or player.unit_type or ''
        tgroove = sheet.groove_time(points, float(config.get('sample_rate', 0.1)))
        wire = sheet.wire_of(comment)
        details = sheet.details_of(comment)

        # greenieboard's verdict, or our own if it wrote no row.
        row = await self._find(server, player, grade) if grade else None
        if row:
            row_id, score, case, night = row
        else:
            row_id = None
            night = self._night(server, meta.get('clock'))
            case = 3 if night else 1
            score = sheet.points_of(grade)

        now = datetime.now(timezone.utc)
        info = {
            'callsign': player.name,
            'unit_type': unit_type,
            'legend': (grades.get(grade) or {}).get('legend') or grade,
            'points': score,
            'details': details,
            'carrier_name': sheet.CARRIERS.get(meta.get('ctype') or ''),
            'hull': meta.get('ctype'),
            'case': case,
            'wind': meta.get('wind'),
            'wire': wire,
            'tgroove': tgroove,
            'theatre': meta.get('theatre'),
            'date': meta.get('date'),
            'clock': sheet.clock_text(meta.get('clock')),
            'now': now.strftime('%Y-%b-%d (%H:%M:%S)'),
        }

        # The plotter has no fallback branch for an unknown airframe: it would
        # get partway through the AoA panel and raise on an unbound `xmax`.
        # Checked here so an unsupported type costs the picture, not the pass.
        image = None
        if sheet.plot_key(unit_type) is None:
            self.log.info(f"JSH TrapSheet: {unit_type or 'unknown type'} has no AoA "
                          f"reference in greenieboard's plotter, so {player.name}'s "
                          f"pass is stored without a trapsheet image.")
        else:
            pinfo = {'aircraft': sheet.aircraft_label(unit_type),
                     'callsign': player.name,
                     'time': now.strftime("%Y-%m-%d %H:%M:%S")}
            try:
                image = await asyncio.to_thread(
                    sheet.render, sheet.to_trapsheet(points, config.get('axes')),
                    pinfo, sheet.trapfile_name(unit_type, player.name), info)
            except Exception as ex:
                self.log.warning(f"JSH TrapSheet: could not plot the pass for "
                                 f"{player.name}: {ex}")

        trapdata = Jsonb({'points': points, 'source': 'jsh_trapsheet', 'meta': meta})
        if row_id is not None:
            written = await self._set(row_id, image, trapdata, tgroove)
        elif grade is not None:
            written = await self._insert(server, player, unit_type, grade, comment,
                                         pass_.get('carrier') or '', wire, case,
                                         night, score, image, trapdata, tgroove)
        else:
            # Nothing to match greenieboard's row on, and inserting our own
            # would risk the double row FIND exists to prevent. Post the
            # picture, keep the database as greenieboard left it.
            written = None

        self.log.info(f"JSH TrapSheet: {player.name} on {server.name}, "
                      f"{len(points)} samples, grade={grade or 'none'}, "
                      f"traps row={written if written is not None else 'none'}")

        await self.post(server, player, config, image, grade, info,
                        sheet.decode(details, errors, marks),
                        (grades.get(grade) or {}).get('grade'))

    @staticmethod
    def _night(server: Server, clock) -> bool:
        """
        Night as greenieboard reckons it: 20:00 to 06:00 mission time. The
        recorder's own clock reading at touchdown is used when it is there.
        """
        try:
            if clock is None:
                mission = server.current_mission
                clock = int(mission.start_time) + int(mission.mission_time)
            s = int(clock) % 86400
            return s > 20 * 3600 or s < 6 * 3600
        except (TypeError, ValueError, AttributeError):
            return False

    async def _find(self, server: Server, player: Player, grade: str):
        """
        greenieboard's row for this pass: (id, points, case, night), or None.

        greenieboard reacts to the same LSO event and nothing orders the two
        plugins, so its row may not be written yet. Retry for a few seconds
        before concluding it never will be -- which is the case when FunkMan
        is enabled in greenieboard.yaml and Super Carrier events are ignored.
        """
        params = {'ucid': player.ucid, 'mission_id': server.mission_id,
                  'grade': grade, 'window': sql.WINDOW}
        for _ in range(ATTACH_TRIES):
            async with self.apool.connection() as conn:
                cursor = await conn.execute(sql.FIND, params)
                found = await cursor.fetchone()
            if found:
                return found
            await asyncio.sleep(ATTACH_DELAY)
        return None

    async def _set(self, row_id: int, image: bytes | None, trapdata, tgroove) -> int | None:
        async with self.apool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(sql.SET, {
                    'id': row_id, 'image': psycopg.Binary(image) if image else None,
                    'trapdata': trapdata, 'tgroove': tgroove})
                found = await cursor.fetchone()
        return found[0] if found else None

    async def _insert(self, server: Server, player: Player, unit_type: str,
                      grade: str, comment: str, carrier: str, wire, case: int,
                      night: bool, score, image: bytes | None, trapdata,
                      tgroove) -> int | None:
        """greenieboard wrote no row for this pass, so write one shaped like its."""
        async with self.apool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(sql.INSERT, {
                    'mission_id': server.mission_id, 'ucid': player.ucid,
                    'unit_type': unit_type, 'grade': grade, 'comment': comment,
                    'place': carrier, 'case': case, 'wire': wire,
                    'night': night, 'points': score,
                    'image': psycopg.Binary(image) if image else None,
                    'trapdata': trapdata, 'tgroove': tgroove})
                found = await cursor.fetchone()
        return found[0] if found else None

    # ------------------------------------------------------------------- post
    async def post(self, server: Server, player: Player, config: dict,
                   image: bytes | None, grade: str | None, info: dict,
                   described: list, meaning: str | None) -> None:
        """
        Put the trapsheet in a Discord channel.

        greenieboard posts a one-line text message to the server's events
        channel and never attaches the picture -- the image it stores is only
        seen if somebody runs /traps. That is the gap this fills.

        `channel` is optional. Unset means post nothing, which is the right
        default for an instance where this would be noise.
        """
        if image is None:
            return
        channel_id = config.get('channel')
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            self.log.warning(f"JSH TrapSheet: channel {channel_id} is not "
                             f"reachable; the pass was stored but not posted.")
            return

        # The type recorded WITH the pass: by now the pilot may have changed
        # slot, and player.unit_type would name the wrong airframe.
        case = info.get('case')
        title = ' | '.join(x for x in (
            player.name,
            sheet.aircraft_label(info.get('unit_type') or ''),
            f"CASE {ROMAN.get(case, case)}" if case else None,
            'Night' if case == 3 else 'Day' if case else None) if x)

        embed = discord.Embed(title=title, colour=sheet.colour_of(grade),
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="LSO Grade",
                        value=f"**{grade}** — {meaning}" if grade and meaning
                        else sheet.grade_line(grade), inline=False)
        if info.get('details'):
            # A backtick in the comment would close the code span early.
            embed.add_field(name="LSO Comment",
                            value=f"`{info['details'].replace('`', "'")}`", inline=False)
        if described:
            text = '\n'.join(f"**{where}:** {', '.join(lines)}" for where, lines in described)
            embed.add_field(name="LSO Comment Description", value=text[:1024], inline=False)

        def add(name, value):
            if value not in (None, ''):
                embed.add_field(name=name, value=str(value), inline=True)

        add("Points", f"{float(info['points']):g}" if info.get('points') is not None else None)
        add("Wire", info.get('wire'))
        add("Groove", f"{info['tgroove']:.1f}s" if info.get('tgroove') else None)
        add("Wind", f"{float(info['wind']):.0f} kn" if info.get('wind') is not None else None)
        add("Carrier", ' '.join(x for x in (info.get('carrier_name'),
                                            f"({info['hull']})" if info.get('hull') else None) if x))
        add("Aircraft", info.get('unit_type'))
        embed.set_image(url="attachment://trapsheet.png")
        files = [discord.File(io.BytesIO(image), filename='trapsheet.png')]
        if info.get('points') is not None:
            # FunkMan's "N PTS" badge, top right. Drawn, because an embed's
            # top-right corner only takes an image. A failure costs the badge.
            try:
                files.append(discord.File(io.BytesIO(sheet.badge(info['points'], grade)),
                                          filename='points.png'))
                embed.set_thumbnail(url="attachment://points.png")
            except Exception as ex:
                self.log.warning(f"JSH TrapSheet: could not draw the points badge: {ex}")
        where = ' '.join(x for x in (
            f"{info['theatre']}:" if info.get('theatre') else None,
            info.get('date'),
            f"({info['clock']})" if info.get('clock') else None) if x)
        embed.set_footer(text=' · '.join(x for x in (where, server.name) if x))

        try:
            await channel.send(embed=embed, files=files)
        except discord.DiscordException as ex:
            # A posting failure must not lose the pass: it is already stored.
            self.log.warning(f"JSH TrapSheet: could not post {player.name}'s "
                             f"pass to {channel_id}: {ex}")


ROMAN = {1: 'I', 2: 'II', 3: 'III'}


async def setup(bot: DCSServerBot):
    await bot.add_cog(TrapSheet(bot, TrapSheetEventListener))
