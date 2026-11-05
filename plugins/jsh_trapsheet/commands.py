import asyncio
import io
import json
import os

import matplotlib
matplotlib.use('Agg')                      # no display on a headless node
import matplotlib.pyplot as plt            # noqa: E402
import psycopg                             # noqa: E402

from core import Plugin, Server, Player, Status          # noqa: E402
from datetime import datetime, timezone                  # noqa: E402
from psycopg.types.json import Jsonb                     # noqa: E402
from services.bot import DCSServerBot                    # noqa: E402

from . import sheet                                      # noqa: E402
from .listener import TrapSheetEventListener             # noqa: E402

#: do_script goes bot -> DCS, where UDPRecvSocket:receive() reads into a fixed
#: buffer and truncates SILENTLY. The samples come back the other way, through
#: dcsbot.sendBotTable, which chunks at 65000 and is reassembled by the bot --
#: that direction is not constrained, so only the injection is checked here.
DATAGRAM_LIMIT = 8192
DATAGRAM_MARGIN = 400

RUNNING = [Status.RUNNING, Status.PAUSED]


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

    # ----------------------------------------------------------------- drawing
    @staticmethod
    def render(ts: dict, pinfo: dict, trapfile: str) -> bytes:
        """
        The trapsheet PNG, drawn by greenieboard's own plotter.

        `trapfile` is a synthetic name, never a real path -- the plotter opens
        nothing, it only substring-matches the stem to choose the AoA
        reference band. See sheet.trapfile_name().

        Imported here rather than at module level: greenieboard is a stock
        plugin and always present, but a module-level import would make this
        plugin fail to LOAD rather than fail to draw if that ever stopped
        being true, and a missing picture is a far better outcome than a
        plugin that will not start.
        """
        from plugins.greenieboard.trapsheet import plot_trapsheet

        fig, axs = plt.subplots(3, 1, sharex=True, facecolor="#404040", dpi=150)
        fig.set_size_inches(8, 6)
        try:
            plot_trapsheet(axs, ts, pinfo, trapfile)
            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            return buf.getvalue()
        finally:
            plt.close(fig)

    # ------------------------------------------------------------------- store
    async def store(self, server: Server, player: Player, pass_: dict,
                    grade: str | None, comment: str) -> None:
        """
        Draw the pass and write it to greenieboard's `traps` table.

        Writing there rather than to a table of our own is deliberate: the
        existing /greenieboard and /traps commands then show these passes with
        their picture, instead of there being two boards to look at.
        """
        config = self.get_config(server)
        points = pass_.get('points') or []
        unit_type = pass_.get('unit_type') or player.unit_type or ''

        ts = sheet.to_trapsheet(points, config.get('axes'))
        pinfo = {
            'aircraft': sheet.aircraft_label(unit_type),
            'callsign': player.name,
            'time': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }

        # The plotter has no fallback branch for an unknown airframe: it would
        # get partway through the AoA panel and raise on an unbound `xmax`.
        # Checked here so an unsupported type costs the picture, not the pass.
        if sheet.plot_key(unit_type) is None:
            self.log.info(f"JSH TrapSheet: {unit_type or 'unknown type'} has no AoA "
                          f"reference in greenieboard's plotter, so {player.name}'s "
                          f"pass is stored without a trapsheet image.")
            image = None
        else:
            try:
                image = await asyncio.to_thread(
                    self.render, ts, pinfo,
                    sheet.trapfile_name(unit_type, player.name))
            except Exception as ex:
                self.log.warning(f"JSH TrapSheet: could not plot the pass for "
                                 f"{player.name}: {ex}")
                image = None

        tgroove = sheet.groove_time(points, float(config.get('sample_rate', 0.1)))
        wire = sheet.wire_of(comment)
        night = bool(config.get('night', False))

        async with self.apool.connection() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO traps (mission_id, player_ucid, unit_type, grade,
                                       comment, place, trapcase, wire, night,
                                       points, trapsheet, trapdata, tgroove)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (server.mission_id, player.ucid, unit_type,
                      # An ungraded pass stores an EMPTY grade, never '--'.
                      # '--' is a real LSO no-grade, and writing it for a
                      # legacy deck that simply cannot grade would quietly
                      # push everyone's greenieboard average down with passes
                      # nobody actually flew badly. The column is NOT NULL, so
                      # '' is the honest value: it matches no grade bucket in
                      # any greenieboard report, and points stays NULL.
                      grade or '', comment, pass_.get('carrier') or '',
                      1, wire, night, sheet.points_of(grade),
                      psycopg.Binary(image) if image else None,
                      Jsonb({'points': points, 'source': 'jsh_trapsheet'}),
                      tgroove))

        self.log.info(f"JSH TrapSheet: stored a pass for {player.name} on "
                      f"{server.name} ({len(points)} samples, "
                      f"grade={grade or 'none'})")


async def setup(bot: DCSServerBot):
    await bot.add_cog(TrapSheet(bot, TrapSheetEventListener))
