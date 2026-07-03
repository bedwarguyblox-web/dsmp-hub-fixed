"""
blacklist.py — Partner server blacklist.

Stores blacklisted servers by their permanent Discord server ID (resolved from
any invite link). When a blacklisted server's invite is posted in the
partnership channel the bot:
  1. Deletes the message instantly.
  2. DMs the person who posted it.
  3. DMs every online member holding a Partnership role to alert them.
  4. Logs to PARTNERSHIP_LOGS_CHANNEL_ID if configured.

Commands (Admin+):
  /blacklist add    invite:<link>  reason:<text>
  /blacklist remove server_id:<id>
  /blacklist check  invite:<link>
  /blacklist list
"""

import re
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import (
    blacklist_add, blacklist_remove, blacklist_check, blacklist_list,
    log_staff_action,
)
from utils.permissions import is_authorized, CONFIG

logger = logging.getLogger(__name__)

# Matches discord.gg/CODE  or  discord.com/invite/CODE
_INVITE_RE = re.compile(
    r"discord(?:\.gg|(?:app)?\.com/invite)/([a-zA-Z0-9\-]+)",
    re.IGNORECASE,
)

# Partnership roles that should receive DM alerts
_PARTNERSHIP_ROLES = [
    "Jr Partnership Manager",
    "Partnership Manager",
    "Sr Partnership Manager",
    "Head Partnership Manager",
]


async def _resolve_invite(bot: commands.Bot, code: str):
    """
    Resolve an invite code to a discord.Invite object.
    Returns None on failure.
    """
    try:
        return await bot.fetch_invite(code, with_counts=False)
    except (discord.NotFound, discord.HTTPException):
        return None


async def _notify_partnership_staff(guild: discord.Guild, embed: discord.Embed):
    """DM every online member who holds a partnership role."""
    role_ids = {
        CONFIG.get("STAFF_ROLES", {}).get(r)
        for r in _PARTNERSHIP_ROLES
    } - {None}

    notified: set[int] = set()
    for member in guild.members:
        if member.bot or member.id in notified:
            continue
        member_role_ids = {r.id for r in member.roles}
        if member_role_ids & role_ids:
            try:
                await member.send(embed=embed)
                notified.add(member.id)
            except discord.Forbidden:
                pass   # DMs closed — skip silently


class BlacklistCog(commands.Cog, name="Blacklist"):
    """Partner server blacklist with auto-detection in the partnership channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /blacklist group ──────────────────────────────────────────────────────
    bl_group = app_commands.Group(
        name="blacklist",
        description="Partner server blacklist management",
    )

    # ── /blacklist add ────────────────────────────────────────────────────────
    @bl_group.command(
        name="add",
        description="Blacklist a server by invite link (Admin+)",
    )
    @app_commands.describe(
        invite="Any valid invite link for the server",
        reason="Why this server is blacklisted",
    )
    async def bl_add(
        self,
        interaction: discord.Interaction,
        invite: str,
        reason: str,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "blacklist"):
            await interaction.followup.send(embed=_err("You must be **Admin** or above."), ephemeral=True)
            return

        code = _extract_code(invite)
        if not code:
            await interaction.followup.send(embed=_err("That doesn't look like a valid Discord invite link."), ephemeral=True)
            return

        inv = await _resolve_invite(self.bot, code)
        if not inv or not inv.guild:
            await interaction.followup.send(embed=_err("Couldn't resolve that invite — it may be expired or invalid."), ephemeral=True)
            return

        server_id   = inv.guild.id
        server_name = inv.guild.name

        added = blacklist_add(
            server_id   = server_id,
            server_name = server_name,
            reason      = reason,
            added_by    = interaction.user.id,
            guild_id    = interaction.guild.id,
        )

        log_staff_action("blacklist_add", interaction.user.id, interaction.guild.id,
                         target_id=server_id, details=f"{server_name} | {reason}")

        if added:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🚫 Server Blacklisted",
                    description=(
                        f"**{discord.utils.escape_markdown(server_name)}** `({server_id})` has been blacklisted.\n"
                        f"**Reason:** {reason}"
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=_warn(f"**{discord.utils.escape_markdown(server_name)}** is already on the blacklist."),
                ephemeral=True,
            )

    # ── /blacklist remove ─────────────────────────────────────────────────────
    @bl_group.command(
        name="remove",
        description="Remove a server from the blacklist by its server ID (Admin+)",
    )
    @app_commands.describe(server_id="The Discord server ID to remove")
    async def bl_remove(
        self,
        interaction: discord.Interaction,
        server_id: str,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "blacklist"):
            await interaction.followup.send(embed=_err("You must be **Admin** or above."), ephemeral=True)
            return

        try:
            sid = int(server_id)
        except ValueError:
            await interaction.followup.send(embed=_err("Server ID must be a number."), ephemeral=True)
            return

        removed = blacklist_remove(sid)
        log_staff_action("blacklist_remove", interaction.user.id, interaction.guild.id, target_id=sid)

        if removed:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="✅ Removed from Blacklist",
                    description=f"Server `{sid}` has been removed.",
                    color=discord.Color.green(),
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=_warn(f"Server `{sid}` was not found on the blacklist."),
                ephemeral=True,
            )

    # ── /blacklist check ──────────────────────────────────────────────────────
    @bl_group.command(
        name="check",
        description="Check whether a server is blacklisted (Admin+)",
    )
    @app_commands.describe(invite="Any valid invite link for the server")
    async def bl_check(
        self,
        interaction: discord.Interaction,
        invite: str,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "blacklist"):
            await interaction.followup.send(embed=_err("You must be **Admin** or above."), ephemeral=True)
            return

        code = _extract_code(invite)
        if not code:
            await interaction.followup.send(embed=_err("That doesn't look like a valid Discord invite link."), ephemeral=True)
            return

        inv = await _resolve_invite(self.bot, code)
        if not inv or not inv.guild:
            await interaction.followup.send(embed=_err("Couldn't resolve that invite."), ephemeral=True)
            return

        row = blacklist_check(inv.guild.id)
        if row:
            embed = discord.Embed(
                title="🚫 Server is Blacklisted",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Server", value=f"{discord.utils.escape_markdown(row['server_name'])} `({row['server_id']})`", inline=False)
            embed.add_field(name="Reason", value=row["reason"], inline=False)
            embed.add_field(name="Blacklisted", value=f"<t:{_ts(row['timestamp'])}:R>", inline=True)
            embed.add_field(name="Added by", value=f"<@{row['added_by']}>", inline=True)
        else:
            embed = discord.Embed(
                title="✅ Not Blacklisted",
                description=f"**{discord.utils.escape_markdown(inv.guild.name)}** is not on the blacklist.",
                color=discord.Color.green(),
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /blacklist list ───────────────────────────────────────────────────────
    @bl_group.command(
        name="list",
        description="Show all blacklisted servers (Admin+)",
    )
    async def bl_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "blacklist"):
            await interaction.followup.send(embed=_err("You must be **Admin** or above."), ephemeral=True)
            return

        rows = blacklist_list(interaction.guild.id)

        if not rows:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="📋 Partner Blacklist",
                    description="No servers are currently blacklisted.",
                    color=discord.Color.blurple(),
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🚫 Partner Blacklist — {len(rows)} server{'s' if len(rows) != 1 else ''}",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        for row in rows[:20]:   # cap at 20 fields to stay under Discord limit
            embed.add_field(
                name=f"{discord.utils.escape_markdown(row['server_name'])} ({row['server_id']})",
                value=f"**Reason:** {row['reason']}\nAdded by <@{row['added_by']}> • <t:{_ts(row['timestamp'])}:R>",
                inline=False,
            )
        if len(rows) > 20:
            embed.set_footer(text=f"Showing 20 of {len(rows)} entries")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Auto-detect in partnership channel ───────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return

        partnership_ch_id = CONFIG.get("PARTNERSHIP_CHANNEL_ID")
        if not partnership_ch_id:
            return
        if message.channel.id != partnership_ch_id:
            return

        codes = _INVITE_RE.findall(message.content)
        if not codes:
            return

        for code in codes:
            inv = await _resolve_invite(self.bot, code)
            if not inv or not inv.guild:
                continue

            row = blacklist_check(inv.guild.id)
            if not row:
                continue

            # ── Blacklisted server detected ───────────────────────────────
            server_name = discord.utils.escape_markdown(row["server_name"])

            # 1. Delete the message
            try:
                await message.delete()
            except discord.Forbidden:
                logger.warning("Could not delete blacklisted invite in #%s", message.channel.id)

            # 2. DM the person who posted it
            poster_embed = discord.Embed(
                title="🚫 Partnership Denied",
                description=(
                    f"Your message in **{message.guild.name}** was removed because "
                    f"**{server_name}** is on our partner blacklist.\n\n"
                    f"**Reason:** {row['reason']}"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            try:
                await message.author.send(embed=poster_embed)
            except discord.Forbidden:
                pass

            # 3. Alert embed for staff
            alert_embed = discord.Embed(
                title="🚨 Blacklisted Server Detected",
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc),
            )
            alert_embed.add_field(name="Server", value=f"{server_name} `({row['server_id']})`", inline=False)
            alert_embed.add_field(name="Reason on blacklist", value=row["reason"], inline=False)
            alert_embed.add_field(name="Posted by", value=f"{message.author.mention} `({message.author.id})`", inline=True)
            alert_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
            alert_embed.add_field(name="Blacklisted by", value=f"<@{row['added_by']}>", inline=True)
            alert_embed.set_footer(text="Message was auto-deleted")

            # 4. DM all partnership staff
            await _notify_partnership_staff(message.guild, alert_embed)

            # 5. Log to partnership logs channel if configured
            logs_ch_id = CONFIG.get("PARTNERSHIP_LOGS_CHANNEL_ID")
            if logs_ch_id:
                logs_ch = message.guild.get_channel(logs_ch_id)
                if logs_ch:
                    try:
                        await logs_ch.send(embed=alert_embed)
                    except discord.Forbidden:
                        pass

            log_staff_action(
                "blacklist_auto_delete", self.bot.user.id, message.guild.id,
                target_id=inv.guild.id,
                details=f"Posted by {message.author.id} in #{message.channel.id}",
            )

            # Only need to act on the first blacklisted invite per message
            break


# ── Small embed helpers ───────────────────────────────────────────────────────

def _err(text: str) -> discord.Embed:
    return discord.Embed(title="❌ Error", description=text, color=discord.Color.red())

def _warn(text: str) -> discord.Embed:
    return discord.Embed(title="⚠️ Warning", description=text, color=discord.Color.orange())

def _extract_code(text: str) -> str | None:
    m = _INVITE_RE.search(text)
    return m.group(1) if m else None

def _ts(iso: str) -> int:
    """Convert ISO datetime string to Unix timestamp for Discord's <t:N:R> format."""
    try:
        dt = datetime.fromisoformat(iso)
        return int(dt.timestamp())
    except Exception:
        return 0


async def setup(bot: commands.Bot):
    await bot.add_cog(BlacklistCog(bot))
