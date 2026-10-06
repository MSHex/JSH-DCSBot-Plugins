import asyncio
import discord
import os
import uuid

from openpyxl import load_workbook

from core import Plugin, Group, utils, Server, Status
from discord import app_commands
from services.bot import DCSServerBot
from typing import Optional

from .listener import AirportEventListener

RUNNING = [Status.RUNNING, Status.PAUSED]
GM_ROLES = ['DCS Admin', 'GameMaster']


# ---------------------------------------------------------------- Lua helpers
def lua_str(value) -> str:
    s = str(value)
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
    return f'"{s}"'


def lua_value(value) -> str:
    if value is None:
        return 'nil'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return '{' + ', '.join(f'[{lua_str(k)}] = {lua_value(v)}' for k, v in value.items()) + '}'
    if isinstance(value, (list, tuple)):
        return '{' + ', '.join(lua_value(v) for v in value) + '}'
    return lua_str(value)


# ---------------------------------------------------------------- autocomplete
async def _server(interaction: discord.Interaction) -> Optional[Server]:
    try:
        return await utils.ServerTransformer().transform(
            interaction, utils.get_interaction_param(interaction, 'server'))
    except Exception:
        return None


async def airbase_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Airbases in the running mission, with their stored level where there is one."""
    server = await _server(interaction)
    if not server:
        return []
    plugin: Airport = interaction.client.cogs['Airport']

    async with plugin.apool.connection() as conn:
        cursor = await conn.execute(
            "SELECT airbase, level FROM jsh_airport_levels WHERE server_name = %s", (server.name,))
        levels = {r[0]: r[1] for r in await cursor.fetchall()}

    live = plugin.airbases.get(server.name)
    problem = None
    if live is None:
        # First use on this mission: ask for the list, but don't let Discord time out.
        try:
            live, problem = await plugin.refresh_airbases(server, timeout=2, explain=True)
        except Exception as ex:
            live, problem = [], str(ex)

    choices = []
    if live:
        sides = {0: 'neutral', 1: 'red', 2: 'blue'}
        for base in live:
            name = base.get('name')
            if not name or current.lower() not in name.lower():
                continue
            label = f"{name} [{sides.get(base.get('coalition'), '?')}]"
            if base.get('category') and base['category'] != 'AIRDROME':
                label += f" {base['category']}"
            if name in levels:
                label += f" - level {levels[name]}"
            choices.append(app_commands.Choice(name=label[:100], value=name))
    else:
        choices = [app_commands.Choice(name=f"{n} (level {lvl})", value=n)
                   for n, lvl in sorted(levels.items()) if current.lower() in n.lower()]
        if not choices:
            # Nothing live and nothing stored: an empty dropdown with no reason is
            # the worst outcome, so put the reason in front of the GM. Typing a
            # name still works, and the command itself reports the real error.
            if problem == "still loading":
                label = "[loading airbases from the mission - type another letter]"
            else:
                label = f"[cannot read airbases: {problem or 'the mission returned none'}]"
            choices = [app_commands.Choice(name=label[:100], value=current or "?")]
    return choices[:25]


# ---------------------------------------------------------------- plugin
class Airport(Plugin[AirportEventListener]):

    def __init__(self, bot: DCSServerBot, eventlistener=None):
        super().__init__(bot, eventlistener)
        self.pending: dict[str, asyncio.Future] = {}
        self.airbases: dict[str, list] = {}      # server name -> [{name, category, coalition}]

    def enabled(self, server: Server) -> bool:
        """enabled: false in DEFAULT or in the server's instance section turns the plugin off there."""
        return bool(self.get_config(server).get('enabled', True))

    async def refuse_if_disabled(self, interaction: discord.Interaction, server: Server) -> bool:
        if self.enabled(server):
            return False
        await interaction.followup.send(
            f"Airport is disabled on {server.name} (see jsh_airport.yaml).", ephemeral=True)
        return True

    async def warehouse_request(self, server: Server, method: str, args: str, timeout: int = 15) -> dict:
        """Calls jsh_airport.<method>(request_id, <args>) in the mission and waits for its reply."""
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        call = (f'jsh_airport.{method}({lua_str(request_id)}'
                f'{", " + args if args else ""})')
        # If the mission is running an older mission.lua, reply with a clear
        # message instead of raising a Lua error in the mission.
        await server.send_to_dcs({
            "command": "jshAirportRun",
            "lua": (f'if jsh_airport and jsh_airport.{method} then {call} '
                    f'elseif dcsbot and dcsbot.sendBotTable then '
                    f'dcsbot.sendBotTable({{ command = "jshAirportResult", '
                    f'request_id = {lua_str(request_id)}, ok = false, '
                    f'error = "mission.lua is out of date or not loaded - restart the mission" }}) end')
        })
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "timeout": True,
                    "error": "no reply from the mission (is the plugin's mission.lua loaded?)"}
        finally:
            self.pending.pop(request_id, None)

    async def refresh_airbases(self, server: Server, timeout: int = 15, explain: bool = False):
        """
        Asks the mission for its airbase list and caches it.

        With explain=True returns (airbases, problem) so a caller can tell an
        empty list caused by a failure from a mission that genuinely has none.
        """
        result = await self.warehouse_request(server, "listAirbases", "", timeout=timeout)
        problem = None
        if result.get('ok') and result.get('airbases'):
            self.airbases[server.name] = result['airbases']
            skipped = result.get('skipped') or 0
            if skipped:
                self.log.warning(f"Airport: {server.name} skipped {skipped} airbase(s) "
                                 f"the mission could not describe")
        elif result.get('timeout'):
            # Distinguishes a slow round trip from a broken one: the reply often
            # lands just after a short autocomplete timeout, and the listener
            # caches it when it does.
            problem = "still loading"
        elif not result.get('ok'):
            problem = result.get('error') or "the mission did not reply"
            self.log.warning(f"Airport: could not list airbases on {server.name}: {problem}")
        else:
            problem = "the mission reported no airbases"
        bases = self.airbases.get(server.name, [])
        return (bases, problem) if explain else bases

    # ------------------------------------------------------------ warehouse sheets
    def sheet_path(self, server: Server, level: int) -> str:
        cfg = self.get_config(server)
        folder = cfg.get('warehouse_dir', os.path.join('config', 'plugins', self.plugin_name))
        name = cfg.get('warehouse_file', 'warehouse-lvl{level}.xlsx').format(level=level)
        return os.path.join(folder, name)

    @staticmethod
    def read_sheet(path: str) -> dict:
        """Reads an /airbase info export: sheets Aircraft, Weapons, Liquids with columns Name, Count."""
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            items, liquids = {}, {}
            for sheet in ('Aircraft', 'Weapons', 'Liquids'):
                if sheet not in wb.sheetnames:
                    continue
                for row in wb[sheet].iter_rows(min_row=2, values_only=True):
                    if not row or row[0] is None or len(row) < 2 or row[1] is None:
                        continue
                    target = liquids if sheet == 'Liquids' else items
                    target[str(row[0]).strip()] = int(float(row[1]))
            return {"items": items, "liquids": liquids}
        finally:
            wb.close()

    async def apply_sheet(self, server: Server, airbase: str, level: int) -> dict:
        """Applies warehouse-lvl<level>.xlsx to the airbase, in chunks. Returns {ok, error, failed}."""
        path = self.sheet_path(server, level)
        if not os.path.exists(path):
            return {"ok": False, "error": f"sheet not found: {path}"}
        try:
            sheet = await asyncio.to_thread(self.read_sheet, path)
        except Exception as ex:
            return {"ok": False, "error": f"could not read {path}: {ex}"}

        names = list(sheet["items"].items())
        chunks = [names[i:i + 200] for i in range(0, len(names), 200)] or [[]]
        failed: list[str] = []
        for i, chunk in enumerate(chunks):
            preset = {"items": dict(chunk)}
            if i == 0:
                preset["liquids"] = sheet["liquids"]
            result = await self.warehouse_request(
                server, "applyWarehouse", f"{lua_str(airbase)}, {lua_value(preset)}")
            if not result.get('ok'):
                if result.get('failed') is None:
                    return {"ok": False, "error": result.get('error')}   # airbase missing, no reply, ...
                failed.extend(result['failed'])
        return {"ok": True, "failed": failed}

    async def damage_warehouse(self, server: Server, airbase: str, percent: int) -> dict:
        """
        Deducts `percent` of whatever is in the airbase warehouse right now.

        Reads live quantities in the mission rather than a level sheet, so what
        players have already used is accounted for: a base down to half a sheet
        loses half of that half, not half of the sheet.
        """
        keep = max(0.0, 1.0 - percent / 100.0)
        return await self.warehouse_request(
            server, "damageWarehouse", f"{lua_str(airbase)}, {keep!r}")

    @staticmethod
    def failed_note(result: dict) -> str:
        failed = result.get('failed') or []
        if not failed:
            return ""
        shown = ", ".join(failed[:10]) + (f" (+{len(failed) - 10} more)" if len(failed) > 10 else "")
        return f"\n{len(failed)} items not accepted by DCS: {shown}"

    # ------------------------------------------------------------ level storage
    async def get_level(self, server: Server, airbase: str) -> Optional[int]:
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT level FROM jsh_airport_levels WHERE server_name = %s AND airbase = %s",
                (server.name, airbase))
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_level(self, server: Server, airbase: str, level: int, by: str) -> None:
        async with self.apool.connection() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO jsh_airport_levels (server_name, airbase, level, updated_by)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (server_name, airbase) DO UPDATE
                    SET level = EXCLUDED.level, updated_by = EXCLUDED.updated_by,
                        updated_at = now() AT TIME ZONE 'utc'
                """, (server.name, airbase, level, by))

    # ------------------------------------------------------------ commands
    airport_group = Group(name="airport", description="Airbase warehouse levels")

    @airport_group.command(description='Set an airbase warehouse to a level')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    @app_commands.autocomplete(airbase=airbase_autocomplete)
    @app_commands.choices(level=[
        app_commands.Choice(name="Level 0 - stripped", value=0),
        app_commands.Choice(name="Level 1 - resupplied", value=1),
        app_commands.Choice(name="Level 2 - forward base", value=2),
        app_commands.Choice(name="Level 3 - logistics hub", value=3),
    ])
    async def level(self, interaction: discord.Interaction,
                    server: app_commands.Transform[Server, utils.ServerTransformer(status=RUNNING)],
                    airbase: str, level: app_commands.Choice[int]):
        await interaction.response.defer(ephemeral=True)
        if await self.refuse_if_disabled(interaction, server):
            return
        current = await self.get_level(server, airbase)
        result = await self.apply_sheet(server, airbase, level.value)
        if not result.get('ok'):
            await interaction.followup.send(
                f"Warehouse update failed: {result.get('error')}", ephemeral=True)
            return
        await self.set_level(server, airbase, level.value, interaction.user.display_name)
        was = "unset" if current is None else f"level {current}"
        await interaction.followup.send(
            f"{airbase} on {server.name}: {was} -> level {level.value}.{self.failed_note(result)}",
            ephemeral=True)

    @airport_group.command(description='Deduct a percentage from an airbase warehouse (battle damage)')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    @app_commands.autocomplete(airbase=airbase_autocomplete)
    @app_commands.choices(percent=[
        app_commands.Choice(name="25% - light damage", value=25),
        app_commands.Choice(name="50% - heavy damage", value=50),
        app_commands.Choice(name="75% - severe damage", value=75),
        app_commands.Choice(name="100% - destroyed (sets level 0)", value=100),
    ])
    async def damage(self, interaction: discord.Interaction,
                     server: app_commands.Transform[Server, utils.ServerTransformer(status=RUNNING)],
                     airbase: str, percent: app_commands.Choice[int]):
        await interaction.response.defer(ephemeral=True)
        if await self.refuse_if_disabled(interaction, server):
            return

        result = await self.damage_warehouse(server, airbase, percent.value)
        if not result.get('ok'):
            await interaction.followup.send(
                f"Warehouse damage failed: {result.get('error')}", ephemeral=True)
            return

        before, after = result.get('items_before', 0), result.get('items_after', 0)
        fuel_before, fuel_after = result.get('fuel_before', 0), result.get('fuel_after', 0)

        if before == 0 and fuel_before == 0:
            await interaction.followup.send(
                f"{airbase} on {server.name}: nothing to deduct - the warehouse is "
                f"already empty, or set to unlimited in the mission editor.",
                ephemeral=True)
            return

        lines = [f"{airbase} on {server.name}: {percent.value}% damage applied.",
                 f"Items: {before:,} -> {after:,}",
                 f"Liquids: {fuel_before:,} -> {fuel_after:,}"]

        # 100% means the base can no longer spawn anything, so its level is 0.
        if percent.value == 100:
            current = await self.get_level(server, airbase)
            await self.set_level(server, airbase, 0, interaction.user.display_name)
            was = "unset" if current is None else f"level {current}"
            lines.append(f"Level: {was} -> 0 (destroyed).")

        await interaction.followup.send(
            "\n".join(lines) + self.failed_note(result), ephemeral=True)

    @airport_group.command(description='Show airbase levels on a server')
    @app_commands.guild_only()
    @utils.app_has_roles(GM_ROLES)
    async def status(self, interaction: discord.Interaction,
                     server: app_commands.Transform[Server, utils.ServerTransformer]):
        await interaction.response.defer(ephemeral=True)
        async with self.apool.connection() as conn:
            cursor = await conn.execute(
                "SELECT airbase, level, updated_by, updated_at FROM jsh_airport_levels "
                "WHERE server_name = %s ORDER BY level DESC, airbase", (server.name,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.followup.send(f"No airbase levels set on {server.name} yet.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Airbase levels - {server.name}", color=discord.Color.blue())
        embed.add_field(name="Airbase", value="\n".join(r[0] for r in rows), inline=True)
        embed.add_field(name="Level", value="\n".join(str(r[1]) for r in rows), inline=True)
        embed.add_field(name="Set by", value="\n".join(f"{r[2]} ({r[3]:%d %b})" for r in rows), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Airport(bot, AirportEventListener))
