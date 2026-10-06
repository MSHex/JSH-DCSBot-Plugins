from __future__ import annotations

import discord
from discord import app_commands

from core import Group, Plugin, PluginRequiredError, Server, Status, utils
from services.bot import DCSServerBot

from .economy import EconomyService
from .listener import HardcoreEventListener


class Hardcore(Plugin[HardcoreEventListener]):
    hardcore = Group(
        name="hardcore",
        description="Dynamic Campaign flight economy and Hardcore mode"
    )

    def __init__(self, bot: DCSServerBot, eventlistener=None):
        super().__init__(bot, eventlistener)
        self.economy = EconomyService(self)

    def economy_servers(self) -> list[Server]:
        return [
            server for server in self.bot.servers.values()
            if self.get_config(server).get("enabled", False)
        ]

    async def resolve_economy_server(self) -> Server | None:
        servers = self.economy_servers()
        if len(servers) == 1:
            return servers[0]
        # This plugin intentionally requires exactly one enabled economy server
        # for player-facing commands so balances cannot be charged against an
        # ambiguous campaign.
        return None

    @hardcore.command(description="Check your flight economy status and campaign balance.")
    @app_commands.guild_only()
    @utils.app_has_role("DCS")
    async def status(self, interaction: discord.Interaction):
        ucid = await self.bot.get_ucid_by_member(interaction.user)
        if not ucid:
            await interaction.response.send_message(
                "❌ Your Discord account is not linked to a DCS UCID.",
                ephemeral=True
            )
            return

        server = await self.resolve_economy_server()
        if not server:
            await interaction.response.send_message(
                "❌ Hardcore Economy must have exactly one server with `enabled: true`.",
                ephemeral=True
            )
            return

        data = await self.economy.status_for(server, ucid)
        mode = "🔥 HARDCORE (+50%)" if data["hardcore"] else "🛡️ NORMAL"

        embed = discord.Embed(title="✈️ Pilot Economy Profile", color=discord.Color.blue())
        embed.add_field(name="Pilot", value=interaction.user.display_name, inline=True)
        embed.add_field(name="Mode", value=mode, inline=True)
        embed.add_field(name="Campaign", value=data["campaign_name"] or "No active campaign", inline=False)
        embed.add_field(name="Bank Balance", value=f'{data["balance"]:,} cr', inline=True)

        session = data["session"]
        if session:
            m, s = divmod(session["eligible_seconds"], 60)
            embed.add_field(name="Current Eligible Time", value=f"{m}m {s}s", inline=True)
            embed.add_field(name="Current Losses", value=str(session["deaths"]), inline=True)
            if session["hardcore_revoked"]:
                embed.add_field(
                    name="Session Notice",
                    value="Hardcore was revoked during this session; the whole session settles at Normal rate.",
                    inline=False
                )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @hardcore.command(description="Pay the configured buy-in and activate Hardcore Mode.")
    @app_commands.guild_only()
    @utils.app_has_role("DCS")
    async def join(self, interaction: discord.Interaction):
        ucid = await self.bot.get_ucid_by_member(interaction.user)
        if not ucid:
            await interaction.response.send_message(
                "❌ Your Discord account is not linked to a DCS UCID.",
                ephemeral=True
            )
            return

        server = await self.resolve_economy_server()
        if not server:
            await interaction.response.send_message(
                "❌ Hardcore Economy must have exactly one server with `enabled: true`.",
                ephemeral=True
            )
            return

        ok, message, balance = await self.economy.enroll(server, ucid)
        if ok:
            await interaction.response.send_message(
                f"🔥 **{message}**\nNew balance: **{balance:,} cr**",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)

    @hardcore.command(description="Leave Hardcore Mode. Buy-in fees are non-refundable.")
    @app_commands.guild_only()
    @utils.app_has_role("DCS")
    async def leave(self, interaction: discord.Interaction):
        ucid = await self.bot.get_ucid_by_member(interaction.user)
        if not ucid:
            await interaction.response.send_message(
                "❌ Your Discord account is not linked to a DCS UCID.",
                ephemeral=True
            )
            return

        ok, message = await self.economy.leave(ucid)
        await interaction.response.send_message(
            ("🛡️ " if ok else "❌ ") + message,
            ephemeral=True
        )

    @hardcore.command(description="[ADMIN] Inspect a member's Hardcore economy state.")
    @app_commands.guild_only()
    @utils.app_has_role("DCS Admin")
    async def admin_check(self, interaction: discord.Interaction, member: discord.Member):
        ucid = await self.bot.get_ucid_by_member(member)
        if not ucid:
            await interaction.response.send_message(
                f"❌ {member.display_name} is not linked to a DCS UCID.",
                ephemeral=True
            )
            return

        server = await self.resolve_economy_server()
        if not server:
            await interaction.response.send_message(
                "❌ Hardcore Economy must have exactly one server with `enabled: true`.",
                ephemeral=True
            )
            return

        data = await self.economy.status_for(server, ucid)
        embed = discord.Embed(
            title=f"🛠️ Hardcore Audit: {member.display_name}",
            color=discord.Color.orange()
        )
        embed.add_field(name="UCID", value=f"`{ucid}`", inline=False)
        embed.add_field(
            name="Mode",
            value="🔥 HARDCORE" if data["hardcore"] else "🛡️ NORMAL",
            inline=True
        )
        embed.add_field(name="Balance", value=f'{data["balance"]:,} cr', inline=True)
        embed.add_field(name="Campaign", value=data["campaign_name"] or "None", inline=False)

        if data["session"]:
            session = data["session"]
            m, s = divmod(session["eligible_seconds"], 60)
            embed.add_field(name="Open Session", value=f"{m}m {s}s", inline=True)
            embed.add_field(name="Losses", value=str(session["deaths"]), inline=True)
            embed.add_field(name="Airborne", value=str(session["airborne"]), inline=True)
            embed.add_field(
                name="Hardcore Revoked",
                value=str(session["hardcore_revoked"]),
                inline=True
            )
        else:
            embed.add_field(name="Open Session", value="None", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @hardcore.command(description="[ADMIN] Force a member into or out of Hardcore Mode.")
    @app_commands.guild_only()
    @utils.app_has_role("DCS Admin")
    async def admin_set(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        active: bool
    ):
        ucid = await self.bot.get_ucid_by_member(member)
        if not ucid:
            await interaction.response.send_message(
                f"❌ {member.display_name} is not linked to a DCS UCID.",
                ephemeral=True
            )
            return

        if await self.economy.has_open_session(ucid):
            await interaction.response.send_message(
                "❌ Refusing to change mode while that player has an open economy session.",
                ephemeral=True
            )
            return

        await self.economy.set_hardcore(
            ucid, active, f"ADMIN:{interaction.user.id}"
        )
        await interaction.response.send_message(
            f"🛠️ {member.display_name} is now "
            f"{'🔥 HARDCORE' if active else '🛡️ NORMAL'}.",
            ephemeral=True
        )


async def setup(bot: DCSServerBot):
    if "creditsystem" not in bot.plugins:
        raise PluginRequiredError("creditsystem")
    await bot.add_cog(Hardcore(bot, HardcoreEventListener))
