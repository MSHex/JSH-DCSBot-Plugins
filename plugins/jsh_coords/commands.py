import asyncio
import discord
import uuid

from core import Plugin, Group, utils, Server, Status
from discord import app_commands
from services.bot import DCSServerBot
from typing import Optional

from . import coords as cc
from .listener import CoordsEventListener

RUNNING = [Status.RUNNING, Status.PAUSED]


def lua_num(value: float) -> str:
    """Lua literal for a number. repr() keeps full double precision."""
    return repr(float(value))


def lua_str(value) -> str:
    s = str(value)
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
    return f'"{s}"'


class Coords(Plugin[CoordsEventListener]):

    def __init__(self, bot: DCSServerBot, eventlistener=None):
        super().__init__(bot, eventlistener)
        self.pending: dict[str, asyncio.Future] = {}

    def enabled(self, server: Server) -> bool:
        """enabled: false in DEFAULT or in the server's instance section turns the plugin off there."""
        return bool(self.get_config(server).get('enabled', True))

    # ------------------------------------------------------------ mission round trip

    async def mission_request(self, server: Server, call: str, timeout: int = 15) -> dict:
        """Calls jsh_coords.<call> in the mission and waits for its reply."""
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        lua = (f'if not jsh_coords then dcsbot.sendBotTable({{ '
               f'command = "jshCoordsResult", request_id = {lua_str(request_id)}, '
               f'ok = false, error = "mission.lua is not loaded" }}) else '
               f'jsh_coords.{call.format(request_id=lua_str(request_id))} end')
        try:
            await server.send_to_dcs({"command": "jshCoordsRun", "lua": lua})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return {'ok': False,
                    'error': 'no reply from the mission (is the plugin loaded on this node?)'}
        finally:
            self.pending.pop(request_id, None)

    async def to_metric(self, server: Server, point: cc.LatLon) -> dict:
        return await self.mission_request(
            server,
            f'fromLatLon({{request_id}}, {lua_num(point.lat)}, {lua_num(point.lon)})')

    async def from_metric(self, server: Server, x: float, z: float) -> dict:
        return await self.mission_request(
            server, f'fromMetric({{request_id}}, {lua_num(x)}, {lua_num(z)})')

    # ------------------------------------------------------------ commands

    coords_group = Group(name="coords", description="Convert between coordinate formats")

    @coords_group.command(description='Convert a coordinate into every other format')
    @app_commands.guild_only()
    @app_commands.describe(
        coordinate='MGRS, Lat/Long (any of the three forms), decimal degrees, or DCS metric X/Z',
        server='Only needed for DCS metric X/Z and terrain altitude',
        public='Post the result in the channel instead of only to you')
    async def convert(self, interaction: discord.Interaction, coordinate: str,
                      server: Optional[app_commands.Transform[
                          Server, utils.ServerTransformer(status=RUNNING)]] = None,
                      public: bool = False):
        ephemeral = not public
        await interaction.response.defer(ephemeral=ephemeral)

        if server and not self.enabled(server):
            # The four terrain-independent formats still work, so carry on
            # without the server rather than refusing the whole conversion.
            await interaction.followup.send(
                f"Coords is disabled on {server.name} (see jsh_coords.yaml), so metric and "
                f"altitude are unavailable. Converting the rest.", ephemeral=True)
            server = None

        try:
            point, metric, note = await self.resolve(coordinate, server)
        except cc.CoordError as ex:
            await interaction.followup.send(f"{ex}", ephemeral=True)
            return

        if not point:
            await interaction.followup.send(
                f"I could not read `{coordinate}` as a coordinate.\n"
                f"Try one of:\n"
                f"- MGRS  `32U MV 12345 67890`\n"
                f"- Lat/Long precise  `N 49\u00b026'01.48\" E 8\u00b013'06.09\"`\n"
                f"- Lat/Long decimal minutes  `N 49\u00b026.024' E 8\u00b013.101'`\n"
                f"- Decimal degrees  `49.433744, 8.218358`\n"
                f"- DCS metric  `X-00506931 Z-00890072`  (needs a server)",
                ephemeral=True)
            return

        embed = self.build_embed(coordinate, point, metric, note, server)
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    async def resolve(self, text: str, server: Optional[Server]) -> tuple[
            Optional[cc.LatLon], Optional[dict], Optional[str]]:
        """
        Works out what was pasted and fills in whatever else is obtainable.

        Returns the point, the mission's metric reply (if any) and a note to show
        the user about anything that could not be produced.
        """
        # Metric first: it is the only form that cannot be converted without a
        # mission, so there is no point trying the others on it.
        xz = cc.parse_metric(text)
        if xz:
            if not server:
                raise cc.CoordError(
                    "DCS metric X/Z is terrain-specific, so I need to ask a running "
                    "mission. Add the `server` option and try again.")
            reply = await self.from_metric(server, *xz)
            if not reply.get('ok'):
                raise cc.CoordError(f"The mission could not convert that: {reply.get('error')}")
            point = cc.validate(cc.LatLon(reply['lat'], reply['lon']))
            reply.setdefault('x', xz[0])
            reply.setdefault('z', xz[1])
            return point, reply, None

        grid = cc.parse_mgrs(text)
        if grid:
            try:
                lat, lon = utils.mgrs_to_dd(grid)
            except Exception as ex:
                raise cc.CoordError(f"`{grid}` is not a valid MGRS grid reference: {ex}")
            point = cc.validate(cc.LatLon(lat, lon))
        else:
            point = cc.parse_latlon(text)
            if not point:
                return None, None, None
            cc.validate(point)

        if not server:
            return point, None, ("Metric X/Z and altitude need a running mission -- "
                                 "add the `server` option for those.")
        reply = await self.to_metric(server, point)
        if not reply.get('ok'):
            return point, None, f"Metric and altitude unavailable: {reply.get('error')}"
        return point, reply, None

    def build_embed(self, original: str, point: cc.LatLon, metric: Optional[dict],
                    note: Optional[str], server: Optional[Server]) -> discord.Embed:
        embed = discord.Embed(title="Coordinate conversion", colour=discord.Colour.blue())
        embed.description = f"Input: `{original.strip()}`"

        try:
            grid = utils.dd_to_mgrs(point.lat, point.lon)
        except Exception as ex:
            self.log.warning(f"Coords: MGRS conversion failed for {point}: {ex}")
            grid = "unavailable"
        embed.add_field(name="MGRS", value=f"`{grid}`", inline=False)
        embed.add_field(name="Lat/Long (precise)", value=f"`{cc.to_dms(point)}`", inline=False)
        embed.add_field(name="Lat/Long (standard)", value=f"`{cc.to_dms(point, 0)}`", inline=False)
        embed.add_field(name="Lat/Long decimal minutes", value=f"`{cc.to_ddm(point)}`", inline=False)
        embed.add_field(name="Decimal degrees", value=f"`{cc.to_dd(point)}`", inline=False)

        if metric and metric.get('x') is not None:
            embed.add_field(name="DCS metric",
                            value=f"`{cc.to_metric(metric['x'], metric['z'])}`", inline=False)
            if metric.get('altitude') is not None:
                embed.add_field(name="Terrain altitude",
                                value=cc.format_altitude(metric['altitude']), inline=False)

        footer = []
        if server:
            theatre = (metric or {}).get('theatre')
            footer.append(f"{server.name}{f' ({theatre})' if theatre else ''}")
        if note:
            footer.append(note)
        if footer:
            embed.set_footer(text=" \u2014 ".join(footer))
        return embed


async def setup(bot: DCSServerBot):
    await bot.add_cog(Coords(bot, CoordsEventListener))
