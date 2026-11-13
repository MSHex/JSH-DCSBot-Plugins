import discord
import os

from core import Plugin, Group, utils
from discord import app_commands
from ruamel.yaml import YAML
from services.bot import DCSServerBot
from typing import Optional

from . import checks
from .checks import Finding

ADMIN_ROLES = ['DCS Admin', 'Admin']

#: Foothold's external config file names. Normandy uses the WW2 one.
FOOTHOLD_CONFIGS = ('Foothold Config.lua', 'Foothold Config WW2.lua')

SQL_REVERSED = "SELECT COUNT(*) FROM statistics WHERE hop_off < hop_on"
SQL_STALE_OPEN = """
    SELECT COUNT(*) FROM statistics
     WHERE hop_off IS NULL AND hop_on < (now() AT TIME ZONE 'utc') - interval '1 day'
"""
SQL_CQ_REQUIREMENTS = """
    SELECT name FROM logbook_qualifications
     WHERE name = ANY(%s) AND requirements IS NOT NULL
       AND requirements::text NOT IN ('null', '{}')
"""


class Doctor(Plugin):
    """
    /jsh doctor: checks JSH's setup for the silent mistakes that have each
    already cost an evening. Read-only -- it changes nothing.
    """

    def _yaml(self, path: str) -> Optional[dict]:
        try:
            with open(path, encoding='utf-8') as f:
                return YAML(typ='safe').load(f) or {}
        except FileNotFoundError:
            return None
        except Exception as ex:
            self.log.warning(f"JSH Doctor: could not read {path}: {ex}")
            return None

    # ------------------------------------------------------------ the checks
    def check_config(self) -> list[Finding]:
        config_dir = self.node.config_dir
        out = [checks.validation(self.node.config)]
        nodes = checks.instances_by_node(self._yaml(os.path.join(config_dir, 'nodes.yaml')) or {})
        servers = list(self.bus.servers.keys())
        plugin_dir = os.path.join(config_dir, 'plugins')
        problems = []
        for name in sorted(os.listdir(plugin_dir)) if os.path.isdir(plugin_dir) else []:
            if not name.endswith('.yaml'):
                continue
            data = self._yaml(os.path.join(plugin_dir, name))
            if isinstance(data, dict):
                problems += checks.config_sections(name[:-5], data, nodes, servers)
        out += problems or [Finding(checks.OK, 'config',
                                    "Every plugin config section names a real instance.")]
        return out

    async def check_foothold(self) -> list[Finding]:
        """
        Through the server's own node, so an instance on the AGENT is checked
        as well as one here: list_directory and read_file are the bot's node
        API, run locally on this node and by RPC on the other. Nothing extra
        has to be installed on the agent beyond the bot itself.
        """
        out = []
        ranks_loaded = 'Ranks' in self.bot.cogs
        seen = set()
        for server in self.bus.servers.values():
            if not server.instance or not server.instance.home or server.instance.name in seen:
                continue
            seen.add(server.instance.name)
            instance = server.instance.name
            where = f" (on {server.node.name})" if server.is_remote else ""
            saves = os.path.join(server.instance.home, 'Missions', 'Saves')
            try:
                _, paths = await server.node.list_directory(saves, pattern='Foothold*Config*.lua')
            except Exception as ex:
                out.append(Finding(checks.SKIP, 'foothold',
                                   f"{instance}{where}: could not list its files -- is the node "
                                   f"online? ({type(ex).__name__})"))
                continue
            names = [p.replace('\\', '/').rsplit('/', 1)[-1] for p in paths]
            found = checks.foothold_files(instance + where, names)
            if not found:
                continue                    # no Foothold config here at all
            out += found
            if not ranks_loaded:
                continue
            for config in FOOTHOLD_CONFIGS:
                if config not in names:
                    continue
                try:
                    data = await server.node.read_file(os.path.join(saves, config))
                except Exception as ex:
                    out.append(Finding(checks.SKIP, 'foothold',
                                       f"{instance}{where}: could not read `{config}` "
                                       f"({type(ex).__name__})."))
                    continue
                text = data.decode('utf-8', errors='replace') if isinstance(data, bytes) else None
                finding = checks.ranking_system(instance + where, text)
                if finding:
                    out.append(finding)
        return out

    def check_discord(self) -> list[Finding]:
        out = [checks.members_intent(bool(self.bot.intents.members))]
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            return out + [Finding(checks.FAIL, 'discord', "The bot is in no guild.")]
        bot_top = guild.me.top_role.position
        ranks = self.bot.cogs.get('Ranks')
        if ranks:
            def position(token) -> Optional[int]:
                role = self.bot.get_role(token)
                return role.position if role else None
            try:
                ladder = ranks.earned_ladder()
                out += checks.role_order(bot_top, {n: position(n) for n in ladder}, "Rank roles")
            except Exception as ex:
                out.append(Finding(checks.WARN, 'discord', f"Could not read the rank ladder: {ex}"))
            officer = set()
            for step in (ranks.get_config().get('officer_roles') or {}).values():
                for action in ('add', 'remove'):
                    officer.update(str(r) for r in ((step or {}).get(action) or []))
            if officer:
                out += checks.role_order(
                    bot_top, {(self.bot.get_role(r).name if self.bot.get_role(r) else r): position(r)
                              for r in officer}, "Officer roles")
        return out

    async def check_database(self) -> list[Finding]:
        cq = self.bot.cogs.get('CarrierQual')
        names = []
        if cq:
            cfg = cq.settings()
            names = [cfg.get('day_qualification', 'CQ_CV_DAY'),
                     cfg.get('night_qualification', 'CQ_CV_NIGHT')]
        async with self.apool.connection() as conn:
            reversed_ = (await (await conn.execute(SQL_REVERSED)).fetchone())[0]
            stale = (await (await conn.execute(SQL_STALE_OPEN)).fetchone())[0]
            with_requirements = []
            if names and (await (await conn.execute(
                    "SELECT to_regclass('logbook_qualifications') IS NOT NULL")).fetchone())[0]:
                cursor = await conn.execute(SQL_CQ_REQUIREMENTS, (names,))
                with_requirements = [r[0] for r in await cursor.fetchall()]
        return checks.database(int(reversed_), int(stale), with_requirements)

    async def run(self) -> list[Finding]:
        findings: list[Finding] = []
        for step in (self.check_config, self.check_discord):
            try:
                findings += step()
            except Exception as ex:
                self.log.exception(f"JSH Doctor: {step.__name__} failed: {ex}")
                findings.append(Finding(checks.WARN, step.__name__[6:], f"Check failed: {ex}"))
        try:
            findings += await self.check_foothold()
        except Exception as ex:
            self.log.exception(f"JSH Doctor: check_foothold failed: {ex}")
            findings.append(Finding(checks.WARN, 'foothold', f"Check failed: {ex}"))
        try:
            findings += await self.check_database()
        except Exception as ex:
            findings.append(Finding(checks.WARN, 'database', f"Check failed: {ex}"))
        return findings

    # -------------------------------------------------------------- commands
    jsh = Group(name="jsh", description="JSH plugin tools")

    @jsh.command(name='doctor', description='Check the JSH setup for known silent mistakes')
    @app_commands.guild_only()
    @utils.app_has_roles(ADMIN_ROLES)
    @app_commands.describe(everything='Also list the checks that passed.')
    async def doctor(self, interaction: discord.Interaction, everything: bool = False):
        await interaction.response.defer(ephemeral=True)
        findings = await self.run()
        shown = findings if everything else [f for f in findings
                                             if f.level in (checks.FAIL, checks.WARN)]
        embed = discord.Embed(
            title=f"JSH doctor — {self.node.name}",
            description=checks.summary(findings),
            colour=(discord.Colour.red() if any(f.level == checks.FAIL for f in findings)
                    else discord.Colour.orange() if any(f.level == checks.WARN for f in findings)
                    else discord.Colour.green()))
        for area in ('config', 'foothold', 'discord', 'database'):
            lines = [f.line() for f in shown if f.area == area]
            if lines:
                text = '\n'.join(lines)
                embed.add_field(name=area.capitalize(),
                                value=text if len(text) <= 1024 else text[:1000] + '\n…',
                                inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Doctor(bot))
