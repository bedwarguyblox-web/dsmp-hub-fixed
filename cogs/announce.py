"""
announce.py — /message command.
Lets authorized staff send a formatted embedded message through the bot.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.permissions import is_authorized

logger = logging.getLogger(__name__)

_COLOR_MAP = {
    "blue":   discord.Color.blue(),
    "green":  discord.Color.green(),
    "red":    discord.Color.red(),
    "gold":   discord.Color.gold(),
    "purple": discord.Color.purple(),
    "white":  discord.Color.from_rgb(255, 255, 255),
}


class AnnounceCog(commands.Cog, name="Announce"):
    """Bot-announcement command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="message",
        description="Send an embedded message from the bot to any channel",
    )
    @app_commands.describe(
        channel="Channel to send the message in",
        message="The message body to send",
        title="Optional embed title",
        color="Embed accent color (default: blue)",
    )
    @app_commands.choices(color=[
        app_commands.Choice(name="Blue",   value="blue"),
        app_commands.Choice(name="Green",  value="green"),
        app_commands.Choice(name="Red",    value="red"),
        app_commands.Choice(name="Gold",   value="gold"),
        app_commands.Choice(name="Purple", value="purple"),
        app_commands.Choice(name="White",  value="white"),
    ])
    async def message(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        title: Optional[str] = None,
        color: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "message"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above (or granted `message` access) to use this command.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        chosen_color = _COLOR_MAP.get(color.value if color else "blue", discord.Color.blue())

        embed = discord.Embed(
            description=message,
            color=chosen_color,
            timestamp=datetime.now(timezone.utc),
        )
        if title:
            embed.title = title
        embed.set_footer(text=f"Sent by {interaction.user.display_name}")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ No Access",
                    description=f"I don't have permission to send messages in {channel.mention}.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        logger.info(
            "/message used by %s → #%s (%s) in %s",
            interaction.user, channel.name, channel.id, interaction.guild.name,
        )

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Message Sent",
                description=f"Your message was delivered to {channel.mention}.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AnnounceCog(bot))
