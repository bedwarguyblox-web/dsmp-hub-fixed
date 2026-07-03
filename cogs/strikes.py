"""
strikes.py — /strike, /removestrike, /checkstrikes commands.
Handles per-user strike tracking with full history and logging.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone

from utils.permissions import is_authorized, CONFIG
from utils.database import (
    add_strike, remove_strike, get_strike_count,
    get_strike_history, log_staff_action, reset_all_strikes
)
from cogs.activitycheck import _auto_demote_staff

logger = logging.getLogger(__name__)

# Cooldown: 1 strike per user per 10 seconds (prevents rapid spam)
STRIKE_COOLDOWN = app_commands.checks.cooldown(1, 10.0, key=lambda i: (i.guild_id, i.user.id))


class StrikesCog(commands.Cog, name="Strikes"):
    """Strike management commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /strike ─────────────────────────────────────────────────────────────
    @app_commands.command(name="strike", description="Add one strike to a user")
    @app_commands.describe(
        user="The member to strike",
        reason="Reason for the strike"
    )
    @STRIKE_COOLDOWN
    async def strike(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "strike"):
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="You must be **Admin** or above to issue strikes.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Prevent striking yourself
        if user.id == interaction.user.id:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Invalid Target", description="You cannot strike yourself.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        # Prevent striking the bot
        if user.bot:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Invalid Target", description="You cannot strike a bot.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        guild = interaction.guild
        new_count = add_strike(user.id, guild.id, interaction.user.id, reason)

        log_staff_action(
            "strike_add", interaction.user.id, guild.id,
            target_id=user.id,
            details=f"Reason: {reason} | New total: {new_count}"
        )

        # ── Response embed ──────────────────────────────────────────────────
        will_demote = new_count >= 3
        embed = discord.Embed(
            title="⚡ Strike Issued",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="User",          value=user.mention,             inline=True)
        embed.add_field(name="Moderator",     value=interaction.user.mention, inline=True)
        embed.add_field(name="Total Strikes", value=str(new_count),           inline=True)
        embed.add_field(name="Reason",        value=reason,                   inline=False)
        if will_demote:
            embed.add_field(
                name="🔴 Auto-Demotion Triggered",
                value="This member has reached **3 strikes** — all staff roles are being removed.",
                inline=False,
            )
        embed.set_footer(text=f"User ID: {user.id}")
        await interaction.followup.send(embed=embed)

        # ── Log channel ─────────────────────────────────────────────────────
        await self._send_to_logs(guild, embed)

        # ── DM the user ─────────────────────────────────────────────────────
        try:
            dm_embed = discord.Embed(
                title=f"⚡ You received a strike in {guild.name}",
                description=f"**Reason:** {reason}\n**Total strikes:** {new_count}",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            pass  # User has DMs closed — silently ignore

        # ── Auto-demote if 3+ strikes ────────────────────────────────────────
        if will_demote:
            await _auto_demote_staff(
                self.bot, user, guild, new_count,
                f"Reached 3 strikes (last: {reason})"
            )

    # ── /removestrike ────────────────────────────────────────────────────────
    @app_commands.command(name="removestrike", description="Remove one strike from a user")
    @app_commands.describe(user="The member whose strike to remove")
    async def removestrike(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "removestrike"):
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="You must be **Admin** or above to remove strikes.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        guild = interaction.guild
        current = get_strike_count(user.id, guild.id)

        if current == 0:
            embed = discord.Embed(
                title="ℹ️ No Strikes",
                description=f"{user.mention} has no strikes to remove.",
                color=discord.Color.blue(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        new_count = remove_strike(user.id, guild.id, interaction.user.id)
        log_staff_action(
            "strike_remove", interaction.user.id, guild.id,
            target_id=user.id,
            details=f"Removed 1 strike | New total: {new_count}"
        )

        embed = discord.Embed(
            title="✅ Strike Removed",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="User",          value=user.mention,            inline=True)
        embed.add_field(name="Performed By",  value=interaction.user.mention, inline=True)
        embed.add_field(name="Total Strikes", value=str(new_count),           inline=True)
        embed.set_footer(text=f"User ID: {user.id}")
        await interaction.followup.send(embed=embed)
        await self._send_to_logs(guild, embed)

    # ── /checkstrikes ────────────────────────────────────────────────────────
    @app_commands.command(name="checkstrikes", description="Check strike history for a user")
    @app_commands.describe(user="The member to check")
    async def checkstrikes(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "checkstrikes"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to check strike records.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        guild = interaction.guild
        count   = get_strike_count(user.id, guild.id)
        history = get_strike_history(user.id, guild.id, limit=5)

        embed = discord.Embed(
            title=f"📋 Strike Record — {user.display_name}",
            color=discord.Color.orange() if count > 0 else discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="User",          value=user.mention, inline=True)
        embed.add_field(name="Total Strikes", value=str(count),   inline=True)
        embed.add_field(name="\u200b",        value="\u200b",     inline=True)  # spacer

        if history:
            # Most recent strike details
            last = history[0]
            embed.add_field(name="Last Strike Reason", value=last["reason"],    inline=True)
            embed.add_field(name="Last Strike Date",   value=last["timestamp"], inline=True)
            embed.add_field(name="\u200b",             value="\u200b",          inline=True)

            # Full recent history
            history_lines = []
            for i, h in enumerate(history, 1):
                mod = guild.get_member(h["moderator_id"])
                mod_str = mod.display_name if mod else f"ID:{h['moderator_id']}"
                history_lines.append(
                    f"`{i}.` **{h['reason']}**\n"
                    f"    By {mod_str} on {h['timestamp'][:10]}"
                )
            embed.add_field(
                name=f"Recent History (last {len(history)})",
                value="\n".join(history_lines),
                inline=False
            )
        else:
            embed.add_field(name="History", value="No strike history found.", inline=False)

        embed.set_footer(text=f"User ID: {user.id} • Resets every Monday 08:00 UTC+8")
        await interaction.followup.send(embed=embed)

    # ── /clearallstrikes ─────────────────────────────────────────────────────
    @app_commands.command(name="clearallstrikes", description="Clear all strikes for every user in this server")
    async def clearallstrikes(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "clearallstrikes"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to clear all strikes.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        guild = interaction.guild
        count = reset_all_strikes(guild.id)

        log_staff_action(
            "strike_reset_manual", interaction.user.id, guild.id,
            details=f"Manual clear-all | {count} record(s) reset"
        )

        embed = discord.Embed(
            title="⚙️ All Strikes Cleared",
            description=(
                f"All strikes have been manually reset.\n"
                f"**{count}** user record(s) cleared."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Performed by {interaction.user} (ID: {interaction.user.id})")
        await interaction.followup.send(embed=embed)
        await self._send_to_logs(guild, embed)

    # ── Cooldown error handler ───────────────────────────────────────────────
    @strike.error
    async def on_cooldown(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = discord.Embed(
                title="⏳ Slow Down",
                description=f"Please wait **{error.retry_after:.1f}s** before issuing another strike.",
                color=discord.Color.yellow(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            raise error

    # ── Internal: log channel helper ─────────────────────────────────────────
    async def _send_to_logs(self, guild: discord.Guild, embed: discord.Embed):
        channel_id = CONFIG.get("STRIKE_LOGS_CHANNEL_ID")
        if not channel_id:
            return
        ch = guild.get_channel(channel_id)
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                logger.warning("Cannot send to strike logs channel %s", channel_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(StrikesCog(bot))
