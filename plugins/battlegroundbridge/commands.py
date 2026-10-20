import asyncio
import discord
import uuid

from core import Plugin, Group, utils, Server, Status, Player
from discord import app_commands
from services.bot import DCSServerBot
from typing import Optional

from .domain import (DeleteById, DeleteThisMarker, ImportSummary, IntelUpsert, ParseError,
                     parse_marker_command)
from .listener import BridgeEventListener
from .store import (AlreadyDone, BridgeStore, Created, Deleted, Denied, NotFound, Updated)

RUNNING = [Status.RUNNING, Status.PAUSED]

#: The original clamp, kept. Below three seconds the bot spends its time asking
#: a question whose answer is almost always "nothing"; above a minute players
#: assume their marker command failed and type it again.
POLL_MIN, POLL_MAX = 3, 60
DEFAULT_ADMIN_ROLES = ['DCS Admin', 'GameMaster']


class BattlegroundBridge(Plugin[BridgeEventListener]):

    def __init__(self, bot: DCSServerBot, eventlistener=None):
        super().__init__(bot, eventlistener)
        self.pending: dict[str, asyncio.Future] = {}
        self.store = BridgeStore(self.apool, self.log)
        self.poll_tasks: dict[str, asyncio.Task] = {}
        #: server name -> last ImportSummary, for /bgbridge status
        self.last_import: dict[str, ImportSummary] = {}
        self.tokens: dict[str, str] = {}

    async def cog_unload(self) -> None:
        for task in self.poll_tasks.values():
            task.cancel()
        await super().cog_unload()

    # ------------------------------------------------------------- config

    def enabled(self, server: Server) -> bool:
        """
        Off unless the instance section turns it on AND the server is in that
        section's allowlist.

        Two gates rather than one because a website display alias is not
        necessarily the DCS server name, and the consequence of getting it wrong
        is writing one server's intel onto another's map.
        """
        config = self.get_config(server)
        if not config.get('enabled', False):
            return False
        allowed = config.get('server_names') or []
        return server.name in allowed if allowed else False

    def poll_seconds(self, server: Server) -> int:
        value = int(self.get_config(server).get('poll_seconds', 5) or 5)
        return max(POLL_MIN, min(POLL_MAX, value))

    def admin_roles(self, server: Server) -> list[str]:
        return self.get_config(server).get('admin_roles', DEFAULT_ADMIN_ROLES)

    def is_admin(self, server: Server, player: Optional[Player], ucid: Optional[str]) -> bool:
        config = self.get_config(server)
        if ucid and ucid in (config.get('admin_ucids') or []):
            return True
        # A linked Discord account with a configured role. An unlinked player is
        # never an administrator here, however their name is spelled in game.
        member = getattr(player, 'member', None) if player else None
        return bool(member and utils.check_roles(self.admin_roles(server), member))

    # -------------------------------------------------- mission round trip

    async def snapshot(self, server: Server, timeout: int = 15) -> dict:
        """
        Ask the mission for settled marker events.

        A fresh candidate token goes out with every request. The mission adopts
        it only if it has none, so a reloaded mission takes the new one while a
        still-running mission keeps the token the bot was already using. The bot
        must never send back the token it remembers from a previous mission --
        that would make a new mission inherit the old one's marker mappings.
        """
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await server.send_to_dcs({"command": "jshBgbSnapshot",
                                      "request_id": request_id,
                                      "token": uuid.uuid4().hex})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return {'error': 'no reply from the mission (is the plugin loaded on this node?)'}
        finally:
            self.pending.pop(request_id, None)

    async def ensure_polling(self, server: Server) -> None:
        task = self.poll_tasks.get(server.name)
        if task and not task.done():
            return
        self.poll_tasks[server.name] = asyncio.create_task(self.poll_forever(server))

    async def poll_forever(self, server: Server) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll_seconds(server))
                if server.status not in RUNNING or not self.enabled(server):
                    continue
                await self.poll_once(server)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                # Never let one bad cycle end the loop; the next one may well
                # succeed, and a dead poller is silent.
                self.log.warning(f"BgBridge: poll failed for {server.name}: {ex}")

    async def poll_once(self, server: Server) -> None:
        reply = await self.snapshot(server)
        if reply.get('error'):
            self.log.debug(f"BgBridge: {server.name}: {reply['error']}")
            return

        token = reply.get('token')
        if not token:
            return
        if reply.get('overflowed'):
            self.log.warning(
                f"BgBridge: {server.name} reported a full marker queue "
                f"({reply.get('pending_total')} pending) -- marker commands were dropped")

        if self.tokens.get(server.name) != token:
            # First sight of this mission session.
            self.tokens[server.name] = token
            await self.store.touch_session(server.name, token, reply.get('mission_name'))

        acknowledged = []
        for raw in reply.get('events') or []:
            try:
                if await self.handle_event(server, token, raw, reply.get('input_max')):
                    acknowledged.append({'marker': int(raw['marker']),
                                         'sequence': int(raw['sequence'])})
            except Exception as ex:
                # Left unacknowledged on purpose, so the next cycle retries it.
                self.log.warning(f"BgBridge: marker {raw.get('marker')} failed: {ex}")

        if acknowledged:
            await server.send_to_dcs({"command": "jshBgbAck", "token": token,
                                      "acknowledged": acknowledged})

        hours = int(self.get_config(server).get('intel_expiry_hours', 0) or 0)
        if hours > 0:
            expired = await self.store.expire_intel(server.name, hours)
            if expired:
                self.log.info(f"BgBridge: {server.name}: expired {len(expired)} reports")

    # ----------------------------------------------------- event handling

    async def handle_event(self, server: Server, token: str, raw: dict,
                           input_max: Optional[int]) -> bool:
        """
        Process one marker event. True means "committed -- stop sending it".

        False leaves it pending for the next cycle, which is what a database
        failure gets. A command that is simply not ours, or is rejected, returns
        True: retrying it forever would achieve nothing.
        """
        prefix = self.bot.node.config.get('chat_command_prefix', '-')
        text = raw.get('text') or ''
        command = parse_marker_command(text, prefix)
        if command is None:
            return True

        player = self.resolve_player(server, raw)

        if isinstance(command, ParseError):
            await self.notify(player, f"Intel: {command.reason}")
            return True

        if input_max and int(raw.get('length') or 0) > int(input_max):
            await self.notify(player, f"Intel: that command is too long to send to the bot.")
            return True

        # Identity is required for anything that mutates. The marker's text is
        # not evidence of who wrote it, and GM/spectator marker events routinely
        # arrive with no usable initiator.
        if not player:
            await self.log_unidentified(server, raw, text)
            return True

        ucid, name = player.ucid, player.name
        admin = self.is_admin(server, player, ucid)
        marker_id = int(raw['marker'])
        sequence = int(raw['sequence'])

        if isinstance(command, DeleteById):
            outcome = await self.store.delete_by_id(server.name, command.intel_id, ucid,
                                                    name, admin)
            await self.notify(player, self.describe(outcome, command.intel_id))
            return True

        if isinstance(command, DeleteThisMarker):
            outcome = await self.store.delete_by_marker(server.name, token, marker_id,
                                                       sequence, ucid, name, admin)
            await self.notify(player, self.describe(outcome))
            return True

        if isinstance(command, IntelUpsert):
            if raw.get('lat') is None or not raw.get('mgrs'):
                await self.notify(player, "Intel: I could not work out that marker's position.")
                return True
            outcome = await self.store.upsert_intel(
                server.name, token, marker_id, sequence,
                title=command.title, description=command.description,
                mgrs=raw['mgrs'], lat=float(raw['lat']), lon=float(raw['lon']),
                ucid=ucid, player_name=name)
            await self.notify(player, self.describe(outcome))
            return True

        return True

    def resolve_player(self, server: Server, raw: dict) -> Optional[Player]:
        """
        Match the event's initiator to exactly one active player.

        Both the player name and the unit name must match, and the match must be
        unique. Two pilots sharing a display name is unusual but possible, and
        the failure it would cause -- one of them able to delete the other's
        reports -- is bad enough to justify refusing rather than picking.
        """
        name, unit = raw.get('player_name'), raw.get('unit_name')
        if not name or not unit:
            return None
        matches = [player for player in server.get_active_players()
                   if player.name == name and player.unit_name == unit]
        return matches[0] if len(matches) == 1 else None

    async def log_unidentified(self, server: Server, raw: dict, text: str) -> None:
        prefix = self.bot.node.config.get('chat_command_prefix', '-')
        self.log.info(
            f"BgBridge: {server.name}: ignoring marker {raw.get('marker')} -- no identifiable "
            f"player (name={raw.get('player_name')!r} unit={raw.get('unit_name')!r}). "
            f"GM and spectator marker events often arrive this way; the player can use "
            f"'{prefix}intel delete <id>' in chat, or an admin can use /bgbridge delete.")

    @staticmethod
    async def notify(player: Optional[Player], message: str) -> None:
        if player:
            try:
                await player.sendChatMessage(message)
            except Exception:
                pass

    def describe(self, outcome, requested_id: Optional[int] = None) -> str:
        if isinstance(outcome, Created):
            return (f"Intel #{outcome.intel_id} submitted. Edit the marker to move or "
                    f"retitle it; it stays on the map if you remove the marker.")
        if isinstance(outcome, Updated):
            return f"Intel #{outcome.intel_id} updated."
        if isinstance(outcome, Deleted):
            return f"Intel #{outcome.intel_id} deleted."
        if isinstance(outcome, Denied):
            owner = outcome.owner_name or 'another pilot'
            return f"That report belongs to {owner}; only they or an admin can change it."
        if isinstance(outcome, NotFound):
            target = requested_id or outcome.intel_id
            return (f"No intel #{target} on this server. IDs are per server, and drawings "
                    f"and website entries cannot be deleted this way.")
        if isinstance(outcome, AlreadyDone):
            return "Nothing to do -- that report has already been handled."
        return "Unexpected result; check the bot log."

    # -------------------------------------------------------- drawings

    async def on_new_mission(self, server: Server) -> None:
        """
        Establish the new mission's token first, then import.

        Importing before the token is known would file this mission's drawings
        against the session that just ended, which is the "snapshot from the
        previous mission" trap.
        """
        await asyncio.sleep(15)
        await self.ensure_polling(server)
        reply = await self.snapshot(server)
        token = reply.get('token')
        if not token:
            self.log.warning(f"BgBridge: {server.name}: no mission token after load; "
                             f"skipping the drawing import this mission")
            return
        self.tokens[server.name] = token
        await self.store.touch_session(server.name, token, reply.get('mission_name'))

        config = self.get_config(server)
        if not config.get('retain_intel_on_mission_change', True):
            cleared = await self.store.clear_intel(server.name)
            if cleared:
                self.log.info(f"BgBridge: {server.name}: cleared {cleared} reports "
                              f"(retain_intel_on_mission_change is false)")
        if config.get('import_drawings', True):
            await self.import_drawings(server, token)

    async def import_drawings(self, server: Server, token: str) -> ImportSummary:
        config = self.get_config(server)
        layers = config.get('drawing_layers') or ['Common']
        result = await server.send_to_dcs_sync({"command": "getMissionDrawings"}, timeout=60)
        if result.get('error'):
            self.log.warning(f"BgBridge: {server.name}: {result['error']}")
            return ImportSummary()
        summary = await self.store.replace_drawings(
            server.name, token, _clean(result.get('drawings') or {}), layers,
            author_name='Mission', side='blue')
        self.last_import[server.name] = summary
        self.log.info(f"BgBridge: {server.name}: drawings -- {summary.describe()}")
        for note in summary.skipped:
            self.log.info(f"BgBridge: {server.name}: skipped {note}")
        for note in summary.approximated:
            self.log.info(f"BgBridge: {server.name}: approximated {note}")
        return summary

    # -------------------------------------------------------- commands

    bgbridge = Group(name="bgbridge", description="Battleground bridge")

    @bgbridge.command(description='Reimport the mission drawings for a server')
    @app_commands.guild_only()
    @utils.app_has_roles(DEFAULT_ADMIN_ROLES)
    async def refresh(self, interaction: discord.Interaction,
                      server: app_commands.Transform[Server, utils.ServerTransformer(
                          status=RUNNING)]):
        await interaction.response.defer(ephemeral=True)
        if not self.enabled(server):
            await interaction.followup.send(
                f"The bridge is not enabled for {server.name}.", ephemeral=True)
            return
        token = self.tokens.get(server.name)
        if not token:
            reply = await self.snapshot(server)
            token = reply.get('token')
            if not token:
                await interaction.followup.send(
                    "No mission token yet -- the plugin's Lua may not be loaded on that node.",
                    ephemeral=True)
                return
            self.tokens[server.name] = token
        summary = await self.import_drawings(server, token)
        detail = f"Drawings on {server.name}: {summary.describe()}."
        if summary.skipped:
            detail += "\nSkipped:\n" + '\n'.join(f"- {note}" for note in summary.skipped[:10])
        await interaction.followup.send(detail[:1900], ephemeral=True)

    @bgbridge.command(description='Delete a bridge-created intel report by ID')
    @app_commands.guild_only()
    @utils.app_has_roles(DEFAULT_ADMIN_ROLES)
    async def delete(self, interaction: discord.Interaction,
                     server: app_commands.Transform[Server, utils.ServerTransformer],
                     intel_id: int):
        await interaction.response.defer(ephemeral=True)
        outcome = await self.store.delete_by_id(
            server.name, intel_id, ucid=None,
            player_name=interaction.user.display_name, is_admin=True)
        await interaction.followup.send(self.describe(outcome, intel_id), ephemeral=True)

    @bgbridge.command(description='Show bridge status and the last drawing import')
    @app_commands.guild_only()
    @utils.app_has_roles(DEFAULT_ADMIN_ROLES)
    async def status(self, interaction: discord.Interaction,
                     server: app_commands.Transform[Server, utils.ServerTransformer]):
        await interaction.response.defer(ephemeral=True)
        config = self.get_config(server)
        embed = discord.Embed(title="Battleground bridge", colour=discord.Colour.blue())
        embed.add_field(name="Server", value=server.name, inline=False)
        embed.add_field(name="Enabled", value='yes' if self.enabled(server) else 'no')
        embed.add_field(name="Poll", value=f"{self.poll_seconds(server)}s")
        embed.add_field(name="Mission token",
                        value=f"`{self.tokens.get(server.name, 'none yet')[:12]}`")
        embed.add_field(name="Retain intel across missions",
                        value=str(config.get('retain_intel_on_mission_change', True)))
        expiry = int(config.get('intel_expiry_hours', 0) or 0)
        embed.add_field(name="Expiry", value=f"{expiry}h" if expiry else 'disabled')
        summary = self.last_import.get(server.name)
        embed.add_field(name="Last drawing import",
                        value=summary.describe() if summary else 'none this session', inline=False)
        if summary and summary.skipped:
            embed.add_field(name="Skipped",
                            value='\n'.join(f"- {note}" for note in summary.skipped[:5])[:1000],
                            inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------- shared with chat

    async def delete_for_player(self, server: Server, player: Player, intel_id: int) -> str:
        outcome = await self.store.delete_by_id(
            server.name, intel_id, player.ucid, player.name,
            self.is_admin(server, player, player.ucid))
        return self.describe(outcome, intel_id)


def _clean(drawings: dict) -> dict:
    """
    The mission plugin encodes Lua nil as an empty table, so `{}` arrives where
    a value is absent. Left alone, `radius = {}` would reach float() and raise.
    """
    def convert(value):
        if isinstance(value, dict):
            return None if not value else {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return {layer: [convert(drawing) or {} for drawing in (items or [])]
            for layer, items in drawings.items()}


async def setup(bot: DCSServerBot):
    await bot.add_cog(BattlegroundBridge(bot, BridgeEventListener))
